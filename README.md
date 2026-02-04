# dARK Core Minter API

**REST API service for the dARK Core Orchestrator**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)

## Overview

The Core Minter API exposes the dARK Core Orchestrator functionality via HTTP/JSON endpoints. It handles the full lifecycle of ARK identifiers:
- **Reserve**: Generate IDs locally (with optional external identifiers like DOI/OAI).
- **Publish**: Persist metadata to IPFS and register on blockchain (`draft` -> `published`).
- **Resolve**: Retrieve current state and metadata.
- **Tombstone**: Deactivate identifiers.

## Quick Start

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -e ../dark-core-orchestrator

# Configure
cp .env.example .env
# Edit .env with your blockchain settings

# Run (development)
uvicorn app.main:app --reload

# Run (production with workers)
uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 4

# Run via package entrypoint
dark-core-api
```

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/arks` | POST | Reserve new ARK ID (supports `alternate_identifiers`) |
| `/api/v1/arks/batch` | POST | Batch reserve ARK IDs (supports `alternate_identifiers`) |
| `/api/v1/arks/{ark}` | GET | Get ARK details |
| `/api/v1/arks/{ark}` | PUT | Update metadata & Publish (supports `alternate_identifiers`) |
| `/api/v1/arks/{ark}` | DELETE | Tombstone/Deactivate ARK |
| `/api/v1/authority/{uuid}` | GET | Get authority info |
| `/api/v1/authority/{uuid}/naans` | GET | List authority NAANs |
| `/api/v1/authority/{uuid}/authorized/{naan}` | GET | Check authority NAAN authorization |
| `/health` | GET | Health check |

## Documentation

Once running, visit:
- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`

## Configuration
 
 Key environment variables in `.env`:
 
 | Variable | Description | Default |
 |----------|-------------|---------|
 | `MINTER_SHOULDER` | Unique prefix for this minter instance (e.g. `x`, `s1`). Used in ID generation. | `""` |
 | `DARK_RPC_URL` | Blockchain RPC URL | `http://localhost:8545` |
 | `DARK_AUTHORITY_ADDRESS` | Minter Authority Address | - |
 
 ## mTLS Configuration

For production, enable mTLS by setting in `.env`:

```env
MTLS_ENABLED=true
TLS_CERT_FILE=/path/to/server.crt
TLS_KEY_FILE=/path/to/server.key
TLS_CA_FILE=/path/to/ca.crt
```

## License

GPL-3.0
