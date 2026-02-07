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
| `/api/v1/arks/batch` | POST | Batch reserve ARK IDs (requires `client_item_id` for correlation) |
| `/api/v1/arks/{ark}` | GET | Get ARK details |
| `/api/v1/arks/{ark}` | PUT | Update metadata (JSON/XML) & transition to DRAFT state |
| `/api/v1/arks/{ark}` | DELETE | Tombstone/Deactivate ARK |
| `/api/v1/authority/{uuid}` | GET | Get authority info |
| `/api/v1/authority/{uuid}/naans` | GET | List authority NAANs |
| `/api/v1/authority/{uuid}/authorized/{naan}` | GET | Check authority NAAN authorization |
| `/api/v1/worker/status` | GET | Get async worker status and statistics |
| `/health` | GET | Health check (includes DB, blockchain, storage, worker) |

## Documentation

Once running, visit:
- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`

## Configuration Guide

### Quick Setup

```bash
# Copy the example configuration
cp .env.example .env

# Edit with your settings
nano .env  # or your preferred editor
```

### Configuration Reference

All settings are configured via environment variables or a `.env` file.

#### 🌐 API Server

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `CORE_API_HOST` | Host address to bind | `0.0.0.0` | No |
| `CORE_API_PORT` | Port number | `8001` | No |
| `BATCH_SIZE_LIMIT` | Max items per batch request | `100` | No |

#### ⛓️ Blockchain Connection

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `DARK_RPC_URL` | Blockchain RPC endpoint | `http://localhost:8545` | **Yes** |
| `DARK_CHAIN_ID` | Chain ID | `1337` | **Yes** |
| `DARK_AUTHORITY_ADDRESS` | Authority contract address | - | **Yes** |
| `DARK_CONTRACT_ADDRESS` | dARK contract address | - | **Yes** |
| `DARK_ADMIN_PRIVATE_KEY` | Admin private key for signing | - | **Yes** |

#### 🏷️ Minter Identity

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `MINTER_SHOULDER` | Unique prefix for ARK generation (e.g., `x`, `s1`, `test`) | `""` | No |

> **Note**: The shoulder helps identify ARKs from this minter instance. Use different shoulders for different environments (dev, staging, prod).

#### 🗄️ Database

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `DATABASE_URL` | Database connection string | `sqlite:///./minter.db` | No |
| `DATABASE_ECHO` | Enable SQL query logging | `false` | No |
| `DATABASE_POOL_SIZE` | Connection pool size | `5` | No |
| `DATABASE_MAX_OVERFLOW` | Max overflow connections | `10` | No |

**Development (SQLite):**
```env
DATABASE_URL=sqlite:///./minter.db
```

**Production (PostgreSQL):**
```env
DATABASE_URL=postgresql://user:password@localhost:5432/minter_db
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20
```

#### ⚡ Authorization Cache

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `AUTH_CACHE_TTL` | Cache time-to-live in seconds | `60` | No |
| `AUTH_CACHE_MAXSIZE` | Maximum cache entries | `1000` | No |

> **Note**: The cache is thread-safe and reduces blockchain queries for NAAN authorization checks.

#### 🔄 Async Worker

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `WORKER_ENABLED` | Enable background publisher | `true` | No |
| `WORKER_INTERVAL_SECONDS` | Cycle interval | `60` | No |
| `WORKER_BATCH_SIZE` | ARKs per cycle | `10` | No |
| `WORKER_MAX_RETRIES` | Max retries before failure | `5` | No |
| `WORKER_RETRY_BACKOFF_BASE` | Exponential backoff base (seconds) | `2.0` | No |

#### 📦 Metadata Storage

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `METADATA_STORAGE_TYPE` | Backend type: `filesystem` or `ipfs` | `filesystem` | No |
| `METADATA_STORAGE_PATH` | Path for filesystem storage | `./metadata_storage` | No |

#### 🔐 Security (mTLS)

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `MTLS_ENABLED` | Enable mutual TLS authentication | `false` | No |
| `TLS_CERT_FILE` | Server certificate path | - | If mTLS enabled |
| `TLS_KEY_FILE` | Server private key path | - | If mTLS enabled |
| `TLS_CA_FILE` | CA certificate for client validation | - | If mTLS enabled |

### Example Configurations

#### Development Environment

