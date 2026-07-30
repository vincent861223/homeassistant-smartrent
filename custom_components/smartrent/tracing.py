"""Observability for the SmartRent integration.

Everything here is read-only instrumentation: it wraps client methods to record
what happened and never changes behaviour. It exists because the failure we are
chasing -- SmartRent intermittently stops accepting commands -- left almost no
trace in the logs, and the periodic reload masked it before it could be observed.

Output goes to ``/config/smartrent_diagnostic.log`` (rotating, 4 x 2 MB) as well
as the normal Home Assistant log. The separate file matters because
``home-assistant.log`` keeps only one backup and rotates on every restart, so a
trace from an overnight failure is easily lost.
"""
import functools
import logging
import time
from logging.handlers import RotatingFileHandler

from smartrent.device import Device
from smartrent.utils import Client

_LOGGER = logging.getLogger(__name__)

DIAGNOSTIC_FILENAME = "smartrent_diagnostic.log"
MAX_BYTES = 2_000_000
BACKUP_COUNT = 3
# Loggers whose records are mirrored into the diagnostic file.
TRACED_LOGGERS = ("smartrent", "custom_components.smartrent")

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"

_handler: RotatingFileHandler | None = None
_instrumented = False

# Populated by the instrumentation below.
_ws_connects = 0
_last_ws_connect: float | None = None


def _ttl(client: Client) -> str:
    """Human-readable remaining access-token lifetime."""
    exp = getattr(client, "_token_exp_time", None)
    if not exp:
        return "ttl=unknown"
    return f"ttl={exp - time.time():+.0f}s"


def setup_file_log(config_dir: str) -> None:
    """Mirror the smartrent loggers into a dedicated rotating file."""
    global _handler
    if _handler is not None:
        return

    path = f"{config_dir}/{DIAGNOSTIC_FILENAME}"
    handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(logging.DEBUG)

    for name in TRACED_LOGGERS:
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        # The logger's own level still gates records; configuration.yaml sets
        # these to debug. Guard against an effective level that would drop
        # everything we care about.
        if logger.level > logging.DEBUG or logger.level == logging.NOTSET:
            logger.setLevel(logging.DEBUG)

    _handler = handler
    _LOGGER.info("SmartRent diagnostic log -> %s", path)


def _wrap_fetch_state():
    """Log device online/offline transitions and fetch latency."""
    original = Device._async_fetch_state

    @functools.wraps(original)
    async def wrapper(self: Device):
        before = self._online
        started = time.monotonic()
        try:
            result = await original(self)
        except Exception as exc:
            _LOGGER.warning(
                "TRACE fetch FAILED dev=%s (%s) after %.0fms: %s: %s",
                self._device_id,
                self._name,
                (time.monotonic() - started) * 1000,
                type(exc).__name__,
                exc,
            )
            raise
        elapsed = (time.monotonic() - started) * 1000
        if before != self._online:
            _LOGGER.warning(
                "TRACE online CHANGED dev=%s (%s) %s -> %s",
                self._device_id,
                self._name,
                before,
                self._online,
            )
        else:
            _LOGGER.debug(
                "TRACE fetch ok dev=%s (%s) online=%s in %.0fms",
                self._device_id,
                self._name,
                self._online,
                elapsed,
            )
        return result

    Device._async_fetch_state = wrapper


def _wrap_refresh_token():
    """Log every token refresh with the before/after lifetime."""
    original = Client._async_refresh_token

    @functools.wraps(original)
    async def wrapper(self: Client):
        before_exp = getattr(self, "_token_exp_time", None)
        before_token = getattr(self, "_token", None)
        started = time.monotonic()
        try:
            return await original(self)
        finally:
            after_exp = getattr(self, "_token_exp_time", None)
            rotated = getattr(self, "_token", None) != before_token
            _LOGGER.debug(
                "TRACE token refresh: rotated=%s ttl %s -> %s in %.0fms",
                rotated,
                f"{before_exp - time.time():+.0f}s" if before_exp else "unknown",
                f"{after_exp - time.time():+.0f}s" if after_exp else "unknown",
                (time.monotonic() - started) * 1000,
            )

    Client._async_refresh_token = wrapper


def _wrap_ws_join_devices():
    """Log updater-websocket connects, exposing reconnect churn.

    The upstream reconnect loop never resets its retry counter, so its backoff
    grows without bound (capped at 300s). The gap logged here is how that
    degradation becomes visible.
    """
    original = Client._async_ws_join_devices

    @functools.wraps(original)
    async def wrapper(self: Client, ws, devices):
        global _ws_connects, _last_ws_connect
        _ws_connects += 1
        now = time.monotonic()
        if _last_ws_connect is None:
            gap = "first connect this session"
        else:
            gap = f"previous connection lasted {now - _last_ws_connect:.0f}s"
        _last_ws_connect = now
        _LOGGER.warning(
            "TRACE updater websocket CONNECTED (#%s this session, %s, %s devices, %s)",
            _ws_connects,
            gap,
            len(devices),
            _ttl(self),
        )
        return await original(self, ws, devices)

    Client._async_ws_join_devices = wrapper


def instrument() -> None:
    """Attach the observational wrappers. Safe to call more than once."""
    global _instrumented
    if _instrumented:
        return

    _wrap_fetch_state()
    _wrap_refresh_token()
    _wrap_ws_join_devices()

    _instrumented = True
    _LOGGER.info("SmartRent diagnostic instrumentation attached")
