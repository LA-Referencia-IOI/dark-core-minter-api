# Minter request authentication

The implemented protocol is documented in [README.md](README.md#authentication-and-authorization).

Mutations and administrative retries use optional Ed25519 signatures, with one
public key per authority in the Authority contract. Admin manages registration
and replacement. Queries are public. HTTPS terminates at the gateway; client
certificates and a client CA are not required.

`MINTER_SIGNED_AUTH_ENABLED=false` disables only cryptographic verification;
authority headers, ownership and NAAN rules remain enforced. `true` requires the
updated contract and nonce migration.

The sliding 60-second default cache includes key and authority active state.
Continued valid use can retain old cached credentials and active state after
rotation or deactivation. Expired cache entries require an available registry.
Administrative retry is restricted to ARKs owned by the requesting authority.

Nonce admission is durable before business execution. A lost response requires
state reconciliation by the caller; a new nonce may represent another operation.

See the cross-component [design](../../docs/minter-authentication-design.md).
