# Fork notes

This is a maintenance fork of
[ZacheryThomas/homeassistant-smartrent](https://github.com/ZacheryThomas/homeassistant-smartrent),
branched from **v0.5.5**. It exists to fix an intermittent failure where SmartRent
stops accepting commands and the integration reports success anyway.

`main` here tracks upstream plus the commits below. To pull upstream changes in:

```sh
git fetch upstream
git merge upstream/main      # or: git rebase upstream/main
```

## What diverges from upstream

### 1. Commands are verified instead of fired and forgotten

Upstream `smartrent-py` `Client._async_send_payload` opens a websocket, sends
`phx_join` and `update_attributes` back to back, then closes the socket
immediately — without ever reading a Phoenix reply. A command the hub never
processed is indistinguishable from one that worked.

`custom_components/smartrent/patches.py` replaces it with a version that awaits
the `phx_join` reply and requires `status == "ok"` before sending. Phoenix always
acknowledges `phx_join`, so a missing ack is a hard failure. `update_attributes`
is *not* guaranteed to generate a reply, so a missing one is treated as
provisional success and only an explicit rejection fails.

### 2. The access token no longer races expiry

The token lives **900s**, but upstream only refreshes it reactively off a **600s**
REST poll. That leaves a 300s window every cycle where the token is dead and
nothing knows it — 25% of wall-clock time. Commands issued in that window fail
their websocket handshake.

`patches.py` refreshes proactively (120s skew) and force-refreshes between
retries, bypassing the `_token_exp_time` guard that logs
`"Token not expired. Not refreshing."` and otherwise makes the retry replay the
same rejected token.

### 3. Entities report availability

`sensor.py` already gated `available` on `device.get_online()`, but `lock.py` and
`climate.py` had no `available` property, so during a hub outage they kept showing
stale state while the sensors correctly went unavailable. Both now derive
availability from `get_online()`.

### 4. Lock changes are confirmed

`lock.py` waits up to 15s for the hub to echo the new state over the updater
websocket (~5s typical), falls back to an explicit REST read if no echo arrives
(the updater socket may be mid-reconnect), and raises `HomeAssistantError` only
when neither source agrees. It also reports `is_locking` / `is_unlocking` during
the wait.

`DoorLock.async_set_locked` no longer sets `_locked` optimistically before
sending.

### 5. Optional tracing

`custom_components/smartrent/tracing.py` adds read-only instrumentation —
per-command latencies and token lifetime, token rotations, online/offline
transitions, and updater-websocket reconnect churn — mirrored to a rotating
`smartrent_diagnostic.log` in the config directory. Enable with:

```yaml
logger:
  logs:
    smartrent: debug
    custom_components.smartrent: debug
```

Named `tracing.py` rather than `diagnostics.py` on purpose: Home Assistant
discovers `<integration>/diagnostics.py` for every integration via
`async_process_integration_platforms` and registers
`getattr(platform, "async_get_config_entry_diagnostics", None)` from it, so that
filename shadows the "Download diagnostics" platform.

In practice the impact today is nil — upstream does not implement
`async_get_config_entry_diagnostics`, so Download Diagnostics is already
unavailable for this integration either way. The rename is hygiene: it keeps the
name free for a real diagnostics platform, and avoids this module being imported
during platform discovery for unrelated reasons.

### 6. Confirm timeout shortened; hub-level status logged on failure

A user reported SmartRent going intermittently unresponsive after a few
lock toggles, including through the *official SmartRent app* -- not just
this integration. That detail matters: the app is a separate, independently
-coded client hitting the same cloud API, so if it fails at the same time
this integration does, the cause is very unlikely to be something in this
codebase's own connection handling.

Two theories were tested live against the real account before writing any
fix:

1. **Does a command's short-lived connection evict the persistent updater
   websocket's channel subscription** (both join the same `devices:{id}`
   topic)? Four rapid real toggles against an isolated listener did not
   disrupt it at all -- refuted.
