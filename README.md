# GAM Proxy Service

A lightweight Python Flask proxy that translates JSON HTTP requests into Google Ad Manager SOAP API calls via the `googleads` Python SDK.

## Prerequisites

- Python >= 3.8
- A Google Ad Manager service account with API access
- A JSON key file for the service account

## Setup

### 1. Create a virtual environment

```bash
cd packages/gam-proxy
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure googleads.yaml

```bash
cp googleads.yaml.example googleads.yaml
```

Edit `googleads.yaml` and fill in:

- `network_code` — your GAM network code (numeric string, e.g. `"12345678"`)
- `path_to_private_key_file` — absolute or relative path to your service account JSON key file

### 4. Run the proxy

#### Development (Flask dev server)

```bash
python app.py
```

The proxy starts on `http://127.0.0.1:5000` by default. Override the port with the `GAM_PROXY_PORT` environment variable:

```bash
GAM_PROXY_PORT=5001 python app.py
```

#### Production (gunicorn)

```bash
gunicorn -w 2 -b 127.0.0.1:5000 app:app
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/gam/health` | Health check — calls `NetworkService.getCurrentNetwork()` |
| POST | `/gam/ad-units` | Create an ad unit via `InventoryService.createAdUnits()` |
| GET | `/gam/ad-units?code={adUnitCode}` | Lookup an ad unit by its code |

## Error Responses

| Status | Error Code | Description |
|--------|-----------|-------------|
| 400 | `GAM_ERROR` | Missing required fields or bad request |
| 404 | `NOT_FOUND` | Ad unit not found by code |
| 409 | `ALREADY_EXISTS` | Ad unit with same code already exists |
| 415 | `VALIDATION_ERROR` | Wrong Content-Type (must be application/json) |
| 429 | `RATE_LIMITED` | GAM API quota exceeded |
| 500 | `GAM_ERROR` | Unexpected SOAP error |
| 503 | (health only) | GAM credentials invalid or network unreachable |

## Notes

- The proxy binds to `127.0.0.1` (localhost only) — it is not intended to be exposed to external traffic.
- If `googleads.yaml` is missing at startup, the process exits immediately with a non-zero exit code.
- All requests and SOAP errors are logged via the Python `logging` module.
