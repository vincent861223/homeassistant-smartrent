"""Platform for lock integration."""
import asyncio
import logging
from typing import Any, Optional, Union

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType
from smartrent import DoorLock
from smartrent.utils import SmartRentError

from .const import CONFIGURATION_URL, PROPER_NAME
from .patches import async_log_hub_status

_LOGGER = logging.getLogger(__name__)

# Every websocket echo observed in testing and production arrived within
# 0-4.5s; REST fallback itself resolves in ~100-200ms once triggered. 15s was
# needlessly conservative and is exactly what made a hub-side hiccup feel like
# "the lock doesn't respond" -- the command was retried and eventually
# confirmed correctly, just after a much longer wait than necessary.
CONFIRM_TIMEOUT = 6.0
# Slow echoes have landed up to 14s after the first send, so the resend gets
# the rest of that window rather than failing a command that is still in flight.
RESEND_CONFIRM_TIMEOUT = 10.0
CONFIRM_POLL_INTERVAL = 0.5


async def async_setup_entry(hass, entry, async_add_entities):
    """Setup lock platform."""
    client = hass.data["smartrent"][entry.entry_id]
    locks = client.get_locks()
    for lock in locks:
        async_add_entities([SmartrentLock(lock)])


class SmartrentLock(LockEntity):
    def __init__(self, lock: DoorLock) -> None:
        super().__init__()
        self.device = lock
        self._attr_supported_features = LockEntityFeature.OPEN
        self._pending: Optional[bool] = None

        self.device.start_updater()
        self.device.set_update_callback(self.async_schedule_update_ha_state)

    @property
    def supported_features(self):
        """Flag supported features."""
        return LockEntityFeature.OPEN

    @property
    def should_poll(self):
        """Return the polling state, if needed."""
        return False

    @property
    def available(self) -> bool:
        """SmartRent reports the device offline while the hub is unreachable.

        Without this the entity keeps showing a stale lock state during a hub
        outage, which makes a dead integration look perfectly healthy.
        """
        return bool(self.device.get_online())

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self.device._device_id

    @property
    def name(self):
        """Return the display name of this lock."""
        return self.device._name

    @property
    def changed_by(self) -> Union[str, None]:
        return self.device.get_notification()

    @property
    def is_locked(self) -> Union[bool, None]:
        return self.device.get_locked()

    @property
    def is_locking(self) -> bool:
        return self._pending is True

    @property
    def is_unlocking(self) -> bool:
        return self._pending is False

    @property
    def is_jammed(self) -> Union[bool, None]:
        return "ALARM_TYPE_9" in str(self.device.get_notification())

    async def async_lock(self, **kwargs: Any):
        await self._async_set_locked(True)

    async def async_unlock(self, **kwargs: Any):
        await self._async_set_locked(False)

    async def _async_set_locked(self, locked: bool) -> None:
        """Send the command and confirm the hub actually applied it.

        Raises so callers see a failure instead of a silently dropped command.
        """
        action = "lock" if locked else "unlock"
        self._pending = locked
        self.async_write_ha_state()
        try:
            await self.device.async_set_locked(locked)
            if not await self._async_confirm(locked):
                raise HomeAssistantError(
                    f"{self.name}: lock did not confirm {action} after resending"
                )
        except SmartRentError as err:
            raise HomeAssistantError(f"{self.name}: {action} failed: {err}") from err
        finally:
            self._pending = None
            self.async_write_ha_state()

    async def _async_wait_for_echo(self, locked: bool, timeout: float) -> bool:
        """Wait for the lock's own report that it reached ``locked``."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() < started + timeout:
            if self.device.get_locked() is locked:
                _LOGGER.debug(
                    "TRACE confirm OK locked=%s via websocket echo in %.1fs",
                    locked,
                    loop.time() - started,
                )
                return True
            await asyncio.sleep(CONFIRM_POLL_INTERVAL)
        return False

    async def _async_confirm(self, locked: bool) -> bool:
        """Wait for the websocket echo, resending the command once without it.

        REST cannot stand in for the echo: SmartRent's cloud reports the
        requested value once it accepts a command, even if the lock never
        moved. Resending is safe because the command is idempotent.
        """
        if await self._async_wait_for_echo(locked, CONFIRM_TIMEOUT):
            return True

        # Also record whether the hub itself is reachable from SmartRent's
        # cloud -- if it's not, that would affect the official app too, and is
        # not something this integration caused or can fix by itself.
        _LOGGER.warning(
            "TRACE confirm no websocket echo for locked=%s after %.0fs -- resending",
            locked,
            CONFIRM_TIMEOUT,
        )
        await async_log_hub_status(
            self.device._client, context=f"confirm-timeout locked={locked}"
        )
        await self.device.async_set_locked(locked)
        if await self._async_wait_for_echo(locked, RESEND_CONFIRM_TIMEOUT):
            return True

        _LOGGER.error(
            "TRACE confirm FAILED locked=%s -- no echo after resending", locked
        )
        return False

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
