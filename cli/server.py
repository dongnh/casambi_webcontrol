import argparse
import asyncio
import getpass
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from CasambiBt import Casambi, discover
from CasambiBt._unit import UnitControlType
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("casambi_webcontrol")

MIRED_MIN = 153
MIRED_MAX = 500
HW_MIN = 0
HW_MAX = 255
MATTER_MAX = 254

# Last non-zero hardware brightness per unit, used by /api/toggle to restore
# previous level instead of blasting 100%.
_last_level: dict[str, int] = {}

casa = Casambi()

# --- Connection health / self-healing (v0.10.0) -----------------------------
# The Casambi link is push-driven: casambi-bt updates unit.state from BLE
# notifications. If the link half-dies (writes still go out but notifications
# stop arriving) unit.state silently freezes — the bridge would keep reporting
# stale values until a full process restart. A background watchdog (reconnects
# when the link drops), a post-command confirm probe (catches a half-open link
# on the next write and forces a resync), and per-unit `online` reporting let
# the bridge notice and self-heal instead.
HEALTH_INTERVAL = 20.0             # watchdog poll cadence (seconds)
RECONNECT_COOLDOWN = 45.0          # min seconds between auto reconnect attempts
CONFIRM_DELAY = 1.5                # seconds to wait for a command's state echo
CONFIRM_TOLERANCE = 3              # hardware-level slop treated as "converged"
UNCONFIRMED_RESYNC_THRESHOLD = 2   # consecutive unconfirmed writes -> force resync

_last_notify_ts: float = 0.0       # monotonic time of the last state notification
_last_reconnect_ts: float = 0.0
_unconfirmed_streak: int = 0
_pending_target: dict[str, int] = {}  # latest commanded hw level per unit id
_reconnect_lock: Optional[asyncio.Lock] = None
_health_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

async def _connect_to_network(password: str) -> bool:
    """Discover and connect. Returns True on success."""
    print("Discovering Casambi networks...")
    devices = await discover()
    if not devices:
        logger.warning("No Casambi networks found.")
        return False
    target = devices[0]
    print("Connecting...")
    await casa.connect(target, password)
    print("Connected.")
    return True


def _note_unit_changed(unit) -> None:
    """casambi-bt unit-changed callback: a state notification just arrived, so
    the push stream is alive. Used for the `seconds_since_last_state_update`
    health signal."""
    global _last_notify_ts
    _last_notify_ts = time.monotonic()


def _note_disconnect() -> None:
    """casambi-bt disconnect callback (sync). Just log — the watchdog polls
    `casa.connected` and performs the actual (awaitable) reconnect."""
    logger.warning("Casambi BLE link dropped (disconnect callback).")


async def _reconnect(reason: str, respect_cooldown: bool = True) -> bool:
    """Guarded reconnect. A fresh connect re-reads unit state from the mesh, which
    is what clears a frozen/half-open cache. Cooldown-limited so a flapping link or
    a burst of unconfirmed writes can't trigger a reconnect storm.

    casambi-bt's `disconnect()` closes its httpx client and does NOT recreate it on
    the next `connect()`, so reconnecting the SAME object fails with "client has
    been closed". We therefore rebuild a fresh `Casambi()` — exactly what a process
    restart does — and re-register the handlers on it.
    """
    global casa, _last_reconnect_ts, _last_notify_ts, _unconfirmed_streak
    if _reconnect_lock is None:
        return False
    async with _reconnect_lock:
        now = time.monotonic()
        if respect_cooldown and now - _last_reconnect_ts < RECONNECT_COOLDOWN:
            logger.info("Reconnect (%s) skipped: within %.0fs cooldown.",
                        reason, RECONNECT_COOLDOWN)
            return False
        _last_reconnect_ts = now
        password = getattr(app.state, "network_password", "") or ""
        logger.warning("Casambi resync: reconnecting (reason: %s)...", reason)

        try:
            await asyncio.wait_for(casa.disconnect(), timeout=8)
        except Exception:
            pass  # best-effort teardown of the old object

        casa = Casambi()
        casa.registerUnitChangedHandler(_note_unit_changed)
        casa.registerDisconnectCallback(_note_disconnect)

        try:
            ok = await asyncio.wait_for(_connect_to_network(password), timeout=45)
        except Exception as e:
            logger.error("Reconnect failed (reason: %s): %s", reason, e)
            return False
        if ok:
            _last_notify_ts = time.monotonic()
            _unconfirmed_streak = 0
            logger.info("Casambi reconnected (reason: %s); %d units.",
                        reason, len(_units()))
        return ok


