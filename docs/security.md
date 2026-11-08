# Minter request authentication

The Minter authenticates mutations using optional Ed25519-signed DARK2
requests. Authentication identifies an authority; it does not grant NAAN
permissions or ARK ownership. Admin API authentication and Dashboard login are
separate.

This page summarizes the Minter implementation. The deployer's
[authentication design](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/minter-auth/minter-authentication-design.md)
and [client integration guide](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/minter-auth/minter-client-authentication-guide.md)
contain the complete protocol, examples and troubleshooting.

## Modes and transport

`MINTER_SIGNED_AUTH_ENABLED` defaults to `false` in the Minter source:

| Value | Identity on protected operations |
|---|---|
| `false` | `X-DARK-Authority-Id` is required but is not cryptographically verified. The caller can assert an identity, so unsigned mode needs an independently enforced trusted access boundary. |
| `true` | All six DARK2 headers are required and the signature is verified against the authority's registered client key. |

Both modes retain applicable assignment, active-status, body identity, NAAN
permission and ownership checks. Restart the API after changing the setting.
Certificate metadata does not supply the identity for these routes.

`MTLS_ENABLED` and the `TLS_*` settings remain for legacy helpers; they do not
activate a certificate gate on the current Minter routes or configure TLS in
the standard server entrypoint. Use HTTPS with server certificate verification
at a trusted gateway and protect direct upstream access. A gateway may enforce
additional access or client-certificate policies; DARK2 does not require a
client certificate or client CA.

## Protected operations

These routes require the authority header in both modes and a DARK2 signature
when enabled:

| Method and route | Operation |
|---|---|
| `POST /api/v1/arks` | Reserve an ARK |
| `POST /api/v1/arks/batch` | Reserve a batch |
| `PUT /api/v1/arks/{ark:path}` | Stage metadata and target |
| `DELETE /api/v1/arks/{ark:path}` | Mark a local tombstone |
| `POST /api/v1/worker/retry` | Retry eligible failed ARKs owned by the requesting authority |

The Minter router does not require authentication for its ARK, authority or
worker reads. `POST /api/v1/arks/status/batch` is a read despite its method and
also has no authentication dependency. Gateways may restrict any of these
routes. A local tombstone is not currently propagated to the blockchain.

## Keys and signed requests

Each authority can have one current Ed25519 client public key in the Authority
contract, independent of its blockchain wallet. The client keeps its private
key. An operator registers the public key through Admin at authority creation
or replaces it with `PUT /api/v1/admin/authority/{uuid}/client-key`. Admin
accepts an OpenSSH Ed25519 public-key line or standard base64 of the raw
32-byte public key. Authorities without a key cannot use signed mutations
until one is registered.

For each signed operation send:

1. `X-DARK-Authority-Id` -- authority ID; it must match the body where present.
2. `X-DARK-Key-Id` -- lowercase SHA-256 hex of the raw public-key bytes.
3. `X-DARK-Timestamp` -- canonical decimal Unix seconds.
4. `X-DARK-Nonce` -- canonical UUID; generate a new UUID for every attempt.
5. `X-DARK-Content-SHA256` -- lowercase SHA-256 hex of the exact request body.
6. `X-DARK-Signature` -- standard base64 of the 64-byte Ed25519 signature.

The signed UTF-8 message has eight LF-separated lines and no trailing LF:

```text
DARK2
<UPPERCASE_METHOD>
<RAW_PATH_AND_NONEMPTY_QUERY>
<AUTHORITY_ID>
<KEY_ID>
<TIMESTAMP>
<NONCE>
<BODY_SHA256>
```

Sign the bytes directly with ordinary Ed25519. Serialize the body once and send
the exact bytes that were hashed. The path and query are those received by the
application; coordinate any proxy rewriting with the operator. The signature
does not bind the destination hostname. Disable automatic redirects and verify
the server's TLS certificate. `dark_core_lib.request_signing.sign_request`
provides a Python helper.

## Replay, cache and operational limits

- The timestamp tolerance defaults to 300 seconds on either side of the
  server clock (`MINTER_SIGNED_AUTH_MAX_SKEW_SECONDS`). Synchronize client and
  server clocks.
- Migration `0002_authority_request_nonces` creates the PostgreSQL nonce
  store. Replicas of the same logical endpoint must share it. A nonce is
  admitted in a separate transaction before business execution; a subsequent
  business failure does not release it. Reuse returns
  `409 SIGNED_REQUEST_REPLAY`, not the original result.
- Replay protection is not business idempotency. After a timeout or lost
  response, reconcile state and correlation before issuing a new signed
  modification; a new nonce can cause a second business operation.
- The per-process client-key cache stores the key and active status.
  `MINTER_CLIENT_KEY_CACHE_IDLE_TTL_SECONDS` defaults to 60 seconds; accepted
  signatures renew the idle timer, while invalid requests and replays do not.
  Continuous valid traffic can retain an old key or active state beyond 60
  seconds after an on-chain change. A different key fingerprint causes a
  registry lookup, and separate API processes can have different cache states.
  Urgent revocation requires coordinated traffic/cache/process measures.
- NAAN authorization has a separate cache. Some operations also perform fresh
  active-status or ownership checks; see
  [architecture.md](architecture.md#12-security-model).

Before enabling signatures, deploy compatible Authority contract and ABI,
Minter and `dark-core-lib` versions, apply the nonce migration, and register a
client key for each signing authority. Keep private client keys out of
inventories, images and the blockchain. For deployment settings and detailed
error codes, see the linked deployer guides.
