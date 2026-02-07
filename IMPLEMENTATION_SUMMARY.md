# Implementación Actual - dARK Core Minter API

## Estado

Implementación activa con arquitectura separada:

- API HTTP en proceso dedicado (`app.main`)
- Worker publisher en proceso dedicado (`app.main_worker`)
- Persistencia local completa del ciclo de vida de ARKs en `ark_records`
- Minting secuencial determinístico por namespace en `noid_counters`

## 1. Arquitectura de Procesos

### API (solo HTTP)

- Entry point: `app/main.py`
- CLI: `dark-core-api`
- Uvicorn directo: `uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 4`
- Expone endpoints ARK y Authority
- No ejecuta scheduler ni publisher interno

### Worker (solo publicación)

- Entry point: `app/main_worker.py`
- CLI: `dark-core-worker`
- Scheduler APScheduler con ciclo de publicación por intervalo
- Comando de estado de proceso: `dark-core-worker-status`
- También disponible por módulo: `python -m app.main_worker status`

### Modelo operacional

- API y worker corren desacoplados
- Se despliegan y escalan de forma independiente
- El estado del worker se expone vía API usando heartbeat en DB

## 2. Persistencia y Modelo de Datos

### Base de datos

- SQLAlchemy 2.0+
- Alembic para migraciones
- PostgreSQL como motor estándar (dev/staging/prod)

### Tabla principal: `ark_records`

Campos relevantes:

- `id` (PK autoincrement)
- `naan`, `name`
- `state` (`R`, `D`, `P`, `T`)
- `authority_id`
- `target`, `metadata_cid`, `metadata_format`
- `alternate_identifiers`
- `created_at`, `updated_at`, `tombstoned_at`
- `client_item_id`
- tracking de publicación:
  - `publish_retry_count`
  - `publish_last_error`
  - `publish_last_attempt_at`
  - `publish_permanently_failed`

### Decisión de modelado

- No existe columna `ark` física
- El ARK completo se calcula como `ark:{naan}/{name}`
- Restricción única real: `uq_naan_name` sobre `(naan, name)`

### Tabla de secuencia: `noid_counters`

Campos relevantes:

- `namespace_key` (PK, formato `NAAN:SHOULDER`)
- `next_value` (siguiente contador a asignar)
- `updated_at`

### Estrategia de minting y concurrencia

- El nombre ARK ya no se genera random.
- `POST /arks` y `POST /arks/batch` asignan contador secuencial por namespace.
- El contador se incrementa atómicamente y luego se codifica en base29 (sin vocales).
- Política actual: largo fijo `7` para la parte generada (operado vía `MINTER_NOID_MIN_LENGTH=7`).
- Capacidad por namespace: `29^7 = 17,249,876,309`.
- Se agrega checkdigit NOID al final del `name` (configurable, default activo).
- `GET/PUT/DELETE` validan checkdigit cuando `MINTER_NOID_CHECKDIGIT=true`.
- Con validación activa, ARKs legacy sin checkdigit válido son rechazados con `400`.
- Inserción de `ark_records` en savepoint para manejar colisiones sin perder avance del contador.
- Worker usa claim de filas con locking PostgreSQL (`FOR UPDATE SKIP LOCKED`) para evitar doble publicación concurrente.

## 3. Flujo de Estados

```text
RESERVED (POST /arks)
    -> DRAFT (PUT /arks/{ark})
    -> PUBLISHED (worker standalone)
    -> TOMBSTONE (DELETE /arks/{ark})
```

## 4. Endpoints API

### ARKs

- `POST /api/v1/arks`
- `POST /api/v1/arks/batch`
- `GET /api/v1/arks/{ark}`
- `PUT /api/v1/arks/{ark}`
- `DELETE /api/v1/arks/{ark}`

### Authority

- `GET /api/v1/authority/{uuid}`
- `GET /api/v1/authority/{uuid}/naans`
- `GET /api/v1/authority/{uuid}/authorized/{naan}`

### Worker

- `GET /api/v1/worker/status` (lee heartbeat en DB, sin acoplar procesos)

