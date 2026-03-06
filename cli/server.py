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

@app.get("/api/lights")
async def get_lights():
    # Return empty list if network is not ready
    if getattr(casa, 'units', None) is None:
        return []
    
    result = []
    # Iterate through units to get current state
    for u in casa.units:
        dimmer = u.state.dimmer        
        result.append({
            "name": u.name,
            "dimmer": dimmer
        })

    return result

@app.get("/api/status")
async def get_light_status(name: str):
    # Verify network connection
    if getattr(casa, 'units', None) is None or not casa.units:
        raise HTTPException(status_code=503, detail="Network disconnected")
        
    # Search for the specified unit
    for u in casa.units:
        if u.name == name:
            dimmer = u.state.dimmer
            return {"name": name, "dimmer": dimmer}
            
    raise HTTPException(status_code=404, detail="Unit not found")

@app.get("/api/set")
async def control_light(name: str, dimmer: int):
    # Validate input range
    if not (0 <= dimmer <= 255):
        raise HTTPException(status_code=400, detail="Invalid level")
        
    # Verify network connection
    if getattr(casa, 'units', None) is None or not casa.units:
         raise HTTPException(status_code=503, detail="Network disconnected")
        
    target_unit = None
    # Find the target unit
    for u in casa.units:
        if u.name == name:
            target_unit = u
            break
            
    if not target_unit:
        raise HTTPException(status_code=404, detail="Unit not found")
        
    # Execute the command
    await casa.setLevel(target_unit, dimmer)
        
    return {"status": "success"}

@app.get("/api/level")
async def get_or_set_level(name: str, level: int = None):
    # Check if the network is connected
    if getattr(casa, 'units', None) is None or not casa.units:
        raise HTTPException(status_code=503, detail="Network disconnected")
        
    # Find the target device by name
    target_unit = None
    for u in casa.units:
        if u.name == name:
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
        return {"name": name, "level": matter_level}
        
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
                        "script": f"import urllib.request\n# Execute GET request to set level to maximum (254)\nurllib.request.urlopen('http://{host}:{port}/api/level?name={safe_name}&level=254')"
                    },
                    "turn_off": {
                        "trigger": "on_off_cluster",
                        "script": f"import urllib.request\n# Execute GET request to turn off (0)\nurllib.request.urlopen('http://{host}:{port}/api/level?name={safe_name}&level=0')"
                    },
                    "set_level": {
                        "trigger": "level_control_cluster",
                        "script": f"import sys, urllib.request\n# Send integer level (0-254) directly to the API\nmatter_level = int(sys.argv[1]) if len(sys.argv) > 1 else 254\nurllib.request.urlopen(f'http://{host}:{port}/api/level?name={safe_name}&level={{matter_level}}')"
                    },
                    "read_level": {
                        "trigger": "level_control_cluster",
                        "script": f"import urllib.request, json\n# Retrieve integer level directly from the API\nresponse = urllib.request.urlopen('http://{host}:{port}/api/level?name={safe_name}')\ndata = json.loads(response.read().decode('utf-8'))\nprint(data.get('level', 0))"
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