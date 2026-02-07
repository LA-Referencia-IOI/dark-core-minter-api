# NOID Implementation (dARK Core Minter API)

Este documento describe en detalle la implementación actual de NOID en `dark-core-minter-api`.

## 1. Resumen Ejecutivo

El minting de ARKs usa un esquema determinístico, secuencial y persistente:

- Contador por namespace en DB (`noid_counters`).
- Codificación base29 sin vocales.
- Largo de parte generada operado como fijo (`7`).
- Checkdigit NOID al final (activo por defecto).

Resultado: IDs estables, sin dependencia de RNG, con capacidad predecible por namespace.

## 2. Formato del Identificador

ARK completo:

```text
ark:{naan}/{name}
```

`name` actual:

```text
{shoulder}{counter_part}{checkdigit}
```

Donde:

- `shoulder`: prefijo opcional del minter.
- `counter_part`: contador codificado en base29 con largo fijo `7`.
- `checkdigit`: caracter calculado sobre `"{naan}/{shoulder}{counter_part}"`.

Ejemplo:

- `ark:12345/x0000000d`
- `ark:12345/x0000001w`

## 3. Alfabeto y Base

Alfabeto:

```text
0123456789bcdfghjkmnpqrstvwxz
```

- Base = 29.
- Se excluyen vocales para evitar palabras accidentales.

Implementado en:

- `app/utils/noid.py` (`ALPHABET`, `BASE`).

## 4. Configuración Relevante

Variables:

- `MINTER_SHOULDER` (default `""`)
- `MINTER_NOID_MIN_LENGTH` (default `7`)
- `MINTER_NOID_CHECKDIGIT` (default `true`)

Nota importante:

- El nombre de la variable `MINTER_NOID_MIN_LENGTH` es histórico.
- Política actual: se opera como largo fijo recomendado `7`.

## 5. Capacidad por Namespace

Capacidad de `counter_part`:

```text
base^length = 29^7 = 17,249,876,309
```

Esto es por cada namespace:

```text
namespace = "{NAAN}:{SHOULDER}"
```

El checkdigit no reduce esta capacidad porque se agrega al final.

## 6. Modelo de Datos

Tabla:

- `noid_counters`
  - `namespace_key` (PK, `NAAN:SHOULDER`)
  - `next_value` (siguiente valor a asignar)
  - `updated_at`

Migración:

- `alembic/versions/0002_add_noid_counters.py`

## 7. Flujo de Minting

### 7.1 Reserva simple (`POST /api/v1/arks`)

1. Validar autorización de authority/NAAN.
2. Construir `namespace_key`.
3. Reservar contador atómicamente (`allocate_next`).
4. Codificar `counter_part` en base29.
5. Construir `name` con shoulder.
6. Si `MINTER_NOID_CHECKDIGIT=true`, agregar checkdigit.
7. Insertar `ark_records` en savepoint.
8. `commit` transacción.

### 7.2 Reserva batch (`POST /api/v1/arks/batch`)

- Mismo flujo por item.
- Cada item usa retries por colisión.
- Se acumulan resultados y errores.
- Commit al final del batch.

## 8. Concurrencia y Atomicidad

Repositorio:

- `app/repositories/noid_counter_repository.py`

Estrategia:

- Inserción lazy de fila namespace (`INSERT ... ON CONFLICT DO NOTHING` en SQLite/PostgreSQL).
- Incremento atómico con `UPDATE ... RETURNING` cuando disponible.
- Fallback con lock transaccional (`with_for_update`) para dialectos sin `RETURNING`.

Objetivo:

- Evitar que dos procesos asignen el mismo `counter_value` en el mismo namespace.

## 9. Checkdigit

Funciones:

- `compute_checkdigit(payload: str) -> str`
- `validate_name_checkdigit(naan: str, name: str) -> bool`

Regla:

- Para cada caracter, tomar su ordinal dentro de `ALPHABET`.
- Caracteres fuera del alfabeto aportan `0`.
- Suma ponderada por posición (base 1).
- `sum % 29` determina el caracter checkdigit.

Payload usado para mint:

```text
{naan}/{name_sin_checkdigit}
```

## 10. Validación en API

Cuando `MINTER_NOID_CHECKDIGIT=true`:

- `GET /api/v1/arks/{ark}`
- `PUT /api/v1/arks/{ark}`
- `DELETE /api/v1/arks/{ark}`

validan el checkdigit del `name`.

Si no coincide:

- HTTP `400` con detalle de checkdigit inválido.

Efecto colateral esperado:

- ARKs legacy sin checkdigit válido quedan rechazados por estas rutas mientras la validación esté activa.

## 11. Política de Overflow

Con largo fijo `7`, si `counter_value` excede la capacidad del espacio, la codificación crecería naturalmente.

Política operativa actual:

- Mantener `MINTER_NOID_MIN_LENGTH=7` como estándar de desarrollo.
- Monitorear consumo por namespace.
- Si hiciera falta ampliar espacio, migrar a largo `8` de forma controlada.

## 12. Errores y Retries

- Colisiones únicas en `ark_records` (`uq_naan_name`):
  - Se reintenta con siguiente contador.
- Errores de integridad no-unique:
  - `500`.
- Si se agotan retries por colisión:
  - `409`.

## 13. Archivos Clave

- `app/utils/noid.py`
- `app/api/arks.py`
- `app/repositories/noid_counter_repository.py`
- `app/database/models.py`
- `alembic/versions/0002_add_noid_counters.py`
- `tests/test_persistence.py`

## 14. Decisiones de Diseño

1. Contador en DB en lugar de random:
   - evita colisiones probabilísticas y facilita trazabilidad.
2. Namespace por `NAAN + shoulder`:
   - aislamiento por emisor/contexto.
3. Checkdigit por defecto:
   - detección temprana de errores de tipeo/corrupción.
4. Largo fijo operativo:
   - formato estable y predecible para clientes.
