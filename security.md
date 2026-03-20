# Security

## Objetivo

Este documento describe el modelo de seguridad del `dark-core-minter-api`, el estado actual de autenticacion/autorizacion y una propuesta de evolucion hacia un esquema de mTLS con certificados de cliente.

## Estado Actual

Hoy el `minter` soporta dos modos:

1. `MTLS_ENABLED=false`
2. `MTLS_ENABLED=true`

Cuando `MTLS_ENABLED=false`, la API no exige certificado cliente, pero los endpoints mutantes requieren una identidad explicita en alguno de estos headers:

- `X-Authority-Id`
- `X-Authority-UUID`

Ademas, cuando el request incluye `authority_id` en el body, la API valida que coincida con la identidad resuelta desde el header.

Cuando `MTLS_ENABLED=true`, la API espera una identidad autenticada derivada del certificado cliente o de headers de proxy confiables como:

- `X-SSL-Client-Verify`
- `X-SSL-Client-DN`
- `X-SSL-Client-Cert`

## Principio de diseno

Hay que separar dos conceptos:

1. `authority_id` como dato de negocio
2. identidad autenticada del caller

El `authority_id` del payload indica sobre que autoridad se quiere operar.
La identidad autenticada indica quien esta haciendo realmente la llamada.

La seguridad mejora cuando la API no confia en el body para autenticar, sino que:

1. obtiene identidad desde TLS/header confiable
2. compara esa identidad con el `authority_id` del request
3. rechaza si no coinciden

## Por que tiene sentido usar headers hoy

En desarrollo local, usar `X-Authority-Id` tiene sentido porque:

1. evita requests anonimos en endpoints mutantes
2. separa identidad de autenticacion del payload
3. prepara la migracion a mTLS real

El header en dev debe verse como un reemplazo temporal de la identidad que en produccion deberia venir del certificado.

## Limitaciones del modo actual

El modo por header tiene limitaciones importantes:

1. cualquier cliente que pueda llamar la API puede inventar el header
2. no hay prueba criptografica de posesion de identidad
3. depende de que el despliegue no exponga headers internos de forma insegura

Por eso:

1. `X-Authority-Id` es aceptable en local/dev
2. no deberia ser el mecanismo principal en produccion

## Modelo recomendado para produccion

La recomendacion es usar mTLS real con un proxy delante de la app.

### Flujo recomendado

1. El cliente abre conexion HTTPS.
2. El proxy solicita certificado cliente.
3. El cliente presenta:
   - certificado publico
   - prueba de posesion de la private key
4. El proxy valida:
   - CA emisora
   - vigencia
   - revocacion
   - identidad del certificado
5. El proxy reenvia la request a la app en una red privada.
6. La app obtiene la identidad desde headers confiables inyectados por el proxy.

### Por que usar proxy

Es preferible terminar mTLS en un proxy como Nginx, Traefik o Envoy porque:

1. simplifica la app
2. centraliza politicas TLS
3. facilita rotacion de certificados
4. reduce complejidad operativa en FastAPI/Uvicorn

## Como se presenta un certificado cliente

El certificado no viaja en el body.
Tampoco deberia ser enviado por un header arbitrario del cliente.

En mTLS real, el certificado se presenta en el handshake TLS.

Ejemplo con `curl`:

```bash
curl https://minter.example.org/api/v1/arks \
  --cert client.crt \
  --key client.key \
  --cacert ca.crt \
  -H 'Content-Type: application/json' \
  -d '{"authority_id":"auth-123","naan":"12345"}'
```

Ejemplo con `requests`:

```python
requests.post(
    "https://minter.example.org/api/v1/arks",
    json={"authority_id": "auth-123", "naan": "12345"},
    cert=("client.crt", "client.key"),
    verify="ca.crt",
)
```

## Como modelar la identidad en el certificado

La identidad de autoridad no deberia depender del `CN`.
La mejor opcion es usar `SAN`.

### Recomendacion

Incluir el `authority_id` en una URI de `SAN`, por ejemplo:

```text
URI:spiffe://dark/authorities/<authority_uuid>/clients/<client_id>
```

o un formato mas simple:

```text
URI:dark:authority:<authority_uuid>
```

Esto permite:

1. identidad estable por autoridad
2. multiples certificados por autoridad
3. parsing claro y deterministico

## Como entregar certificados a los clientes

Hay dos modelos posibles.

### Opcion A: el servidor genera key y cert

Flujo:

1. admin registra autoridad
2. servidor genera private key y certificado
3. servidor entrega ambos al cliente

No es recomendable porque:

1. el servidor ve la private key del cliente
2. aumenta el riesgo operativo

### Opcion B: el cliente genera la key y envia un CSR

Flujo:

1. el cliente genera localmente su private key
2. el cliente genera un CSR
3. envia el CSR a una CA o al Admin API
4. la CA firma el CSR
5. el cliente recibe solo el certificado firmado

Esta es la opcion recomendada.

## Propuesta concreta para dARK

### Alta de autoridad

1. `admin-api` registra la autoridad on-chain
2. la autoridad obtiene su `authority_id`
3. el cliente genera:
   - private key
   - CSR
4. el `admin-api` o una CA asociada firma el CSR
5. el cliente recibe:
   - `client.crt`
   - `ca.crt`

La private key nunca sale del cliente.

### Multiples clientes por autoridad

Una autoridad puede tener mas de un certificado:

1. `authority-123 / notebook`
2. `authority-123 / worker`
3. `authority-123 / production-app`

Esto es mejor que compartir un solo certificado entre todos los consumidores.

## Autorizacion esperada en la app

Con mTLS, la app deberia:

1. resolver identidad autenticada desde el certificado
2. usar esa identidad como fuente autoritativa
3. validar que el `authority_id` del body coincida cuando exista
4. permitir incluso omitir `authority_id` del body si la ruta no lo necesita estrictamente

## Revocacion y rotacion

Un esquema serio necesita:

1. expiracion corta de certificados
2. rotacion automatica o semi-automatizada
3. revocacion por serial o por cliente
4. capacidad de reemitir certificados comprometidos

### Recomendacion operativa

1. certificados de 30 a 90 dias
2. revocacion soportada por CRL u OCSP, o por lista interna de seriales revocados
3. separacion entre identidad de autoridad y credencial de cliente

## Reglas por entorno

### Desarrollo local

Recomendado:

1. `MTLS_ENABLED=false`
2. permitir `X-Authority-Id`
3. exigir match entre header y `authority_id` del body

### Produccion

Recomendado:

1. `MTLS_ENABLED=true`
2. no aceptar headers de identidad enviados directamente por clientes externos
3. confiar solo en headers inyectados por el proxy interno
4. preferir identidad derivada de `SAN`

## Cambios recomendados a futuro

### En `admin-api`

Agregar un flujo de firma de CSR, por ejemplo:

1. `POST /api/v1/admin/authority/{uuid}/certificates/sign-csr`
2. `GET /api/v1/admin/authority/{uuid}/certificates`
3. `POST /api/v1/admin/authority/{uuid}/certificates/{serial}/revoke`

### En `minter`

1. mantener header-based identity solo en dev
2. documentar claramente que produccion debe ir por mTLS
3. permitir que la identidad autenticada rellene `authority_id` implicitamente cuando sea seguro hacerlo

## Resumen

El estado actual con `X-Authority-Id` tiene sentido como modo de desarrollo y transicion.

La meta recomendada es:

1. mTLS real
2. certificados emitidos desde CSR generado por el cliente
3. identidad de autoridad en `SAN`
4. proxy confiable delante de la app
5. autorizacion en backend basada en identidad autenticada, no en el body
