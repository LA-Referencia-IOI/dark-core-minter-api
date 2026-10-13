"""Fresh chain checks for mutations with no blockchain transaction backstop."""
from fastapi import Depends, HTTPException, Request
from dark_core_lib.exceptions import ARKNotFoundError
from app.dependencies import get_corelib_client
from app.config import get_settings
from app.middleware.auth import require_authority_identity


def require_mutation_identity(request: Request, identity: dict = Depends(require_authority_identity),
                              client=Depends(get_corelib_client)):
    if not get_settings().authority_is_assigned(identity['authority_id']):
        raise HTTPException(403, 'AUTHORITY_NOT_ASSIGNED_TO_MINTER')
    # PUT payloads are published asynchronously: the contract checks their signer.
    # Reservations, local tombstones and administrative retry have no such check.
    if request.method in {'POST', 'DELETE'}:
        try:
            authority = client.get_authority_by_uuid(identity['authority_id'])
        except Exception:
            raise HTTPException(503, 'AUTHORITY_STATUS_UNAVAILABLE')
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
