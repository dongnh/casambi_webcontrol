# Casambi Web Controller

Using BLE to control a Casambi-based home lighting system via a web interface, run on local machines PC/Mac.
Based on a great library casambi-bt (`https://github.com/lkempf/casambi-bt`) - a bluetooth based Python library for controlling Casambi networks.

## Prerequisites
- Python 3.12 or higher.

## Installation
```bash
python -m venv venv
source venv/bin/activate
pip install casambi-web-controller
```

## Execution
```bash
casambi-srv
```

## API Endpoints

### 1. Acquire device inventory
- **URL:** `/api/lights`
- **Method:** `GET`
- **Description:** Returns a list of all available devices and their current dimmer values.
- **Example:** `http://localhost:8000/api/lights`

### 2. Verify device status
- **URL:** `/api/status`
- **Method:** `GET`
- **Parameters:** `name` (string) for the specific device name.
- **Description:** Retrieves the status of a specified luminaire.
- **Example:** `http://localhost:8000/api/status?name=Entry Hall`

### 3. Configure brightness
- **URL:** `/api/set`
- **Method:** `GET`
- **Parameters:** `name` (string) for the device name and `dimmer` (integer) for the brightness level from 0 to 255.
- **Description:** Configures the brightness level for the specified device.
- **Example:** `http://localhost:8000/api/set?name=Entry Hall&dimmer=128`
