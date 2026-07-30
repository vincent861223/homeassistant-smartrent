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

_LOGGER = logging.getLogger(__name__)

# The hub echoes the new state back over the updater websocket in ~5s.
CONFIRM_TIMEOUT = 15.0
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
                    f"{self.name}: hub did not confirm {action} within "
                    f"{CONFIRM_TIMEOUT:.0f}s"
                )
        except SmartRentError as err:
            raise HomeAssistantError(f"{self.name}: {action} failed: {err}") from err
        finally:
            self._pending = None
            self.async_write_ha_state()

    async def _async_confirm(self, locked: bool) -> bool:
        """Wait for the websocket echo, falling back to an explicit REST read."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + CONFIRM_TIMEOUT
        while loop.time() < deadline:
            if self.device.get_locked() is locked:
                _LOGGER.debug(
                    "TRACE confirm OK locked=%s via websocket echo in %.1fs",
                    locked,
                    loop.time() - started,
                )
                return True
            await asyncio.sleep(CONFIRM_POLL_INTERVAL)

        # The updater websocket may be mid-reconnect, so ask the REST endpoint
        # rather than reporting a failure we are not sure about.
        _LOGGER.warning(
            "TRACE confirm no websocket echo for locked=%s after %.0fs "
            "(updater socket may be down) -- falling back to REST",
            locked,
            CONFIRM_TIMEOUT,
        )
        try:
            await self.device._async_fetch_state()
        except (SmartRentError, OSError) as err:
            _LOGGER.warning("TRACE confirm REST read failed: %s", err)
            return False

        confirmed = self.device.get_locked() is locked
        _LOGGER.log(
            logging.DEBUG if confirmed else logging.ERROR,
            "TRACE confirm %s locked=%s via REST (reported %s)",
            "OK" if confirmed else "FAILED",
            locked,
            self.device.get_locked(),
        )
        return confirmed

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