### Health

- `GET /health`
- Verifica blockchain, DB y metadata storage
- Para estado del worker usar `GET /api/v1/worker/status`

## 5. Worker Publisher Standalone

### Comportamiento

- Toma ARKs `DRAFT` pendientes
- Aplica backoff exponencial por reintentos
- Publica en blockchain via orchestrator
- Actualiza estado a `PUBLISHED` o marca fallos de publicación

### Scheduler

- Basado en `APScheduler`
- Configurado por:
  - `WORKER_INTERVAL_SECONDS`
  - `WORKER_BATCH_SIZE`
  - `WORKER_MAX_RETRIES`
  - `WORKER_RETRY_BACKOFF_BASE`

### Operación singleton por proceso

- Usa pid file: `/tmp/dark-core-worker.pid`
- Evita doble instancia local del worker
- `dark-core-worker-status` retorna:
  - `RUNNING pid=<pid>` (exit code 0)
  - `NOT_RUNNING` (exit code 1)
- Publica heartbeat periódico en DB:
  - `worker_name`
  - `status` (`STARTING`, `RUNNING`, `ERROR`, `STOPPED`)
  - `last_heartbeat_at`
  - métricas de procesamiento

## 6. Metadata Multi-Formato

- `metadata` se recibe como string raw
- `metadata_format` soporta `json` y `xml`
- Contenido se persiste en storage externo
- DB guarda `metadata_cid` + `metadata_format`

## 7. Configuración Relevante

```bash
# DB
DATABASE_URL=postgresql://dark:dark_password@localhost:5432/minter
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10

# Worker standalone
WORKER_ENABLED=true
WORKER_INTERVAL_SECONDS=60
WORKER_BATCH_SIZE=10
WORKER_MAX_RETRIES=5
WORKER_RETRY_BACKOFF_BASE=2.0
WORKER_RUNTIME_NAME=ark-publisher
WORKER_HEARTBEAT_INTERVAL_SECONDS=10
WORKER_HEARTBEAT_STALE_AFTER_SECONDS=180

# Cache autorización
AUTH_CACHE_TTL=60
AUTH_CACHE_MAXSIZE=1000

# Minting secuencial
MINTER_SHOULDER=
MINTER_NOID_MIN_LENGTH=7
MINTER_NOID_CHECKDIGIT=true
```

## 8. Docker y Compose

`docker-compose.yml` define servicios separados:

- `postgres`
- `minter-api`
- `minter-worker`

API y worker dependen de `postgres` y usan `DATABASE_URL` PostgreSQL compartida.

## 9. Testing

Suite principal:

- `tests/test_persistence.py`
- `tests/test_worker.py`
- `tests/test_worker_unit.py`
- `tests/test_storage.py`
- `tests/test_auth_cache.py`
- `tests/test_middleware.py`

Validación reciente de regresión (worker + persistencia): `22 passed`.

## 10. Migraciones

- Migraciones en `alembic/versions/`
- Auto-ejecución de `alembic upgrade head` en startup de API y worker
- Fail-fast si migración falla

## 11. Archivos Clave Actualizados

- `app/main.py` (API sin scheduler interno)
- `app/main_worker.py` (worker standalone + heartbeat DB + status/pidfile)
- `app/api/worker.py` (status API basado en heartbeat DB)
- `app/api/router.py` (rutas de worker restauradas con backend DB)
- `docker-compose.yml` (servicios API/worker separados)
- `pyproject.toml` (scripts `dark-core-worker` y `dark-core-worker-status`)
- `README.md` (operación actual)
- `noid.md` (especificación detallada de esquema y minting NOID)

## 12. Pendientes Técnicos Recomendados

- Endurecer transición de estados con compare-and-set atómico
- Validación estricta de ownership en DELETE (mTLS -> `authority_id`)
- Alertas operativas sobre heartbeat stale (Prometheus/Grafana o equivalente)

---

**Estado general:** Implementación operativa con separación completa API/worker y scheduler ejecutándose en proceso dedicado.