async def _health_watchdog() -> None:
    """Background task: reconnect whenever the BLE link is down. A half-open link
    (writes OK, notifications dead) is caught separately by _confirm_and_retry on
    the next write."""
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        try:
            if not getattr(casa, "connected", False):
                await _reconnect("link down")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Health watchdog error: %s", e)


async def _confirm_and_retry(uid: str, target_hw: int) -> None:
    """Non-blocking post-command probe. If the unit is online but its state did
    not converge to the command, retry once; on a repeated streak force a resync.
    This is how a half-open link self-heals: the write goes out, no echo comes
    back, the state stays stale, and after enough unconfirmed writes we reconnect
    (which re-reads state from the mesh). The unit is re-resolved from the current
    `casa` so a reconnect mid-probe can't leave us holding a stale object."""
    global _unconfirmed_streak
    try:
        await asyncio.sleep(CONFIRM_DELAY)
        if _pending_target.get(uid) != target_hw:
            return  # superseded by a newer command for this unit
        unit = next((u for u in _units() if _unit_id(u) == uid), None)
        if unit is None:
            return  # gone (disconnected / reconnecting)
        if not bool(getattr(unit, "online", False)):
            return  # unit itself unreachable (e.g. powered off) — not a link fault
        cur = int(getattr(unit.state, "dimmer", 0) or 0)
        if abs(cur - target_hw) <= CONFIRM_TOLERANCE:
            _unconfirmed_streak = 0
            return
        _unconfirmed_streak += 1
        logger.warning("Unconfirmed write for %s (target hw %d, read %d, streak %d).",
                       uid, target_hw, cur, _unconfirmed_streak)
        if _unconfirmed_streak >= UNCONFIRMED_RESYNC_THRESHOLD:
            await _reconnect("repeated unconfirmed writes")
        else:
            await casa.setLevel(unit, target_hw)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("Confirm/retry error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reconnect_lock, _health_task, _last_notify_ts
    _reconnect_lock = asyncio.Lock()
    casa.registerUnitChangedHandler(_note_unit_changed)
    casa.registerDisconnectCallback(_note_disconnect)

    password = getattr(app.state, "network_password", "") or ""
    try:
        await _connect_to_network(password)
        _last_notify_ts = time.monotonic()
    except Exception as e:
        logger.error("Initial Casambi connect failed: %s", e)

    _health_task = asyncio.create_task(_health_watchdog())

    yield

    if _health_task is not None:
        _health_task.cancel()
    for unregister, cb in (
        (casa.unregisterUnitChangedHandler, _note_unit_changed),
        (casa.unregisterDisconnectCallback, _note_disconnect),
    ):
        try:
            unregister(cb)
        except Exception:
            pass
    try:
        await casa.disconnect()
    except Exception as e:
        logger.warning("Casambi disconnect raised: %s", e)


app = FastAPI(title="Casambi Web Controller", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth middleware (constant-time compare)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    api_key = getattr(app.state, "api_key", None)
    if api_key:
        provided = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(provided, api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _units() -> list:
    # casambi-bt's `units` property raises ConnectionStateError (not
    # AttributeError) when the link is down, so getattr's default won't catch it.
    # Return [] instead so callers surface a clean 503 and the watchdog can heal.
    try:
        return list(getattr(casa, "units", None) or [])
    except Exception:
        return []


def _unit_id(unit) -> str:
    if getattr(unit, "uuid", None):
        raw = str(unit.uuid)
    elif getattr(unit, "address", None):
        raw = str(unit.address)
    elif getattr(unit, "deviceId", None) is not None:
        raw = str(unit.deviceId)
    else:
        raw = str(unit.name)
    safe = raw.replace("-", "").replace(":", "").lower()
    return f"cas_{safe}"


def _temp_control(unit):
    utype = getattr(unit, "unitType", None)
    if not utype:
        return None
    return utype.get_control(UnitControlType.TEMPERATURE)


def _supports(unit, control_type: UnitControlType) -> bool:
    utype = getattr(unit, "unitType", None)
    if not utype:
        return False
    return utype.get_control(control_type) is not None


def _kelvin_to_mireds(kelvin: Optional[int]) -> Optional[int]:
    if not kelvin:
        return None
    return max(MIRED_MIN, min(MIRED_MAX, int(1_000_000 / kelvin)))


def _mireds_to_kelvin(mireds: int) -> int:
    mireds = max(MIRED_MIN, min(MIRED_MAX, int(mireds)))
    return int(1_000_000 / mireds)


def _clamp_kelvin_to_unit(unit, kelvin: int) -> int:
    """Clamp Kelvin value to the unit's TEMPERATURE control range."""
    ctrl = _temp_control(unit)
    if ctrl is not None:
        if ctrl.min is not None:
            kelvin = max(ctrl.min, kelvin)
        if ctrl.max is not None:
            kelvin = min(ctrl.max, kelvin)
    return kelvin


def _hardware_to_matter(dimmer_0_255: int) -> int:
    """Map Casambi 0–255 dimmer to Matter 0–254 level (clamped)."""
    if dimmer_0_255 <= 0:
        return 0
    return min(MATTER_MAX, int(dimmer_0_255 / HW_MAX * MATTER_MAX))


def _matter_to_hardware(level_0_254: int) -> int:
    """Map Matter 0–254 level to Casambi 0–255 dimmer (clamped)."""
    level = max(0, min(MATTER_MAX, level_0_254))
    if level == 0:
        return 0
    return min(HW_MAX, int(level / MATTER_MAX * HW_MAX))


def _states_for(unit) -> dict:
    state = unit.state
    states: dict[str, Any] = {}

    if _supports(unit, UnitControlType.ONOFF) or _supports(unit, UnitControlType.DIMMER):
        states["on_off"] = bool(getattr(unit, "is_on", False))

    if _supports(unit, UnitControlType.DIMMER):
        states["brightness_raw"] = _hardware_to_matter(int(state.dimmer or 0))

    if _supports(unit, UnitControlType.TEMPERATURE):
        mireds = _kelvin_to_mireds(state.temperature)
        if mireds is not None:
            states["color_temp_mireds"] = mireds

    return states


def _device_entry(unit) -> dict:
    name = getattr(unit, "name", None) or "Unknown"
    return {
        "id": _unit_id(unit),
        "names": [name],
        "online": bool(getattr(unit, "online", False)),
        "states": _states_for(unit),
    }


def _hardware_type(states: dict) -> str:
    if "color_temp_mireds" in states:
        return "color_temperature_light"
    if "brightness_raw" in states:
        return "dimmable_light"
    if "on_off" in states:
        return "on_off_light"
    return "unknown"


def _capabilities(states: dict) -> list[str]:
    caps = []
    if "on_off" in states:
        caps.append("on_off")
    if "brightness_raw" in states:
        caps.append("brightness")
    if "color_temp_mireds" in states:
        caps.append("color_temperature")
    return caps


def _find_unit(device_id: str):
    units = _units()
    if not units:
        raise HTTPException(status_code=503, detail="Network disconnected")
    for u in units:
        if _unit_id(u) == device_id:
            return u
    raise HTTPException(status_code=404, detail="Device not found")


async def _set_level(unit, hardware_level: int) -> None:
    """Set level, record last non-zero level for toggle restore, and schedule a
    non-blocking confirm/retry probe so a half-open link self-heals."""
    hardware_level = max(HW_MIN, min(HW_MAX, int(hardware_level)))
    uid = _unit_id(unit)
    await casa.setLevel(unit, hardware_level)
    if hardware_level > 0:
        _last_level[uid] = hardware_level
    _pending_target[uid] = hardware_level
    asyncio.create_task(_confirm_and_retry(uid, hardware_level))


async def _get_params(request: Request, payload: Optional[BaseModel], keys: list[str]) -> dict:
    """Merge query params and JSON body into a single dict."""
    qp = dict(request.query_params)
    body: dict = {}
    if payload is not None:
        body = payload.model_dump(exclude_none=True)
    elif request.method == "POST":
        try:
            data = await request.json()
            if isinstance(data, dict):
                body = data
        except Exception:
            body = {}
    return {k: body.get(k, qp.get(k)) for k in keys}


# ---------------------------------------------------------------------------
# Pydantic payloads
# ---------------------------------------------------------------------------

class LevelPayload(BaseModel):
    id: Optional[str] = None
    level: Optional[int] = None


class MiredPayload(BaseModel):
    id: Optional[str] = None
    mireds: Optional[int] = None


class ControlPayload(BaseModel):
    id: Optional[str] = None
    brightness: Optional[float] = None
    temperature: Optional[int] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/devices")
async def get_devices():
    return [_device_entry(u) for u in _units()]


@app.get("/api/lights")
async def get_lights():
    out = []
    for u in _units():
        if not _supports(u, UnitControlType.DIMMER) and not _supports(u, UnitControlType.ONOFF):
            continue
        states = _states_for(u)
        entry = {
            "id": _unit_id(u),
            "names": [u.name],
            "online": bool(getattr(u, "online", False)),
            "on_off": states.get("on_off"),
            "brightness": round((states.get("brightness_raw", 0) or 0) / MATTER_MAX, 2),
        }
        if "color_temp_mireds" in states:
            entry["color_temp_mireds"] = states["color_temp_mireds"]
        out.append(entry)
    return out


@app.get("/api/status")
async def get_status():
    units = _units()
    lights_on = lights_off = 0
    units_online = 0
    for u in units:
        if bool(getattr(u, "online", False)):
            units_online += 1
        states = _states_for(u)
        if "on_off" in states or "brightness_raw" in states:
            if states.get("on_off"):
                lights_on += 1
            else:
                lights_off += 1
    since = round(time.monotonic() - _last_notify_ts, 1) if _last_notify_ts else None
    return {
        "lights_on": lights_on,
        "lights_off": lights_off,
        "sensors_active": 0,
        "logical_bridges": 0,
        "total_devices": len(units),
        "connected": bool(getattr(casa, "connected", False)),
        "units_online": units_online,
        "units_offline": len(units) - units_online,
        "seconds_since_last_state_update": since,
    }


@app.api_route("/api/toggle", methods=["GET", "POST"])
async def toggle_device(request: Request):
    params = await _get_params(request, None, ["id"])
    dev_id = params["id"]
    if not dev_id:
        raise HTTPException(status_code=400, detail="Missing device id")
    unit = _find_unit(dev_id)
    if bool(getattr(unit, "is_on", False)):
        await _set_level(unit, 0)
        return {"status": "success", "id": dev_id, "on_off": False}
    # Restore previous brightness if known, otherwise full power
    restore = _last_level.get(dev_id, HW_MAX)
    await _set_level(unit, restore)
    return {"status": "success", "id": dev_id, "on_off": True}


@app.api_route("/api/level", methods=["GET", "POST"])
async def level_api(request: Request, payload: Optional[LevelPayload] = None):
    params = await _get_params(request, payload, ["id", "level"])
    if not params["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")

    unit = _find_unit(params["id"])

    if params["level"] is None:
        return {
            "id": params["id"],
            "level": _hardware_to_matter(int(unit.state.dimmer or 0)),
        }

    level = max(0, min(MATTER_MAX, int(params["level"])))
    await _set_level(unit, _matter_to_hardware(level))
    return {"status": "success", "id": params["id"], "level": level, "type": "physical"}


@app.api_route("/api/mired", methods=["GET", "POST"])
async def mired_api(request: Request, payload: Optional[MiredPayload] = None):
    params = await _get_params(request, payload, ["id", "mireds"])
    if not params["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")

    unit = _find_unit(params["id"])
    if not _supports(unit, UnitControlType.TEMPERATURE):
        raise HTTPException(status_code=400, detail="Color temperature unsupported")

    if params["mireds"] is None:
        mireds = _kelvin_to_mireds(unit.state.temperature)
        if mireds is None:
            raise HTTPException(status_code=404, detail="No color temperature reading")
        return {"id": params["id"], "mireds": mireds}

    kelvin = _clamp_kelvin_to_unit(unit, _mireds_to_kelvin(int(params["mireds"])))
    await casa.setTemperature(unit, kelvin)
    return {
        "status": "success",
        "id": params["id"],
        "mireds": int(params["mireds"]),
        "type": "physical",
    }


@app.api_route("/api/set", methods=["GET", "POST"])
async def set_device(request: Request, payload: Optional[ControlPayload] = None):
    params = await _get_params(request, payload, ["id", "brightness", "temperature"])
    if not params["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")

    if params["brightness"] is None and params["temperature"] is None:
        raise HTTPException(
            status_code=400, detail="Must provide brightness and/or temperature"
        )

    unit = _find_unit(params["id"])

    if params["brightness"] is not None:
        brightness = max(0.0, min(1.0, float(params["brightness"])))
        hardware = 0 if brightness == 0.0 else max(1, int(brightness * HW_MAX))
        await _set_level(unit, hardware)

    if params["temperature"] is not None and int(params["temperature"]) > 0:
        if _supports(unit, UnitControlType.TEMPERATURE):
            kelvin = _clamp_kelvin_to_unit(unit, int(params["temperature"]))
            await casa.setTemperature(unit, kelvin)

    return {"status": "success", "id": params["id"], "type": "physical"}


@app.api_route("/api/refresh", methods=["GET", "POST"])
async def refresh(request: Request):
    """Reconnect the BLE link.

    With `?force=1` (or JSON `{"force": true}`) it drops and reconnects even when
    units are already present — use this to resync a frozen/half-open cache
    without restarting the process (bypasses the reconnect cooldown). Without
    force it only acts when the link is down; state is otherwise push-driven.
    """
    params = await _get_params(request, None, ["force"])
    force = str(params.get("force") or "").strip().lower() in ("1", "true", "yes", "on")

    if force:
        ok = await _reconnect("manual force", respect_cooldown=False)
        if not ok:
            raise HTTPException(status_code=503, detail="Forced reconnect failed")
        return {"status": "success", "message": "Forced resync (reconnected)",
                "units": len(_units())}

    if _units():
        return {"status": "success", "message": "Connected; state is push-driven"}

    password = getattr(app.state, "network_password", "") or ""
    try:
        ok = await _connect_to_network(password)
    except Exception as e:
        logger.error("Reconnect failed: %s", e)
        raise HTTPException(status_code=503, detail=f"Reconnect failed: {e}")

    if not ok:
        raise HTTPException(status_code=503, detail="No Casambi networks found")
    return {"status": "success", "message": "Reconnected"}


@app.get("/api/metadata")
async def get_bridge_metadata(request: Request):
    host = request.url.hostname or "127.0.0.1"
    port = request.url.port or getattr(app.state, "port", 8000)

    devices_metadata = []
    for u in _units():
        states = _states_for(u)
        hw_type = _hardware_type(states)
        if hw_type == "unknown":
            continue
        name = getattr(u, "name", None) or "Unknown"
        devices_metadata.append({
            "id": _unit_id(u),
            "name": name,
            "names": [name],
            "hardware_type": hw_type,
            "capabilities": _capabilities(states),
            "online": bool(getattr(u, "online", False)),
            "states": states,
        })

    return {
        "bridge": {
            "id": "casambi_bridge_http",
            "type": "lighting_controller",
            "network_host": host,
            "network_port": port,
            "api_version": "2",
        },
        "devices": devices_metadata,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Casambi Web Controller")
    parser.add_argument("--port", type=int, default=8000, help="Web server port")
    parser.add_argument(
        "--host", type=str, default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 to expose on LAN)",
    )
    parser.add_argument(
        "--api-key", type=str, default=os.environ.get("CASAMBI_SRV_KEY"),
        help="Require X-API-Key header (or set CASAMBI_SRV_KEY env var)",
    )
    parser.add_argument(
        "--password", type=str, default=None,
        help="Casambi network password. Prefer CASAMBI_NETWORK_PWD env var or "
             "interactive prompt — CLI args are visible in `ps`.",
    )
    args = parser.parse_args()

    if args.host != "127.0.0.1" and not args.api_key:
        logger.warning(
            "Server bound to %s without --api-key. Anyone on the network can control "
            "your lights. Set CASAMBI_SRV_KEY or pass --api-key.",
            args.host,
        )

    # Resolution order: --password > $CASAMBI_NETWORK_PWD > interactive prompt.
    # The env var is scrubbed and the password is held only on app.state, so it
    # never leaks via /proc/PID/environ.
    password = args.password or os.environ.pop("CASAMBI_NETWORK_PWD", "")
    if args.password:
        logger.warning(
            "Password passed via --password is visible to other users in `ps`. "
            "Prefer CASAMBI_NETWORK_PWD env var or the interactive prompt."
        )
    if not password:
        if os.isatty(0):
            password = getpass.getpass("Enter Casambi network password: ")
        else:
            # Headless (service / daemon): no TTY to prompt on. Fail with a clear
            # message instead of crashing on getpass's EOFError.
            parser.error(
                "no Casambi network password and no interactive terminal; set the "
                "CASAMBI_NETWORK_PWD environment variable (or pass --password) when "
                "running headless / as a service."
            )

    app.state.api_key = args.api_key
    app.state.network_password = password
    app.state.port = args.port

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
