# Casambi Web Controller
This document outlines the local Casambi lighting control system utilizing a web API interface.

## System Architecture
The application functions as middleware. It employs the FastAPI framework to convert standard HTTP requests into Casambi network commands. The connection lifecycle and device interactions are managed via the casambi-bt library.

## Limitations
* The system currently supports only a single Casambi network.
* Supported devices are exclusively lighting units capable of dimming.

## Execution
Execute the main Python script. The terminal will prompt for the Casambi network password. The system will automatically discover the network, establish a connection, and initialize the web server on port 8000.

## Logical Bridge Concept
* The system introduces the concept of a Logical Bridge. This is an abstraction layer that maps physical Casambi lighting hardware to virtual nodes.
* Instead of hardcoding device-specific commands into external platforms, the Logical Bridge exposes a standardized JSON metadata schema. Each virtual unit within this schema encapsulates its operational events with directly executable Python scripts.
* This architectural choice ensures high interoperability, allowing any external process with a Python interpreter to dynamically interact with the local HTTP APIs without requiring prior knowledge of the Casambi protocol.

## API Endpoints
### Get all lights
* URL: `/api/lights`
* Method: GET
* Description: Retrieves the complete list of available units in the network alongside their standard identifiers and current dimmer values.
* Sample Response:
  ```JSON
  [
    {
        "device_id": "12345678-1234-5678-1234-567812345678",
        "name": "Ceiling Light",
        "dimmer": 255
    },
    {
        "device_id": "87654321-4321-8765-4321-876543210987",
        "name": "Desk Lamp",
        "dimmer": 0
    }
  ]
  ```

### Get specific light status
* URL: `/api/status`
* Method: GET
* Parameters:
  * `device_id` (string, required): The standard identifier of the device.
* Description: Retrieves the current dimmer state of a specifically identified unit. Returns an HTTP 404 error if the unit is not found.
* Sample Response:
  ```JSON
  {
    "device_id": "12345678-1234-5678-1234-567812345678",
    "name": "Ceiling Light",
    "dimmer": 128
  }
  ```

### Control light intensity
* URL: `/api/set`
* Method: GET
* Parameters:
  * `device_id` (string, required): The standard identifier of the target device.
  * `dimmer` (integer, required): The target brightness level, strictly constrained between 0 and 255.
* Description: Transmits a command to adjust the target device to the specified dimmer level. Returns a success confirmation. Returns an HTTP 400 error if the dimmer value is invalid.
* Sample Response:
  ```JSON
  {
    "status": "success"
  }
  ```

### Read or set light level
* URL: `/api/level`
* Method: GET
* Parameters:
  * `device_id` (string, required): The standard identifier of the target device.
  * `level` (integer, optional): The target brightness level, constrained between 0 and 254 to comply with Matter Level Control Cluster specifications.
* Description: Retrieves the current level mapped to the Matter logical range (0-254) if no parameter is provided. If provided, sets the dimmer level by mapping the Matter logical range to the underlying hardware range.
* Sample Response:
  ```JSON
  {
    "status": "success",
    "level": 127
  }
  ```

### Get logical bridge metadata
* URL: `/api/metadata`
* Method: GET
* Description: Dynamically detects the incoming client request context to resolve the host IP and port. It outputs a JSON payload describing the network as a logical bridge. The payload embeds executable Python scripts mapped to standard lighting events for automated HTTP integration.
* Sample Response:
  ```JSON
  {
    "bridge": {
      "id": "casambi_bridge_http",
      "type": "dimmable_lighting_controller",
      "network_host": "192.168.1.220",
      "network_port": 8000
    },
    "devices": [
      {
        "node_id": "casambi_12345678_1234_5678_1234_567812345678",
        "name": "Ceiling Light",
        "device_id": "12345678-1234-5678-1234-567812345678",
        "hardware_type": "dimmable_light",
        "events": {
          "turn_on": {
            "trigger": "on_off_cluster",
            "script": "import urllib.request\nurllib.request.urlopen('http://192.168.1.220:8000/api/level?device_id=12345678-1234-5678-1234-567812345678&level=254')"
          },
          "turn_off": {
            "trigger": "on_off_cluster",
            "script": "import urllib.request\nurllib.request.urlopen('http://192.168.1.220:8000/api/level?device_id=12345678-1234-5678-1234-567812345678&level=0')"
          },
          "set_level": {
            "trigger": "level_control_cluster",
            "script": "import sys, urllib.request\nmatter_level = int(sys.argv[1]) if len(sys.argv) > 1 else 254\nurllib.request.urlopen(f'http://192.168.1.220:8000/api/level?device_id=12345678-1234-5678-1234-567812345678&level={matter_level}')"
          },
          "read_level": {
            "trigger": "level_control_cluster",
            "script": "import urllib.request, json\nresponse = urllib.request.urlopen('http://192.168.1.220:8000/api/level?device_id=12345678-1234-5678-1234-567812345678')\ndata = json.loads(response.read().decode('utf-8'))\nprint(data.get('level', 0))"
          }
        }
      }
    ]
  }
  ```