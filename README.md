# dARK Core Minter API

**REST API service for the dARK Core Orchestrator**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)

## Overview

The Core Minter API exposes the dARK Core Orchestrator functionality via HTTP/JSON endpoints. It serves as the gateway between Minter nodes and the blockchain.

## Quick Start

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your blockchain settings

# Run (development)
uvicorn app.main:app --reload

# Run (production with workers)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/mint/batch` | POST | Persist ARKs on-chain |
| `/api/v1/authority/{uuid}` | GET | Get authority info |
| `/health` | GET | Health check |

## Documentation

Once running, visit:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

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
