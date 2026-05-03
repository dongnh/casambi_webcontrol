import argparse
import getpass
import hmac
import logging
import os
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    password = getattr(app.state, "network_password", "") or ""
    try:
        await _connect_to_network(password)
    except Exception as e:
        logger.error("Initial Casambi connect failed: %s", e)

    yield

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
    return list(getattr(casa, "units", None) or [])


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
    """Wrapper that records last non-zero level for toggle restore."""
    hardware_level = max(HW_MIN, min(HW_MAX, int(hardware_level)))
    await casa.setLevel(unit, hardware_level)
    if hardware_level > 0:
        _last_level[_unit_id(unit)] = hardware_level


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
    for u in units:
        states = _states_for(u)
        if "on_off" in states or "brightness_raw" in states:
            if states.get("on_off"):
                lights_on += 1
            else:
                lights_off += 1
    return {
        "lights_on": lights_on,
        "lights_off": lights_off,
        "sensors_active": 0,
        "logical_bridges": 0,
        "total_devices": len(units),
    }


@app.get("/api/toggle")
async def toggle_device(id: str):
    unit = _find_unit(id)
    if bool(getattr(unit, "is_on", False)):
        await _set_level(unit, 0)
        return {"status": "success", "id": id, "on_off": False}
    # Restore previous brightness if known, otherwise full power
    restore = _last_level.get(id, HW_MAX)
    await _set_level(unit, restore)
    return {"status": "success", "id": id, "on_off": True}


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


@app.get("/api/refresh")
async def refresh():
    """Attempt reconnect if BLE link is down. State updates are push-driven
    (BLE notifications) so no explicit poll is needed when connected."""
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
        password = getpass.getpass("Enter Casambi network password: ")

    app.state.api_key = args.api_key
    app.state.network_password = password
    app.state.port = args.port

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
