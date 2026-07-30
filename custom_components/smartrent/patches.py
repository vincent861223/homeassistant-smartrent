"""Runtime fixes for the bundled ``smartrent-py`` client.

Upstream 0.5.2 sends a command by opening a websocket, firing ``phx_join`` and
``update_attributes`` back to back, then closing the socket immediately --
without ever reading a Phoenix reply. A command the hub never processed is
therefore indistinguishable from one that worked, so Home Assistant reports
success while nothing happened.

It also only refreshes the access token reactively. The token lives ~12-14
minutes but the REST poll that discovers expiry runs every 600s, so
``client._token`` is routinely stale by the time a command is issued, and the
single retry's ``_async_refresh_token()`` is a no-op whenever ``_token_exp_time``
is still in the future ("Token not expired. Not refreshing.").

These patches are applied from ``async_setup_entry`` so they survive container
image updates that replace ``site-packages``.
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets
from homeassistant.util.ssl import get_default_context
from smartrent.lock import DoorLock
from smartrent.utils import (
    COMMAND_PAYLOAD,
    SMARTRENT_WEBSOCKET_URI,
    Client,
    InvalidAuthError,
    SmartRentError,
)
from websockets.exceptions import ConnectionClosed, InvalidStatus

_LOGGER = logging.getLogger(__name__)

# Phoenix always replies to phx_join, so a missing join ack is a hard failure.
JOIN_ACK_TIMEOUT = 8.0
# update_attributes is not guaranteed to generate a reply, so a missing one is
# not an error -- we only listen briefly for an explicit rejection.
COMMAND_GRACE_SECONDS = 1.5
COMMAND_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
# Refresh this far ahead of nominal expiry rather than waiting for a 403.
TOKEN_SKEW_SECONDS = 120

# Resolved at import time -- Home Assistant imports integrations in an executor
# thread, so this stays off the event loop. Without it websockets falls back to
# ssl.create_default_context() inside the loop on every command, which HA flags
# as a blocking call.
_SSL_CONTEXT = get_default_context()

_PATCHED = False


class SmartRentCommandError(SmartRentError):
    """A command could not be delivered to the hub."""


async def _async_await_reply(
    websocket, topic: str, timeout: float
) -> Optional[dict[str, Any]]:
    """Read messages until a ``phx_reply`` for ``topic`` arrives.

    Returns the reply payload dict, or ``None`` if nothing arrived in time.
    Raises ``SmartRentCommandError`` if the channel reports an error.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None

        try:
            raw = await asyncio.wait_for(websocket.recv(), remaining)
        except (asyncio.TimeoutError, TimeoutError):
            return None

        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            continue

        if not isinstance(message, list) or len(message) < 5:
            continue

        _join_ref, _ref, msg_topic, event, payload = message[:5]
        if msg_topic != topic:
            continue

        if event == "phx_reply":
            return payload if isinstance(payload, dict) else {}

        if event in ("phx_error", "phx_close"):
            raise SmartRentCommandError(f"channel {event} on {topic}: {payload}")


