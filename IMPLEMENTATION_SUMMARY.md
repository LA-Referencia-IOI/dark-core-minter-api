# Implementación de Persistencia Local - dARK Core Minter API

## ✅ Completado

Se ha implementado exitosamente la capa de persistencia local para el ciclo de vida completo de ARKs con las siguientes características:

### 1. Base de Datos
- **Motor**: SQLAlchemy 2.0+ con soporte para SQLite (desarrollo) y PostgreSQL (producción)
- **Migraciones**: Alembic configurado con migración inicial
- **Tabla**: `ark_records` con todos los campos necesarios
- **Índices optimizados**:
  - Único en `ark`
  - Simples en `state`, `authority_id`, `created_at`
  - Compuesto en `(state, authority_id)` para consultas de ARKs pendientes

### 2. Modelo de Datos (`ARKRecord`)
```python
- id (PK autoincrement)
- ark (unique, indexed)
- naan, name
- state (RESERVED, DRAFT, PUBLISHED, TOMBSTONE)
- authority_id (indexed)
- target, metadata_cid, metadata_format
- alternate_identifiers (JSON)
- created_at, updated_at, tombstoned_at
- client_item_id (para batch tracking)
- publish_attempts, last_publish_error, last_publish_attempt_at
```

> **Nota**: `metadata_json` fue reemplazado por `metadata_format` (String). El contenido raw se almacena externamente vía `MetadataStorage`.

### 3. Flujo de Estados Implementado

```
RESERVED (POST /arks)
    ↓ (validación: target + metadata requeridos)
DRAFT (PUT /arks/{ark})
    ↓ (proceso asíncrono futuro: publicación a blockchain/IPFS)
PUBLISHED
    ↓
TOMBSTONE (DELETE /arks/{ark})
```

### 4. Endpoints Modificados

#### POST /arks (Reserve)
- ✅ Valida autorización NAAN con cache LRU (TTL=60s)
- ✅ Genera ARK único con shoulder
- ✅ Persiste en estado RESERVED
- ✅ Maneja colisiones con HTTPException 409

#### POST /arks/batch
- ✅ Validación de autorización única para el batch
- ✅ Manejo individual de errores (no falla todo el batch)
- ✅ Response extendido con campo `errors` (client_item_id, error, index)
- ✅ Commit atómico único al final

#### PUT /arks/{ark}
- ✅ Valida estado RESERVED
- ✅ Valida ownership (authority_id)
- ✅ Acepta `metadata` (raw JSON/XML string) y `metadata_format` ("json" o "xml")
- ✅ Almacena metadata via `MetadataStorage`, obtiene CID
- ✅ Transiciona a DRAFT (NO publica a blockchain)
- ✅ Retorna con `metadata_cid` y `metadata_format`

#### GET /arks/{ark}
- ✅ Consulta DB primero
- ✅ Para RESERVED/DRAFT: retorna desde DB
- ✅ Para PUBLISHED: combina blockchain + DB
- ✅ Fallback a blockchain si no existe en DB

#### DELETE /arks/{ark}
- ✅ Soft delete (estado TOMBSTONE)
- ✅ Timestamp tombstoned_at
- ✅ TODO: propagación futura a blockchain

### 5. Cache de Autorizaciones (LRU + Thread-Safe)
- **TTL**: 60 segundos (configurable via `AUTH_CACHE_TTL`)
- **Max size**: 1000 entradas (configurable via `AUTH_CACHE_MAXSIZE`)
- **Thread-safety**: Implementado con `threading.Lock` para acceso concurrente seguro
- **Función**: `check_authorization_cached()` en `app/utils/auth_cache.py`
- **Beneficios**: Reduce latencia en batch y llamadas frecuentes, seguro para múltiples workers

### 6. Health Check Mejorado
- ✅ Valida conexión a blockchain
- ✅ Valida conexión a DB con `SELECT 1`
- ✅ Valida estado del metadata storage
- ✅ Valida estado del worker (si está habilitado)
- ✅ Respuesta JSON correcta con `json.dumps()` para errores 503
- ✅ Retorna status:
  - `healthy`: Todo OK
  - `degraded`: Blockchain o storage down, DB OK
  - `unhealthy`: DB down (retorna 503)

### 7. Configuración

#### Variables de Entorno (.env.example)
```bash
# Database
DATABASE_URL=sqlite:///./minter.db  # Desarrollo
# DATABASE_URL=postgresql://dark:password@localhost:5432/minter  # Producción
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10

# Authorization Cache
AUTH_CACHE_TTL=60
AUTH_CACHE_MAXSIZE=1000
```

### 8. Docker & Docker Compose

#### Dockerfile
- ✅ Volumen para `/app/data` (persistencia SQLite)
- ✅ Copia alembic/ y alembic.ini
- ✅ ENV DATABASE_URL preconfigurado

#### docker-compose.yml
- ✅ Service `minter-api` con volumen persistente
- ✅ Comentarios con configuración PostgreSQL opcional
- ✅ Fácil switch entre SQLite y PostgreSQL

### 9. Tests
- ✅ Fixtures para DB en memoria
- ✅ Override de `get_db` dependency
- ✅ Tests de persistencia (`test_persistence.py`):
  - Reserve persiste en DB
  - Validación de autorización
  - Transición RESERVED → DRAFT
  - Validación de estados
  - Batch con errores parciales
  - GET desde DB
  - Tombstone
  - Health check con DB
- ✅ Tests de storage (`test_storage.py`):
  - FileSystemMetadataStorage con soporte multi-formato (JSON/XML)
  - Operaciones store_metadata(content, format)/get_metadata(cid) -> (content, format)
  - CID independiente del formato
  - Concurrencia y edge cases
