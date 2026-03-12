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
    # Discover Casambi networks
    print("Discovering Casambi networks...")
    devices = await discover()
    
    if devices:
        target_device = devices[0]
        print("Connecting...")
        
        # Retrieve network password from environment variables
        network_pwd = os.environ.get("CASAMBI_NETWORK_PWD", "")
        await casa.connect(target_device, network_pwd)
        
        print("Connected.")
    else:
        print("Warning: No networks found.")

    yield

    # Disconnect gracefully on shutdown
    print("Disconnecting...")
    await casa.disconnect()

app = FastAPI(title="Casambi Web Controller", lifespan=lifespan)

def get_unit_id(unit):
    # Extract standard identifier (uuid, id, or address) with fallback priority
    if hasattr(unit, 'uuid') and unit.uuid:
        return str(unit.uuid)
    if hasattr(unit, 'id') and unit.id:
         return str(unit.id)
    if hasattr(unit, 'address') and unit.address:
         return str(unit.address)
    # Fallback to name if no standard ID is exposed by the API
    return str(unit.name)

@app.get("/api/lights")
async def get_lights():
    # Return empty list if network is not ready
    if getattr(casa, 'units', None) is None:
        return []
    
    result = []
    # Iterate through units to get current state and device_id
    for u in casa.units:
        dimmer = u.state.dimmer        
        result.append({
            "device_id": get_unit_id(u),
            "name": u.name,
            "dimmer": dimmer
        })

    return result

@app.get("/api/status")
async def get_light_status(device_id: str):
    # Verify network connection
    if getattr(casa, 'units', None) is None or not casa.units:
        raise HTTPException(status_code=503, detail="Network disconnected")
        
    # Search for the specified unit using standard ID
    for u in casa.units:
        if get_unit_id(u) == device_id:
            dimmer = u.state.dimmer
            return {"device_id": device_id, "name": u.name, "dimmer": dimmer}
            
    raise HTTPException(status_code=404, detail="Unit not found")

@app.get("/api/set")
async def control_light(device_id: str, dimmer: int):
    # Validate input range
    if not (0 <= dimmer <= 255):
        raise HTTPException(status_code=400, detail="Invalid level")
        
    # Verify network connection
    if getattr(casa, 'units', None) is None or not casa.units:
         raise HTTPException(status_code=503, detail="Network disconnected")
        
    target_unit = None
    # Find the target unit using standard ID
    for u in casa.units:
        if get_unit_id(u) == device_id:
            target_unit = u
            break
            
    if not target_unit:
        raise HTTPException(status_code=404, detail="Unit not found")
        
    # Execute the command
    await casa.setLevel(target_unit, dimmer)
        
    return {"status": "success"}

@app.get("/api/level")
async def get_or_set_level(device_id: str, level: int = None):
    # Check if the network is connected
    if getattr(casa, 'units', None) is None or not casa.units:
        raise HTTPException(status_code=503, detail="Network disconnected")
        
    # Find the target device by standard ID
    target_unit = None
    for u in casa.units:
        if get_unit_id(u) == device_id:
            target_unit = u
            break
            
    # Raise an error if the device is not found
    if not target_unit:
        raise HTTPException(status_code=404, detail="Unit not found")
        
    # Return current level if level parameter is not provided
    if level is None:
        current_dimmer = target_unit.state.dimmer
        # Map hardware range (0-255) to Matter logical range (0-254)
        matter_level = int((current_dimmer / 255.0) * 254) if current_dimmer is not None else 0
        return {"device_id": device_id, "level": matter_level}
        
    # Validate the input range for Matter Level Control Cluster
    if not (0 <= level <= 254):
        raise HTTPException(status_code=400, detail="Invalid level")
        
    # Map Matter logical range (0-254) to hardware range (0-255)
    hardware_dimmer = int((level / 254.0) * 255)
    await casa.setLevel(target_unit, hardware_dimmer)
        
    return {"status": "success", "level": level}

@app.get("/api/metadata")
async def get_bridge_metadata(request: Request):
    # Dynamically detect host and port from the incoming HTTP request
    host = request.url.hostname
    port = request.url.port or 8000
    
    devices_metadata = []
    
    # Iterate over available units in the Casambi network
    if getattr(casa, 'units', None) is not None:
        for u in casa.units:
            unit_id = get_unit_id(u)
            
            # Sanitize the device ID for URL query string compatibility
            safe_id = urllib.parse.quote(unit_id)
            node_identifier = unit_id.replace("-", "_").replace(":", "_").lower()
            
            # Keep original naming logic for metadata display purposes
            primary_name = u.names[0] if hasattr(u, 'names') and getattr(u, 'names') else getattr(u, 'name', 'Unknown_Device')
            
            device_config = {
                "node_id": f"casambi_{node_identifier}",
                "name": primary_name,
                "device_id": unit_id,
                "hardware_type": "dimmable_light",
                "events": {
                    "turn_on": {
                        "trigger": "on_off_cluster",
                        "script": f"import urllib.request\n# Execute GET request to set level to maximum (254)\nurllib.request.urlopen('http://{host}:{port}/api/level?device_id={safe_id}&level=254')"
                    },
                    "turn_off": {
                        "trigger": "on_off_cluster",
                        "script": f"import urllib.request\n# Execute GET request to turn off (0)\nurllib.request.urlopen('http://{host}:{port}/api/level?device_id={safe_id}&level=0')"
                    },
                    "set_level": {
                        "trigger": "level_control_cluster",
                        "script": f"import sys, urllib.request\n# Send integer level (0-254) directly to the API\nmatter_level = int(sys.argv[1]) if len(sys.argv) > 1 else 254\nurllib.request.urlopen(f'http://{host}:{port}/api/level?device_id={safe_id}&level={{matter_level}}')"
                    },
                    "read_level": {
                        "trigger": "level_control_cluster",
                        "script": f"import urllib.request, json\n# Retrieve integer level directly from the API\nresponse = urllib.request.urlopen('http://{host}:{port}/api/level?device_id={safe_id}')\ndata = json.loads(response.read().decode('utf-8'))\nprint(data.get('level', 0))"
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
    # Prompt for network password before starting the server
    pwd = getpass.getpass("Enter Casambi network password: ")
    os.environ["CASAMBI_NETWORK_PWD"] = pwd
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()