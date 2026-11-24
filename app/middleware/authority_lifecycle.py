"""Fresh chain checks for mutations with no blockchain transaction backstop."""

import logging
from functools import lru_cache
from time import monotonic
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from requests import exceptions as transport
from web3 import Web3
from dark_core_lib.exceptions import ARKNotFoundError, AuthorityNotFoundError
from dark_core_lib.services import AuthorityService
from app.utils.error_headers import dark_error_headers
from app.dependencies import get_corelib_client
from app.config import get_settings
from app.middleware.auth import require_authority_identity


logger = logging.getLogger(__name__)
RPC_TIMEOUT_SECONDS = 2
RETRY_WINDOW_SECONDS = 3


@lru_cache(maxsize=1)
def _authority_reader(client):
    # Cache the transport, never the decision. Isolate its short timeout and
    # explicit retry policy from the shared provider used for transactions.
    w3 = Web3(Web3.HTTPProvider(client.config.rpc_url, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS},
                                exception_retry_configuration=None))
    contract = w3.eth.contract(address=client.authority_contract.address,
                               abi=client.authority_contract.abi)
    return AuthorityService(w3, contract, client.config).get


def get_authority_reader(client=Depends(get_corelib_client)):
    return lambda authority_id: _authority_reader(client)(authority_id)


def _is_transient(cause):
    if isinstance(cause, transport.HTTPError):
        return cause.response is not None and cause.response.status_code in {429, 502, 503, 504}
    if isinstance(cause, transport.SSLError):
        return False
    return isinstance(cause, (TimeoutError, ConnectionError, transport.Timeout, transport.ConnectionError))


def _live_authority(lookup, authority_id, method):
    started = monotonic()
    for attempt in (1, 2):
        try:
            return lookup(authority_id)
        except AuthorityNotFoundError as exc:
            raise HTTPException(403, "AUTHORITY_NOT_FOUND") from exc
        except Exception as exc:
            cause = exc.__cause__ or exc
            transient = _is_transient(cause)
            if transient and attempt == 1 and monotonic() - started < RETRY_WINDOW_SECONDS:
                continue
            request_id = uuid4().hex
            logger.warning(
                "Authority lookup failed request_id=%s method=%s exception_type=%s "
                "cause_type=%s elapsed_ms=%.1f attempts=%d",
                request_id, method, type(exc).__name__,
                type(exc.__cause__).__name__ if exc.__cause__ is not None else "-",
                (monotonic() - started) * 1000, attempt,
            )
            headers = dark_error_headers("AUTHORITY_STATUS_UNAVAILABLE", transient)
            headers["X-Request-ID"] = request_id
            raise HTTPException(503, "AUTHORITY_STATUS_UNAVAILABLE", headers=headers) from exc


def require_mutation_identity(request: Request, identity: dict = Depends(require_authority_identity),
                              lookup=Depends(get_authority_reader)):
    if not get_settings().authority_is_assigned(identity['authority_id']):
        raise HTTPException(403, 'AUTHORITY_NOT_ASSIGNED_TO_MINTER')
    # PUT payloads are published asynchronously: the contract checks their signer.
    # Reservations, local tombstones and administrative retry have no such check.
    if request.method in {'POST', 'DELETE'}:
        authority = _live_authority(lookup, identity['authority_id'], request.method)
        if not authority.active:
            raise HTTPException(403, 'AUTHORITY_INACTIVE')
    return identity


def require_current_owner(client, authority_id, naan, name, allow_missing=False):
    """Verify local-only actions. Never grant permissions or change job identity."""
    try:
        authority = client.get_authority_by_uuid(authority_id)
        if not authority.active:
            raise HTTPException(403, 'AUTHORITY_INACTIVE')
        try:
            current = client.get_ark(naan, name)
        except ARKNotFoundError:
            if allow_missing:
                return
            raise HTTPException(409, 'CHAIN_RECORD_MISSING')
        if current.owner.lower() != authority.wallet_address.lower():
            raise HTTPException(403, 'CHAIN_OWNER_CHANGED')
        if not client.is_authorized_for_naan(authority_id, naan):
            raise HTTPException(403, 'NAAN_PERMISSION_REVOKED')
        return current
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'OWNERSHIP_CHECK_UNAVAILABLE')
