"""Signed mutation authentication with sliding key cache and persistent nonces."""
import base64
import hashlib
import threading
import time
import uuid
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from dark_core_lib.request_signing import canonical_request, key_id
from dark_core_lib.exceptions import AuthorityNotFoundError
from app.config import get_settings
from app.dependencies import get_corelib_client
from app.database.connection import SessionLocal
from app.database.models import AuthorityRequestNonce

_cache = {}
_lock = threading.Lock()
_last_cleanup = 0.0


def _error(status, code):
    return HTTPException(status_code=status, detail=code, headers={"X-DARK-Error-Code": code})


def _lookup(authority, fingerprint, ttl):
    now = time.monotonic()
    with _lock:
        entry = _cache.get(authority)
        if entry and now - entry[2] < ttl and key_id(entry[0]) == fingerprint:
            return entry[0], entry[1]
    try:
        key, active = get_corelib_client().get_authority_auth(authority)
    except AuthorityNotFoundError:
        raise _error(401, "AUTHORITY_NOT_FOUND")
    except Exception:
        raise _error(503, "AUTH_REGISTRY_UNAVAILABLE")
    return key, active


def _claim_nonce(authority, nonce, expires_at):
    global _last_cleanup
    try:
        with SessionLocal()() as db:
            # Cleanup and nonce admission are atomic and independent of business writes.
            now = time.monotonic()
            if now - _last_cleanup >= 60:
                db.execute(delete(AuthorityRequestNonce).where(
                    AuthorityRequestNonce.expires_at < datetime.now(timezone.utc)))
            db.add(AuthorityRequestNonce(authority_id=authority, nonce=nonce, expires_at=expires_at))
            db.commit()
            if now - _last_cleanup >= 60:
                _last_cleanup = now
    except IntegrityError as exc:
        if getattr(exc.orig, "pgcode", None) == "23505":
            raise _error(409, "SIGNED_REQUEST_REPLAY") from exc
        raise _error(503, "AUTH_NONCE_STORE_UNAVAILABLE") from exc
    except SQLAlchemyError as exc:
        raise _error(503, "AUTH_NONCE_STORE_UNAVAILABLE") from exc


async def authenticate(request: Request):
    settings = get_settings()
    names = ["Authority-Id", "Key-Id", "Timestamp", "Nonce", "Content-SHA256", "Signature"]
    values = []
    for name in names:
        value = request.headers.get("X-DARK-" + name)
        if not value or len(value) > 512 or "\n" in value or "\r" in value:
            raise _error(401, "SIGNED_AUTH_HEADERS_REQUIRED")
        if len(request.headers.getlist("X-DARK-" + name)) != 1:
            raise _error(401, "SIGNED_AUTH_HEADERS_INVALID")
        values.append(value)
    authority, fingerprint, timestamp, nonce, digest, signature = values
    if len(authority) > 255 or authority != authority.strip():
        raise _error(401, "SIGNED_AUTH_IDENTITY_INVALID")
    try:
        if str(uuid.UUID(nonce)) != nonce or str(int(timestamp)) != timestamp:
            raise ValueError()
        seconds = int(timestamp)
        if abs(time.time() - seconds) > settings.minter_signed_auth_max_skew_seconds:
            raise _error(401, "SIGNED_AUTH_TIMESTAMP_EXPIRED")
        decoded_signature = base64.b64decode(signature, validate=True)
        if len(decoded_signature) != 64:
            raise ValueError()
    except (ValueError, OverflowError):
        raise _error(401, "SIGNED_AUTH_HEADERS_INVALID")
    body = await request.body()
    if hashlib.sha256(body).hexdigest() != digest:
        raise _error(401, "SIGNED_AUTH_BODY_MISMATCH")
    try:
        raw_path = request.scope.get("raw_path")
        path = (raw_path if raw_path is not None else request.url.path.encode("ascii")).decode("ascii")
        query = request.scope.get("query_string", b"").decode("ascii")
    except UnicodeError:
        raise _error(401, "SIGNED_AUTH_PATH_INVALID")
    if query:
        path += "?" + query
    key, active = await run_in_threadpool(_lookup, authority, fingerprint, settings.minter_client_key_cache_idle_ttl_seconds)
    if not active or len(key) != 32 or not any(key) or key_id(key) != fingerprint:
        raise _error(401, "SIGNED_AUTH_KEY_INVALID")
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(decoded_signature,
            canonical_request(request.method, path, authority, fingerprint, timestamp, nonce, digest))
    except (InvalidSignature, ValueError):
        raise _error(401, "SIGNED_AUTH_SIGNATURE_INVALID")
    expiry = datetime.fromtimestamp(seconds + settings.minter_signed_auth_max_skew_seconds, timezone.utc)
    await run_in_threadpool(_claim_nonce, authority, nonce, expiry)
    with _lock:
        # Never renew entries for invalid signatures or replayed requests.
        if len(_cache) >= 10000 and authority not in _cache:
            oldest = min(_cache, key=lambda k: _cache[k][2])
            del _cache[oldest]
        _cache[authority] = (key, active, time.monotonic())
    return {"authority_id": authority, "auth_mode": "signed", "key_id": fingerprint, "nonce": nonce}