def _ms_since(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _ttl(client: Client) -> str:
    """Remaining access-token lifetime, for correlating failures with expiry."""
    exp = getattr(client, "_token_exp_time", None)
    return f"ttl={exp - time.time():+.0f}s" if exp else "ttl=unknown"


async def _async_force_refresh(client: Client) -> None:
    """Refresh the token, bypassing the "not expired yet" early return."""
    client._token_exp_time = None
    await client._async_refresh_token()


async def _async_refresh_if_stale(client: Client) -> None:
    """Refresh proactively so commands do not race token expiry."""
    exp = client._token_exp_time
    if not client._token or not exp or exp <= time.time() + TOKEN_SKEW_SECONDS:
        await _async_force_refresh(client)


async def _async_send_payload(self: Client, device, payload: str) -> None:
    """Send ``payload`` and confirm the hub's channel accepted it.

    Replaces ``Client._async_send_payload``.
    """
    topic = f"devices:{device._device_id}"
    _LOGGER.info("sending payload %s", payload)

    uri = SMARTRENT_WEBSOCKET_URI.format(self._token)
    headers = {"Authorization": f"Bearer {self._token}"}

    # websockets rejects an ssl argument on a ws:// URI, so only pass it for wss.
    tls_kwargs = {"ssl": _SSL_CONTEXT} if uri.startswith("wss://") else {}

    opened = time.monotonic()
    async with websockets.connect(
        uri,
        additional_headers=headers,
        ping_interval=15,
        ping_timeout=10,
        close_timeout=5,
        **tls_kwargs,
    ) as websocket:
        _LOGGER.debug("TRACE handshake ok topic=%s in %.0fms", topic, _ms_since(opened))
        await self._async_ws_joiner(websocket, device)

        joined = time.monotonic()
        join_reply = await _async_await_reply(websocket, topic, JOIN_ACK_TIMEOUT)
        if join_reply is None:
            raise SmartRentCommandError(
                f"no join ack for {topic} within {JOIN_ACK_TIMEOUT:.0f}s"
            )
        if join_reply.get("status") != "ok":
            raise SmartRentCommandError(f"join rejected for {topic}: {join_reply}")
        _LOGGER.debug("TRACE join ack ok topic=%s in %.0fms", topic, _ms_since(joined))

        await websocket.send(payload)

        sent = time.monotonic()
        reply = await _async_await_reply(websocket, topic, COMMAND_GRACE_SECONDS)
        if reply is not None and reply.get("status") != "ok":
            raise SmartRentCommandError(f"command rejected on {topic}: {reply}")
        _LOGGER.debug(
            "TRACE command accepted topic=%s reply=%s in %.0fms",
            topic,
            "none (provisional)" if reply is None else reply.get("status"),
            _ms_since(sent),
        )


_RETRYABLE = (
    InvalidStatus,
    InvalidAuthError,
    SmartRentCommandError,
    ConnectionClosed,
    OSError,
)


async def _async_send_command(
    self: Client, device, attribute_name: str, value: str
) -> None:
    """Send a command, refreshing the token up front and on failure.

    Replaces ``Client._async_send_command``.
    """
    payload = COMMAND_PAYLOAD.format(
        attribute_name=attribute_name, value=value, device_id=device._device_id
    )
    label = f"{attribute_name}={value} dev={device._device_id} ({device._name})"
    started = time.monotonic()

    _LOGGER.debug("TRACE command START %s %s", label, _ttl(self))
    await _async_refresh_if_stale(self)

    last_exc: Optional[Exception] = None
    for attempt in range(COMMAND_ATTEMPTS):
        try:
            await self._async_send_payload(device, payload)
            _LOGGER.debug(
                "TRACE command DELIVERED %s on attempt %s/%s in %.0fms %s",
                label,
                attempt + 1,
                COMMAND_ATTEMPTS,
                _ms_since(started),
                _ttl(self),
            )
            return
        except _RETRYABLE as exc:
            last_exc = exc
            _LOGGER.warning(
                "TRACE command FAILED %s attempt %s/%s after %.0fms %s: %s: %s",
                label,
                attempt + 1,
                COMMAND_ATTEMPTS,
                _ms_since(started),
                _ttl(self),
                type(exc).__name__,
                exc,
            )
            if attempt + 1 < COMMAND_ATTEMPTS:
                await _async_force_refresh(self)
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    _LOGGER.error(
        "TRACE command GAVE UP %s after %s attempts / %.0fms %s -- last error %s: %s",
        label,
        COMMAND_ATTEMPTS,
        _ms_since(started),
        _ttl(self),
        type(last_exc).__name__,
        last_exc,
    )
    raise SmartRentCommandError(
        f"could not send {attribute_name}={value} to {device._name} "
        f"after {COMMAND_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


async def _async_set_locked(self: DoorLock, value: bool) -> None:
    """Send the lock command without optimistically claiming it worked.

    Replaces ``DoorLock.async_set_locked``, which set ``self._locked`` before
    sending and never reverted it on failure. Real state now only comes from
    the websocket echo or a REST fetch.
    """
    await self._client._async_send_command(
        self, attribute_name="locked", value=str(value).lower()
    )


def apply_patches() -> None:
    """Patch the installed smartrent client. Safe to call more than once."""
    global _PATCHED
    if _PATCHED:
        return

    Client._async_send_payload = _async_send_payload
    Client._async_send_command = _async_send_command
    DoorLock.async_set_locked = _async_set_locked

    _PATCHED = True
    _LOGGER.info("Applied smartrent-py command-acknowledgement patches")
