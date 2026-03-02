import asyncio
import os
import getpass
from contextlib import asynccontextmanager
import urllib.parse
from fastapi import FastAPI, HTTPException, Request
from CasambiBt import Casambi, discover
import uvicorn

casa = Casambi()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Discovering Casambi networks...")
    devices = await discover()
    
    if devices:
        target_device = devices[0]
        print("Connecting...")
        
        network_pwd = os.environ.get("CASAMBI_NETWORK_PWD", "")
        await casa.connect(target_device, network_pwd)
        
        print("Connected.")
    else:
        print("Warning: No networks found.")

    yield

    print("Disconnecting...")
    await casa.disconnect()

app = FastAPI(title="Casambi Web Controller", lifespan=lifespan)

@app.get("/api/lights")
async def get_lights():
    if getattr(casa, 'units', None) is None:
        return []
    
    result = []
    for u in casa.units:
        dimmer = u.state.dimmer        
        result.append({
            "name": u.name,
            "dimmer": dimmer
        })

    return result

@app.get("/api/status")
async def get_light_status(name: str):
    if getattr(casa, 'units', None) is None or not casa.units:
        raise HTTPException(status_code=503, detail="Network disconnected")
        
    for u in casa.units:
        if u.name == name:
            dimmer = u.state.dimmer
            return {"name": name, "dimmer": dimmer}
            
    raise HTTPException(status_code=404, detail="Unit not found")

@app.get("/api/set")
async def control_light(name: str, dimmer: int):
    if not (0 <= dimmer <= 255):
        raise HTTPException(status_code=400, detail="Invalid level")
        
    if getattr(casa, 'units', None) is None or not casa.units:
         raise HTTPException(status_code=503, detail="Network disconnected")
        
    target_unit = None
    for u in casa.units:
        if u.name == name:
            target_unit = u
            break
            
    if not target_unit:
        raise HTTPException(status_code=404, detail="Unit not found")
        
    await casa.setLevel(target_unit, dimmer)
        
    return {"status": "success"}

@app.get("/api/metadata")
async def get_bridge_metadata(request: Request):
    # Dynamically detect host and port from the incoming HTTP request
    host = request.url.hostname
    port = request.url.port or 8000
    
    devices_metadata = []
    
    # Iterate over available units in the Casambi network
    if getattr(casa, 'units', None) is not None:
        for u in casa.units:
            # Sanitize the device name for URL query string compatibility
            safe_name = urllib.parse.quote(u.name)
            node_identifier = u.name.replace(" ", "_").lower()
            
            device_config = {
                "node_id": f"casambi_{node_identifier}",
                "name": u.name,
                "hardware_type": "dimmable_light",
                "events": {
                    "turn_on": {
                        "trigger": "on_off_cluster",
                        "script": f"import urllib.request\n# Execute GET request to set dimmer to maximum\nurllib.request.urlopen('http://{host}:{port}/api/set?name={safe_name}&dimmer=255')"
                    },
                    "turn_off": {
                        "trigger": "on_off_cluster",
                        "script": f"import urllib.request\n# Execute GET request to turn off\nurllib.request.urlopen('http://{host}:{port}/api/set?name={safe_name}&dimmer=0')"
                    },
                    "set_level": {
                        "trigger": "level_control_cluster",
                        "script": f"import sys, urllib.request\n# Convert logical level to hardware range\nlogical_level = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0\nhardware_dimmer = int(logical_level * 255)\nurllib.request.urlopen(f'http://{host}:{port}/api/set?name={safe_name}&dimmer={{hardware_dimmer}}')"
                    },
                    "get_state": {
                        "trigger": "state_polling",
                        "script": f"import urllib.request\n# Retrieve current hardware status\nresponse = urllib.request.urlopen('http://{host}:{port}/api/status?name={safe_name}')\nprint(response.read().decode('utf-8'))"
                    }
                }
            }
            devices_metadata.append(device_config)
            
    bridge_metadata = {
        "bridge": {
            "id": "casambi_bridge_http",
            "type": "dimmable_lighting_controller",
            "network_host": host,
            "network_port": port
        },
        "devices": devices_metadata
    }
    
    return bridge_metadata

def main():
    pwd = getpass.getpass("Enter Casambi network password: ")
    os.environ["CASAMBI_NETWORK_PWD"] = pwd
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()
