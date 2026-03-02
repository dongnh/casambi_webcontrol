# Casambi Web Controller
This document outlines the local Casambi lighting control system utilizing a web API interface.

## System Architecture
The application functions as middleware. It employs the FastAPI framework to convert standard HTTP requests into Casambi network commands. The connection lifecycle and device interactions are managed via the [casambi-bt](https://github.com/lkempf/casambi-bt) library.

## Limitations
The system currently supports only a single Casambi network.

Supported devices are exclusively lighting units capable of dimming.

## Requirements
Python

Required libraries: fastapi, uvicorn, CasambiBt

## Execution
Execute the main Python script. The terminal will prompt for the Casambi network password. The system will automatically discover the network, establish a connection, and initialize the web server on port 8000.

## API Endpoints
### Get all lights

- URL: /api/lights

- Method: GET

- Description: Retrieves the complete list of available units in the network alongside their current dimmer values.

- Sample Response:
[ { "name": "Ceiling Light", "dimmer": 255 }, { "name": "Desk Lamp", "dimmer": 0 } ]

### Get specific light status

- URL: /api/status

- Method: GET

- Parameters:

name (string, required): The exact assigned name of the device.

- Description: Retrieves the current dimmer state of a specifically named unit. Returns an HTTP 404 error if the unit is not found.

- Sample Response:
{ "name": "Ceiling Light", "dimmer": 128 }

### Control light intensity

- URL: /api/set

- Method: GET

- Parameters:

name (string, required): The exact assigned name of the target device.

dimmer (integer, required): The target brightness level, strictly constrained between 0 and 255.

- Description: Transmits a command to adjust the target device to the specified dimmer level. Returns a success confirmation. Returns an HTTP 400 error if the dimmer value is invalid.

Sample Response:
{ "status": "success" }