- ✅ Tests de cache (`test_auth_cache.py`):
  - TTL expiration
  - Maxsize eviction
  - Thread-safety con operaciones concurrentes
  - Integración con check_authorization_cached()
- ✅ Tests de middleware (`test_middleware.py`):
  - MTLSAuthenticator con mTLS enabled/disabled
  - Validación de certificados
  - Integración con endpoints protegidos

### 10. Migraciones Alembic
- ✅ Configuración completa en `alembic/`
- ✅ Migración inicial: `d8bae116f993_initial_schema_optimized.py`
- ✅ Migración metadata format: `a1b2c3d4e5f6_replace_metadata_json_with_format.py`
- ✅ Auto-ejecución en startup con `init_db()`
- ✅ Fallo rápido si migración falla

### 11. Refactoring de Metadata Multi-Formato

Se refactorizó el manejo de metadata para soportar múltiples formatos (JSON, XML):

#### Cambios en Storage Interface
```python
# Antes
store_metadata(metadata: Dict) -> str
get_metadata(cid: str) -> Dict

# Después
store_metadata(content: str, format: str) -> str
get_metadata(cid: str) -> Tuple[str, str]  # (content, format)
```

#### Cambios en API Request
```python
class UpdateARKMetadataRequest:
    metadata: str  # Raw content (JSON/XML string)
    metadata_format: Literal["json", "xml"]
```

#### Flujo de Datos
1. API recibe `metadata` (raw string) + `metadata_format`
2. API almacena metadata via `MetadataStorage.store_metadata(content, format)`
3. API guarda `metadata_cid` y `metadata_format` en DB
4. Worker usa CID existente para publicar a blockchain

## 📋 Archivos Creados

```
app/database/
  ├── __init__.py
  ├── connection.py       # Engine, SessionLocal, init_db(), close_db()
  └── models.py           # ARKRecord ORM model

app/repositories/
  ├── __init__.py
  └── ark_repository.py   # CRUD operations

app/utils/
  └── auth_cache.py       # LRU cache con TTL

alembic/
  ├── env.py              # Configuración Alembic
  ├── script.py.mako      # Template
  ├── README
  └── versions/
      └── 001_initial_create_ark_records.py

alembic.ini               # Configuración Alembic
docker-compose.yml        # Orquestación de servicios
tests/
  ├── test_persistence.py # Tests de persistencia DB
  ├── test_storage.py     # Tests de FileSystemMetadataStorage
  ├── test_auth_cache.py  # Tests de cache thread-safe
  ├── test_middleware.py  # Tests de mTLS middleware
  ├── test_worker.py      # Tests de worker con DB
  └── test_worker_unit.py # Tests unitarios del worker
```

## 📋 Archivos Modificados

```
requirements.txt          # + SQLAlchemy, Alembic, psycopg2-binary
pyproject.toml            # + dependencias DB
.env.example              # + variables DB y cache
app/config.py             # + Settings para DB y cache
app/dependencies.py       # + get_db()
app/main.py               # + init_db() en lifespan, health check mejorado
app/api/arks.py           # Todos los endpoints refactorizados
app/models/responses.py   # ARKBatchResponse + errors
Dockerfile                # + volumen, alembic
tests/conftest.py         # + fixtures DB
```

## 🚀 Próximos Pasos (Fuera del Scope Actual)

### Background Worker para Publicación
Los ARKs en estado DRAFT están listos para publicarse pero **no se publican automáticamente**. Necesitarás:

1. **Worker asíncrono** (Celery, APScheduler, o script con cron)
2. **Proceso**:
   - Query: `SELECT * FROM ark_records WHERE state = 'draft'`
   - Para cada ARK:
     - Upload metadata a IPFS → obtener CID
     - Llamar `orchestrator.create_ark()` con CID
     - Actualizar `state = 'published'` y `metadata_cid`
3. **Manejo de errores**: Reintentos con backoff exponencial
4. **Logging**: Tracking de publicaciones exitosas/fallidas

### Validación de Ownership en DELETE
Actualmente el DELETE no valida ownership estricto. Para implementarlo:
- Extraer `authority_id` del certificado mTLS (`cert_info`)
- Comparar con `db_ark.authority_id`
- Raise 403 si no coincide

## 📊 Resumen de Mejoras

| Aspecto | Antes | Después |
|---------|-------|---------|
| **Persistencia** | Ninguna | SQLite/PostgreSQL |
| **Estados** | Solo RESERVED, salta a PUBLISHED | RESERVED → DRAFT → PUBLISHED → TOMBSTONE |
| **Validación NAAN** | No | Sí, con cache LRU |
| **Batch errors** | Falla todo | Errores individuales reportados |
| **Health check** | Solo blockchain | Blockchain + DB |
| **Tests** | Mock básico | DB en memoria + tests de persistencia |
| **Docker** | Sin persistencia | Volúmenes para SQLite/PostgreSQL |

## ✨ Características Destacadas

1. **Cache LRU thread-safe**: Reduce latencia en validaciones de autorización, seguro para múltiples workers con `threading.Lock`
2. **Transacciones atómicas**: Batch commits únicos para consistencia
3. **Soft delete**: Los ARKs tombstone se mantienen en DB con timestamp
4. **Migraciones automáticas**: `alembic upgrade head` en startup
5. **Índices optimizados**: Consultas rápidas por estado y autoridad
6. **Timestamps completos**: created_at, updated_at, tombstoned_at
7. **Fallback robusto**: Si DB falla, intenta blockchain
8. **Tests exhaustivos**: Cobertura de storage, cache, middleware, worker y persistencia
9. **Health check robusto**: Respuesta JSON válida con `json.dumps()` para todos los casos

---

**Estado**: ✅ Implementación completa y lista para pruebas
**Siguiente acción sugerida**: Ejecutar tests con `pytest tests/`
