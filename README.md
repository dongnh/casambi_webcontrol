# Casambi Web Controller

Local Casambi BLE lighting control exposed as an HTTP API. Compatible with [matter_webcontrol](https://github.com/dongnh/matter_webcontrol) v0.25.0+ federation, so a Casambi network can be merged into a Matter fabric as a logical bridge.

## Architecture

FastAPI middleware over the [casambi-bt](https://github.com/lampelina/casambi-bt) library. The server discovers a Casambi network over BLE, authenticates with the network password, and exposes each unit through a REST API that matches `matter_webcontrol`'s federation contract — so federation peers call `/api/devices`, `/api/level`, `/api/mired`, `/api/set` directly without embedded scripts (no RCE surface).

```
┌──────────────┐    HTTP+API key    ┌──────────────────┐    BLE    ┌──────────┐
│ matter-srv   │ ─────────────────▶ │ casambi-srv      │ ────────▶ │ Casambi  │
│ (Matter      │                    │ (this project)   │           │ network  │
│  fabric)     │ ◀─── /api/devices  │ FastAPI + bleak  │ ◀──notif─ │          │
└──────────────┘                    └──────────────────┘           └──────────┘
```

## Limitations

- Single Casambi network per server instance (uses `devices[0]` from discovery).
- Lighting units only — sensor / occupancy not exposed.
- BLE depends on host OS permissions (on macOS, grant Bluetooth to the parent process via System Settings → Privacy & Security → Bluetooth).

## Install

```bash
pip install -e .            # from source
# or
pip install casambi-web-controller   # from PyPI
```

## Run

```bash
casambi-srv --port 8000 --api-key <secret>
# or via env:
CASAMBI_NETWORK_PWD=<network-password> CASAMBI_SRV_KEY=<secret> casambi-srv --port 8000
```

CLI options:

| Flag | Default | Description |
|---|---|---|
| `--port` | `8000` | Web server port |
| `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on LAN (warns if no API key) |
| `--api-key` | `$CASAMBI_SRV_KEY` | Require `X-API-Key` header on all requests |

The Casambi network password is read from `$CASAMBI_NETWORK_PWD` if set; otherwise prompted interactively.

## Federation with matter_webcontrol

Once both servers are running, register this server as a logical bridge on the Matter side:

```
GET http://<matter-host>:8080/api/bridge?ip=<casambi-host>&port=8000&api_key=<secret>
```

After this, every Casambi device appears under matter's `/api/devices` with id `cas_*` and is controllable through any matter endpoint (`/api/level`, `/api/set`, `/api/toggle`, etc.). Matter routes calls back through this server's REST API.

## API endpoints

All endpoints require `X-API-Key: <secret>` if `--api-key` was set.

### `GET /api/devices`

Federation device list. Each entry: `{id, names, states}`.

```json
[
  {
    "id": "cas_d8dccdc7a8bc4f079f4e76d49bf9c7bc",
    "names": ["Sofa and Painting"],
    "states": {"on_off": false, "brightness_raw": 0}
  }
]
```

`states` keys (only emitted when the unit type supports the control):

- `on_off` (bool)
- `brightness_raw` (int 0–254, Matter scale)
- `color_temp_mireds` (int 153–500)

### `GET /api/lights`

Light-only view with normalized fields.

```json
[{"id": "cas_…", "names": ["…"], "on_off": true, "brightness": 0.78}]
```

### `GET /api/status`

Summary counts.

```json
{"lights_on": 1, "lights_off": 7, "sensors_active": 0, "logical_bridges": 0, "total_devices": 8}
```

### `GET|POST /api/level`

Read or write Matter-scale level (0–254).

| Param | Type | Required | Notes |
|---|---|---|---|
| `id` | str | yes | Canonical device id (`cas_*`) |
| `level` | int | no | If absent, returns current level. If present (0–254), sets it |

Body for POST: `{"id": "cas_…", "level": 200}`. Read-back response: `{id, level}`. Write response: `{status, id, level, type}`.

### `GET|POST /api/mired`

Color temperature in mireds (153–500). Auto-clamped to per-unit Kelvin range. Returns `400` for units without temperature control.

Body for POST: `{"id": "cas_…", "mireds": 250}`.

### `GET|POST /api/set`

Multi-attribute write. At least one of `brightness` / `temperature` is required (else `400`).

| Param | Type | Range |
|---|---|---|
| `id` | str | required |
| `brightness` | float | 0.0–1.0 |
| `temperature` | int | Kelvin |

Body for POST: `{"id": "cas_…", "brightness": 0.5, "temperature": 4000}`.

### `GET /api/toggle?id=`

Flip on/off. When turning on, restores the last non-zero level (cached in-memory) instead of jumping to 100%.

### `GET /api/refresh`

If BLE is connected, no-op (state is push-driven via Casambi notifications). If disconnected, attempts a reconnect using the cached network password.

### `GET /api/metadata`

Declarative bridge metadata for federation discovery. **No embedded Python scripts** — peers consume the device list via `/api/devices` and call control endpoints directly.

```json
{
  "bridge": {
    "id": "casambi_bridge_http",
    "type": "lighting_controller",
    "network_host": "192.168.1.220",
    "network_port": 8000,
    "api_version": "2"
  },
  "devices": [
    {
      "id": "cas_d8dccdc7a8bc4f079f4e76d49bf9c7bc",
      "name": "Sofa and Painting",
      "names": ["Sofa and Painting"],
      "hardware_type": "dimmable_light",
      "capabilities": ["on_off", "brightness"],
      "states": {"on_off": false, "brightness_raw": 0}
    }
  ]
}
```

## Security notes

- `X-API-Key` comparison is constant-time (`hmac.compare_digest`) to resist timing attacks.
- The Casambi network password is held on `app.state` and **not** written into `os.environ` (no leak via `/proc/PID/environ` on Linux).
- Default bind is `127.0.0.1`. Binding to `0.0.0.0` without `--api-key` logs a warning.
- `/api/metadata` no longer emits executable Python; the previous `events.{name}.script` shape was removed in v0.9.0 to match matter_webcontrol v0.25.0 (closes a federation RCE risk).

## Compatibility matrix

| casambi_webcontrol | matter_webcontrol |
|---|---|
| **0.9.x** | **0.25.x +** (federation v2: REST + auth) |
| ≤ 0.6.x | ≤ 0.22.x (legacy embedded scripts) |
