# Casambi Web Controller

Using BLE to control a Casambi-based home lighting system via a web interface.

## Prerequisites
- Python 3.12 or higher.

## Installation
The project is configured via `pyproject.toml`. Install the project and dependencies using your package manager.

To install in development mode, apply the editable installation flag.

## Execution
Initiate the server execution script. The system will securely request the network password via the command-line interface (CLI) before establishing the connection.

## API Endpoints

### 1. Acquire device inventory
- **URL:** `/api/lights`
- **Method:** `GET`
- **Description:** Returns a list of all available devices and their current dimmer values.

### 2. Verify device status
- **URL:** `/api/status`
- **Method:** `GET`
- **Parameters:** `name` (string) for the specific device name.
- **Description:** Retrieves the status of a specified luminaire.

### 3. Configure brightness
- **URL:** `/api/set`
- **Method:** `GET`
- **Parameters:** `name` (string) for the device name and `dimmer` (integer) for the brightness level from 0 to 255.
- **Description:** Configures the brightness level for the specified device.