2. **Does the persistent connection go silently stale from elapsed
   connection age**, given it never sends anything after its initial join
   (a real gap versus Phoenix's own JS client, which heartbeats every 30s)?
   A 5.5-minute sustained probe, well past the ~3.5min mark where this was
   observed in production, did not reproduce it either.

Neither reproduced a stuck state. Combined with the official app also
failing, this points to an intermittent condition in SmartRent's own
infrastructure that a client-side patch cannot fix or prevent.

Given that, `CONFIRM_TIMEOUT` (`lock.py`) was cut from 15s to 6s -- every
successful echo observed, in testing and production, arrived within
0-4.5s, and the REST fallback itself resolves in ~100-200ms once
triggered, so 15s of silence before falling back was pure unnecessary
delay. And `patches.py` gained `async_log_hub_status()`: `GET /hubs`
reports the physical hub's own online/connection status, distinct from
each device's own online flag, and nothing in this codebase checked it
before. It's now logged at WARNING on every confirm timeout -- if this
recurs, the diagnostic log will show whether the hub itself was reported
unreachable from SmartRent's cloud (explaining the app failing too) or
not, which is the one signal that was missing to tell these failure modes
apart.

### 7. Reload hygiene

`entry.add_update_listener` is registered via `async_on_unload` so repeated
reloads stop stacking listeners; unload uses `async_unload_platforms`; and
`async_reload_entry` delegates to `hass.config_entries.async_reload`. This
matters because reloading the config entry (~1s) is the recommended recovery,
in place of power-cycling the hub (1–3 minutes).

## Why the library fixes live in the integration

The `_async_send_payload` and `async_set_locked` fixes properly belong in
[smartrent-py](https://github.com/ZacheryThomas/smartrent-py). They are applied
here as monkeypatches from `patches.py` instead, so that:

- everything installs through HACS with no Python package to publish, and
- the fixes survive Home Assistant container image updates that replace
  `site-packages`.

If they are ever accepted upstream in the library, `patches.py` can be deleted
and `manifest.json` bumped to the fixed `smartrent-py` release.

## How this gets installed

Installed via HACS as a custom repository (`vincent861223/homeassistant-smartrent`,
category `integration`), *not* through HACS's default store — the fork isn't and
won't be submitted there, since it would then compete with upstream's own listing
for the same `smartrent` domain.

`hacs.json` sets `zip_release: true` with `filename: smartrent.zip`, so HACS
installs from a `smartrent.zip` release asset, not directly from the repository
tree. There is currently no working CI to build that asset automatically —
`.github/workflows/release.yml` would do it, but GitHub disables `on: push`
Actions runs on a fork until the owner clicks through the banner on the fork's
Actions tab, and even then the `gh` OAuth token used for this fork lacks the
`workflow` scope needed to push tags on commits touching `.github/workflows/*`.
So releases are built and published by hand:

```sh
# 1. Bump custom_components/smartrent/manifest.json "version" (informational only —
#    HACS compares against the release tag below, not this field).
# 2. Commit, then tag and push:
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main vX.Y.Z          # NOT `--tags`: that also tries to push every
                                      # old upstream tag and fails on the ones that
                                      # touch .github/workflows/* without the
                                      # `workflow` OAuth scope

# 3. Build the zip in a clean ubuntu-latest container, matching what
#    release.yml would have produced:
docker run --rm -v "$PWD:/repo" -w /repo/custom_components/smartrent ubuntu:latest \
  bash -c 'apt-get -qq update && apt-get -qq install -y zip && rm -rf __pycache__ && zip -q smartrent.zip -r ./'

# 4. Publish the release with that zip attached as an asset named smartrent.zip:
gh release create vX.Y.Z custom_components/smartrent/smartrent.zip \
  --repo vincent861223/homeassistant-smartrent --title "vX.Y.Z" --notes "..."
```

Then in Home Assistant: HACS → the SmartRent card shows an update → Update →
restart Home Assistant to activate (integrations need a restart; HACS says so in
its own note after install).

To install from scratch on a new Home Assistant instance: HACS → Integrations →
⋮ → Custom repositories → add `vincent861223/homeassistant-smartrent` as an
Integration → search "SmartRent" → Download → restart → add the integration via
Settings → Devices & Services as usual.

Every commit is checked against the repo's actual `.pre-commit-config.yaml`
before pushing (black, isort, flake8, mypy). The pinned hook versions
(black 23.1.0, flake8 6.0.0) fail outright on Python 3.12+ (`ast.Str` was
removed), which is also why the workflow above runs in a `python:3.11-slim`
container rather than on the host:

```sh
docker run --rm -v "$PWD:/src" -w /src python:3.11-slim sh -c '
  pip install -q pre-commit && git config --global --add safe.directory /src
  pre-commit run --all-files'
```

## Known upstream issues not fixed here

- `Client._async_update_state_via_ws` initialises `retries = 0` outside its
  `while True` loop and never resets it on a successful connect, so
  `wait_time = 1.25 ** retries` grows toward its 300s cap for the life of the
  process. Fixing it means rewriting the reconnect loop; `tracing.py` makes the
  degradation observable instead.
- The same loop builds an SSL context inside the event loop on every reconnect.
  `patches.py` fixes this for the command path only.
- `Client._ws` is only ever assigned `None`, so the re-join branch in
  `_subscribe_device_to_updater` is dead code.