```env
# .env for local development
CORE_API_HOST=127.0.0.1
CORE_API_PORT=8001

# Local blockchain (Ganache/Hardhat)
DARK_RPC_URL=http://localhost:8545
DARK_CHAIN_ID=1337
DARK_AUTHORITY_ADDRESS=0xYourAuthorityAddress
DARK_CONTRACT_ADDRESS=0xYourContractAddress
DARK_ADMIN_PRIVATE_KEY=0xYourPrivateKey

# Development minter
MINTER_SHOULDER=dev

# SQLite for simplicity
DATABASE_URL=sqlite:///./minter_dev.db
DATABASE_ECHO=true

# Faster worker for testing
WORKER_ENABLED=true
WORKER_INTERVAL_SECONDS=10
WORKER_BATCH_SIZE=5

# Local storage
METADATA_STORAGE_TYPE=filesystem
METADATA_STORAGE_PATH=./metadata_dev

# No mTLS in development
MTLS_ENABLED=false
```

#### Production Environment

```env
# .env for production
CORE_API_HOST=0.0.0.0
CORE_API_PORT=8001

# Production blockchain
DARK_RPC_URL=https://your-rpc-endpoint.com
DARK_CHAIN_ID=12345
DARK_AUTHORITY_ADDRESS=0xProductionAuthorityAddress
DARK_CONTRACT_ADDRESS=0xProductionContractAddress
DARK_ADMIN_PRIVATE_KEY=0xProductionPrivateKey  # Use secrets manager!

# Production minter identity
MINTER_SHOULDER=prod

# PostgreSQL
DATABASE_URL=postgresql://dark_user:secure_password@db.example.com:5432/minter_prod
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=30

# Higher cache for production load
AUTH_CACHE_TTL=120
AUTH_CACHE_MAXSIZE=5000

# Production worker settings
WORKER_ENABLED=true
WORKER_INTERVAL_SECONDS=30
WORKER_BATCH_SIZE=50
WORKER_MAX_RETRIES=10

# IPFS for production (when implemented)
METADATA_STORAGE_TYPE=filesystem
METADATA_STORAGE_PATH=/data/metadata

# Enable mTLS
MTLS_ENABLED=true
TLS_CERT_FILE=/etc/certs/server.crt
TLS_KEY_FILE=/etc/certs/server.key
TLS_CA_FILE=/etc/certs/ca.crt
```

#### Docker Environment

```env
# .env for Docker deployment
CORE_API_HOST=0.0.0.0
CORE_API_PORT=8001

DARK_RPC_URL=http://besu:8545
DARK_CHAIN_ID=1337
DARK_AUTHORITY_ADDRESS=0xContainerAuthorityAddress
DARK_CONTRACT_ADDRESS=0xContainerContractAddress
DARK_ADMIN_PRIVATE_KEY=0xContainerPrivateKey

MINTER_SHOULDER=docker

# PostgreSQL container
DATABASE_URL=postgresql://dark:dark_password@postgres:5432/minter

# Container paths
METADATA_STORAGE_PATH=/app/data/metadata
```

### Validation

The application validates configuration on startup:

1. **Blockchain config**: `DARK_AUTHORITY_ADDRESS`, `DARK_CONTRACT_ADDRESS`, `DARK_ADMIN_PRIVATE_KEY` are required
2. **mTLS config**: If `MTLS_ENABLED=true`, all TLS certificate paths must be set
3. **Database**: Connection is tested during startup

If validation fails, the application will exit with a descriptive error message.

## Architecture

### ARK Lifecycle

The Minter API manages ARKs through the following states:

1. **RESERVED**: ARK ID generated and reserved locally
   - Created via `POST /api/v1/arks`
   - No blockchain interaction yet
   - Can be batch reserved with `POST /api/v1/arks/batch`

2. **DRAFT**: Metadata added, ready for publication
   - Transition via `PUT /api/v1/arks/{ark}`
   - Requires `target` URL, `metadata` (raw JSON/XML string), and `metadata_format` ("json" or "xml")
   - Metadata stored immediately, CID returned in response
   - Authorization validated before transition
   - Async worker picks up for blockchain publication

3. **PUBLISHED**: Published to blockchain and metadata stored
   - Automated by async worker
   - Metadata stored (filesystem or IPFS)
   - ARK registered on blockchain via orchestrator
   - CID stored in database

