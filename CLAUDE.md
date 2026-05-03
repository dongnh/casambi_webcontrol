# CLAUDE.md

Codebase guide for AI agents.

## What this is

A single-file FastAPI server that bridges a Casambi BLE lighting network to an HTTP REST API. The API contract matches [matter_webcontrol](https://github.com/dongnh/matter_webcontrol) v0.25.0's federation v2 protocol so this server can be registered as a "logical bridge" inside a Matter fabric.

## Layout

```
cli/server.py     # everything: lifespan, auth, helpers, endpoints, entry point
pyproject.toml    # console script: casambi-srv = cli.server:main
README.md         # user docs
CLAUDE.md         # this file
```

There is intentionally no `core.py` / `mcp_server.py` split (unlike matter_webcontrol). The server is small enough to keep in one module.

## Key concepts

- **Module-level `casa = Casambi()`** — single global connection. Lifespan owns it.
- **Device ID format `cas_<uuid|address|deviceId>`** — derived from `_unit_id(unit)`. Stable across restarts because it hashes the Casambi UUID. Used as the canonical id in every endpoint.
- **Two scales for brightness:**
  - Casambi hardware: `0–255` (`unit.state.dimmer`)
  - Matter / API contract: `0–254` (`brightness_raw`, `level`)
  - Conversion via `_hardware_to_matter()` / `_matter_to_hardware()`. Always convert at the API boundary.
- **Mireds clamp `[153, 500]`** then convert to Kelvin then re-clamp to the unit's `UnitControl.min/max` from `casambi-bt`.
- **Toggle restores last level:** `_last_level: dict[str, int]` caches the most recent non-zero hardware level per device, so toggling on doesn't blast 100%.

## API contract (matter_webcontrol v0.25.0 federation)

`LogicalBridgeClient` in matter_webcontrol calls these endpoints:

| Method | Path | Body / query | Used for |
|---|---|---|---|
| GET | `/api/devices` | — | `refresh()` device list |
| POST | `/api/level` | `{id, level}` | `set_level()` |
| POST | `/api/mired` | `{id, mireds}` | `set_mired()` |
| POST | `/api/set` | `{id, brightness}` | `set_brightness()` |

If you change response shapes for these four, federation breaks. The shapes are also enforced by `/tmp/test_casambi_server.py` (round-trip test against the real `LogicalBridgeClient`).

`/api/metadata` returns `bridge.api_version: "2"` and **must not** emit `events.{name}.script` blobs (the v1 shape was an RCE risk; matter peers no longer execute them).

## Security model

- `auth_middleware` checks `X-API-Key` with `hmac.compare_digest` on every request when `app.state.api_key` is set.
- Default bind `127.0.0.1`. Warns if `--host 0.0.0.0` without `--api-key`.
- Casambi network password lives only on `app.state.network_password`. Never write it into `os.environ` — `main()` calls `os.environ.pop()` to scrub.

## Running locally

```bash
# Live, requires BLE access (macOS: grant Bluetooth to Claude Code's helper bundle, not /Applications/Claude.app)
CASAMBI_NETWORK_PWD=<pwd> casambi-srv --port 8000 --api-key <key>
```

## Testing without hardware

`/tmp/test_casambi_server.py` (kept outside the repo) builds fake `Unit` objects with mocked `unitType`/`state`, monkeypatches `cli.server.casa`, and exercises every endpoint via `TestClient`. It also performs a full round-trip via the real `LogicalBridgeClient` from `/Volumes/Extra/GitHub/matter_webcontrol/cli/logic_bridge.py` to catch federation contract drift.

To recreate:

```python
# Build fake unit with UnitControl entries for ONOFF/DIMMER/(TEMPERATURE)
from CasambiBt._unit import UnitControl, UnitControlType, UnitState, UnitType
# ...mock unit with .uuid, .name, .unitType, .state, .is_on...
import cli.server as srv
srv.casa.units = [fake_unit_1, fake_unit_2]
srv.casa.setLevel = AsyncMock()
srv.casa.setTemperature = AsyncMock()
client = TestClient(srv.app)
```

## Common pitfalls

- **Adding a new state key:** update `_states_for`, `_hardware_type`, `_capabilities` together — they read from the same `states` dict.
- **Changing scales:** update both `_hardware_to_matter` and `_matter_to_hardware`; verify by setting then reading back. Off-by-one is normal (200 hw → 199 matter due to floor).
- **`casa.units` is `None` before connect:** every endpoint that touches it uses `_units()` or `_find_unit()`, which raise 503 if no connection. Don't bypass.
- **macOS Bluetooth TCC:** Claude Code's helper bundle (`~/Library/Application Support/Claude/claude-code/<ver>/claude.app`, identifier `com.anthropic.claude-code`) is the responsible binary, **not** `/Applications/Claude.app`. Grant Bluetooth to the helper.

## Compatibility window

| This version | matter_webcontrol |
|---|---|
| 0.9.x | 0.25.x+ (federation v2) |
| ≤ 0.6.x | ≤ 0.22.x (legacy v1 with embedded scripts; no longer supported) |
