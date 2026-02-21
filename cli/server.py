import asyncio
import os
import getpass
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
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

def main():
    pwd = getpass.getpass("Enter Casambi network password: ")
    os.environ["CASAMBI_NETWORK_PWD"] = pwd
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()