4. **TOMBSTONE**: ARK deactivated
   - Soft delete via `DELETE /api/v1/arks/{ark}`
   - Retains historical record

### Async Worker

The API includes a background worker that automatically publishes DRAFT ARKs to the blockchain:

- **Trigger**: Runs on interval (default: every 60 seconds)
- **Processing**: FIFO order (oldest drafts first)
- **Batch Size**: Configurable (default: 10 ARKs per cycle)
- **Error Handling**: 
  - Independent transactions per ARK (one failure doesn't affect others)
  - Exponential backoff for retriable errors (network, gas, etc.)
  - Permanent failure for authority errors (unauthorized, invalid NAAN)
  - Max retries before marking as permanently failed (default: 5)
- **Monitoring**: `/api/v1/worker/status` endpoint provides statistics

### Metadata Storage

Metadata is stored through an abstraction layer supporting multiple backends and formats:

**Supported Formats:**
- **JSON**: Standard JSON metadata
- **XML**: Dublin Core, OAI-DC, or custom XML schemas

**Storage Backends:**
- **Filesystem** (development): Stores files with format-appropriate extensions (`.json`, `.xml`) with MD5 as CID
- **IPFS** (future): Will store on IPFS network with real CID

CID is calculated from raw content, independent of format. Switch backends via `METADATA_STORAGE_TYPE` environment variable.

### Database

- **Development**: SQLite (file-based, zero configuration)
- **Production**: PostgreSQL recommended (set `DATABASE_URL`)
- **Migrations**: Automatic via Alembic on startup
- **Schema**: Tracks full ARK lifecycle with publish retry tracking

### Authorization Cache

The API includes a thread-safe LRU cache for NAAN authorization checks:

- **TTL**: 60 seconds (configurable via `AUTH_CACHE_TTL`)
- **Max Size**: 1000 entries (configurable via `AUTH_CACHE_MAXSIZE`)
- **Thread-Safety**: Uses `threading.Lock` for safe concurrent access with multiple workers
- **Benefits**: Reduces blockchain queries and improves latency for batch operations

## mTLS Configuration

For production, enable mTLS by setting in `.env`:

```env
MTLS_ENABLED=true
TLS_CERT_FILE=/path/to/server.crt
TLS_KEY_FILE=/path/to/server.key
TLS_CA_FILE=/path/to/ca.crt
```

## Troubleshooting

### Worker Issues

**Worker not processing ARKs:**
1. Check worker is enabled: `WORKER_ENABLED=true`
2. Verify worker status: `GET /api/v1/worker/status`
3. Check logs for errors
4. Verify ARKs are in DRAFT state (not RESERVED or permanently failed)

**ARKs stuck in DRAFT:**
1. Check `/api/v1/worker/status` for recent errors
2. Look for authority errors (indicates unauthorized NAAN)
3. Check if max retries exceeded (ARK marked as permanently failed)
4. Verify blockchain connectivity in health check

**Reset permanently failed ARK:**
```python
# Use ARKRepository.reset_publish_tracking(ark) to clear error state
# Then worker will retry on next cycle
```

### Database Migrations

**Apply migrations manually:**
```bash
source .venv/bin/activate
alembic upgrade head
```

**Create new migration:**
```bash
alembic revision -m "description"
```

### Health Check

Check all components:
```bash
curl http://localhost:8001/health
```

Response includes:
- `blockchain_connected`: Blockchain RPC connectivity
- `database`: Database health
- `metadata_storage`: Storage backend health
- `worker`: Worker status and last run time

## Testing

Run all tests:
```bash
pytest tests/ -v
```

Run specific test categories:
```bash
# Persistence tests
pytest tests/test_persistence.py -v

# Storage tests
pytest tests/test_storage.py -v

# Cache thread-safety tests
pytest tests/test_auth_cache.py -v

# mTLS middleware tests
pytest tests/test_middleware.py -v

# Worker tests
pytest tests/test_worker.py tests/test_worker_unit.py -v
```

### Test Coverage

| Module | Coverage |
|--------|----------|
| `storage/filesystem.py` | Store, retrieve, health check, concurrency |
| `utils/auth_cache.py` | TTL, eviction, thread-safety |
| `middleware/auth.py` | mTLS enabled/disabled, cert validation |
| `workers/publisher.py` | Batch processing, retry logic, error handling |
| `repositories/ark_repository.py` | Full CRUD, state transitions |

## License

GPL-3.0
