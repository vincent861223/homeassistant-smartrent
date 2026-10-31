"""Platform for climate integration."""
import asyncio
import logging
from typing import Awaitable, Callable, Optional

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    FAN_AUTO,
    FAN_ON,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType
from smartrent import Thermostat
from smartrent.utils import SmartRentError

from .const import CONFIGURATION_URL, DOMAIN, PROPER_NAME
from .patches import async_log_hub_status

_LOGGER = logging.getLogger(__name__)

# HomeKit and voice assistants can send several rapid setpoint updates in a
# row (e.g. dragging a slider), so confirmation here is non-blocking -- it
# never delays the service call the way the lock's does. It only verifies,
# in the background, that SmartRent's cloud accepting the command actually
# meant the physical hub applied it, which is not the same thing: a command
# can get a clean "ok" from the cloud and never reach the hardware if the hub
# itself is unresponsive.
CONFIRM_TIMEOUT = 6.0
RESEND_CONFIRM_TIMEOUT = 10.0
CONFIRM_POLL_INTERVAL = 0.5

HA_HVAC_MODE_TO_SMARTRENT = {
    HVACMode.COOL: "cool",
    HVACMode.HEAT: "heat",
    HVACMode.OFF: "off",
    HVACMode.HEAT_COOL: "auto",
}
SMARTRENT_HVAC_MODE_TO_HA = {
    value: key for key, value in HA_HVAC_MODE_TO_SMARTRENT.items()
}
HA_HVAC_ACTION_TO_SMARTRENT = {
    HVACAction.COOLING: "cooling",
    HVACAction.HEATING: "heating",
    HVACAction.OFF: "off",
}
SMARTRENT_HVAC_ACTION_TO_HA = {
    value: key for key, value in HA_HVAC_ACTION_TO_SMARTRENT.items()
}

HA_FAN_TO_SMART_RENT = {FAN_ON: "on", FAN_AUTO: "auto"}
SMARTRENT_FAN_TO_HA = {value: key for key, value in HA_FAN_TO_SMART_RENT.items()}

SUPPORT_FAN = [FAN_ON, FAN_AUTO]
SUPPORT_HVAC = [HVACMode.HEAT, HVACMode.COOL, HVACMode.OFF, HVACMode.HEAT_COOL]


async def async_setup_entry(hass, entry, async_add_entities):
    """Setup climate platform."""
    client = hass.data[DOMAIN][entry.entry_id]
    thermostats = client.get_thermostats()
    for thermostat in thermostats:
        async_add_entities([SmartrentThermostat(thermostat)])


class SmartrentThermostat(ClimateEntity):
    def __init__(self, thermo: Thermostat) -> None:
        super().__init__()
        self.device = thermo
        self._latest_command: dict[str, object] = {}

        self.device.start_updater()
        self.device.set_update_callback(self.async_schedule_update_ha_state)

    @property
    def should_poll(self):
        """Return the polling state, if needed."""
        return False

    @property
    def available(self) -> bool:
        """SmartRent reports the device offline while the hub is unreachable.

        Without this the entity keeps showing a stale setpoint during a hub
        outage, which makes a dead integration look perfectly healthy.
        """
        return bool(self.device.get_online())

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self.device._device_id

    @property
    def name(self):
        """Return the display name of this thermostat."""
        return self.device._name

    @property
    def supported_features(self):
        """Return the list of supported features."""

        # binary list of supported features
        supports_features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF

        fan_mode = self.device.get_fan_mode()
        mode = self.device.get_mode()

        if mode in ["auto", "off"]:
            supports_features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE

        if mode in ["heat", "cool"]:
            supports_features |= ClimateEntityFeature.TARGET_TEMPERATURE

        # if fan has an active mode, assume fan option exists on thermostat
        if fan_mode:
            supports_features |= ClimateEntityFeature.FAN_MODE

        return supports_features

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.FAHRENHEIT

    @property
    def current_temperature(self):
        return self.device.get_current_temp()

    @property
    def target_temperature_high(self):
        return self.device.get_cooling_setpoint()

    @property
    def target_temperature_low(self):
        return self.device.get_heating_setpoint()

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        if self.device.get_mode() == "cool":
            return self.device.get_cooling_setpoint()
        elif self.device.get_mode() == "heat":
            return self.device.get_heating_setpoint()
        else:
            return self.device.get_current_temp()

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        return 1

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        return 60

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return 90

    @property
    def current_humidity(self):
        return self.device.get_current_humidity()

    @property
    def hvac_mode(self):
        """Return current operation ie. heat, cool, idle."""
        smartrent_hvac_mode = self.device.get_mode()

        return SMARTRENT_HVAC_MODE_TO_HA.get(smartrent_hvac_mode, None)

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        return SUPPORT_HVAC

    async def _async_send(
        self,
        what: str,
        send: Callable[[], Awaitable[None]],
        confirm: Optional[
            tuple[Callable[[], object], object, Callable[[object], str]]
        ] = None,
    ) -> None:
        """Send a command, surfacing delivery failures to the caller.

        ``confirm``, if given, is ``(getter, target, describe)``: a
        background task polls ``getter()`` against ``target``, resends once
        without an echo, and logs loudly -- with hub status -- if the hub
        still never applied the change, without blocking this call.
        """
        baseline = confirm[0]() if confirm else None
        command = object()
        self._latest_command[what.partition("=")[0]] = command
        try:
            await send()
        except SmartRentError as err:
            raise HomeAssistantError(f"{self.name}: {what} failed: {err}") from err

        if confirm is not None:
            getter, target, describe = confirm
            self.hass.async_create_task(
                self._async_confirm_background(
                    what, send, command, baseline, getter, target, describe
                ),
                name=f"smartrent confirm {what}",
            )

    @staticmethod
    async def _async_wait_for_echo(
        what: str, getter: Callable[[], object], target: object, timeout: float
    ) -> bool:
        """Wait for the hub's own report that ``getter()`` reached ``target``."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() < started + timeout:
            if getter() == target:
                _LOGGER.debug(
                    "TRACE confirm OK %s via websocket echo in %.1fs",
                    what,
                    loop.time() - started,
                )
                return True
            await asyncio.sleep(CONFIRM_POLL_INTERVAL)
        return False

    async def _async_confirm_background(
        self,
        what: str,
        send: Callable[[], Awaitable[None]],
        command: object,
        baseline: object,
        getter: Callable[[], object],
        target: object,
        describe: Callable[[object], str],
    ) -> None:
        """Best-effort, non-blocking check that a command actually landed.

        SmartRent's cloud can acknowledge a command the physical hub never
        applies -- the delivery check in patches.py only confirms the cloud
        accepted it, not that the hardware moved. REST cannot settle it either,
        since the cloud reports the requested value until the hub overrides it.
        """
        if await self._async_wait_for_echo(what, getter, target, CONFIRM_TIMEOUT):
            return

        _LOGGER.warning(
            "TRACE confirm no websocket echo for %s after %.0fs",
            what,
            CONFIRM_TIMEOUT,
        )
        await async_log_hub_status(
            self.device._client, context=f"confirm-timeout {what}"
        )

        # A resend must never undo something newer: a later command for this
        # attribute (HomeKit slider drags), or a change made at the thermostat.
        attribute = what.partition("=")[0]
        if self._latest_command.get(attribute) is not command or getter() != baseline:
            _LOGGER.debug("TRACE confirm %s superseded, not resending", what)
            return

        try:
            await send()
        except (SmartRentError, OSError) as err:
            _LOGGER.error("TRACE confirm FAILED %s -- resend failed: %s", what, err)
            return

        if not await self._async_wait_for_echo(
            what, getter, target, RESEND_CONFIRM_TIMEOUT
        ):
            _LOGGER.error(
                "TRACE confirm FAILED %s -- hub never applied it (%s)",
                what,
                describe(getter()),
            )

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target operation mode."""
        smartrent_hvac_mode = HA_HVAC_MODE_TO_SMARTRENT.get(hvac_mode)

        await self._async_send(
            f"hvac_mode={hvac_mode}",
            lambda: self.device.async_set_mode(smartrent_hvac_mode),
            confirm=(
                self.device.get_mode,
                smartrent_hvac_mode,
                lambda actual: f"requested mode={smartrent_hvac_mode} actual={actual}",
            ),
        )

    @property
    def hvac_action(self) -> Optional[HVACAction]:
        """Return the current running hvac operation ie. cooling, heating, off"""
        return SMARTRENT_HVAC_ACTION_TO_HA.get(self.device.get_operating_state())

    @staticmethod
    def _setpoint_confirm(attribute: str, requested, getter: Callable[[], object]):
        """Build a ``confirm`` tuple for a setpoint. The hub stores whole
        degrees and rounds what it is sent (78.98 comes back as 79), so the
        target must match that or a legitimate confirmation would never
        compare equal."""
        target = round(float(requested))
        return (
            getter,
            target,
            lambda actual: f"requested {attribute}={target} actual={actual}",
        )

    async def _async_set_cooling_setpoint(self, value) -> None:
        value = round(float(value))
        await self._async_send(
            f"cooling_setpoint={value}",
            lambda: self.device.async_set_cooling_setpoint(value),
            confirm=self._setpoint_confirm(
                "cooling_setpoint", value, self.device.get_cooling_setpoint
            ),
        )

    async def _async_set_heating_setpoint(self, value) -> None:
        value = round(float(value))
        await self._async_send(
            f"heating_setpoint={value}",
            lambda: self.device.async_set_heating_setpoint(value),
            confirm=self._setpoint_confirm(
                "heating_setpoint", value, self.device.get_heating_setpoint
            ),
        )

    async def async_set_temperature(self, **kwargs):
        mode = self.device.get_mode()

        # climate.set_temperature carries hvac_mode here instead of calling
        # async_set_hvac_mode. get_mode() stays stale until the hub echoes, so
        # the setpoint below follows the requested mode, not the reported one.
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode:
            requested_mode = HA_HVAC_MODE_TO_SMARTRENT.get(hvac_mode)
            if requested_mode != mode:
                await self.async_set_hvac_mode(hvac_mode)
            mode = requested_mode

        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature:
            if mode == "cool":
                await self._async_set_cooling_setpoint(temperature)
            else:
                await self._async_set_heating_setpoint(temperature)

        tt_high = kwargs.get("target_temp_high")
        if tt_high:
            await self._async_set_cooling_setpoint(tt_high)

        tt_low = kwargs.get("target_temp_low")
        if tt_low:
            await self._async_set_heating_setpoint(tt_low)

    @property
    def fan_mode(self):
        """Return the fan setting."""
        smartrent_fan_mode = self.device.get_fan_mode()

        return SMARTRENT_FAN_TO_HA.get(smartrent_fan_mode, None)

    async def async_set_fan_mode(self, fan_mode):
        """Set fan mode."""
        smartrent_fan_mode = HA_FAN_TO_SMART_RENT.get(fan_mode)

        await self._async_send(
            f"fan_mode={fan_mode}",
            lambda: self.device.async_set_fan_mode(smartrent_fan_mode),
            confirm=(
                self.device.get_fan_mode,
                smartrent_fan_mode,
                lambda actual: f"requested fan_mode={smartrent_fan_mode} "
                f"actual={actual}",
            ),
        )

    @property
    def fan_modes(self):
        """List of available fan modes."""
        return SUPPORT_FAN

    @property
    def device_info(self):
        return dict(
            identifiers={("id", self.device._device_id)},
            name=str(self.name),
            manufacturer=PROPER_NAME,
            model=str(self.device.__class__.__name__),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=CONFIGURATION_URL,
        )
