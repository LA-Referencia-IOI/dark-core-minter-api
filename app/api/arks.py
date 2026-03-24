"""
ARK Native REST Endpoints.

Implements the ARK lifecycle: reserved -> draft -> published -> tombstone.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from dark_core_lib import DARKCoreClient
from dark_core_lib.metadata import Level1Metadata, MetadataStorage

from app.dependencies import get_corelib_client, get_db, get_metadata_storage
from app.middleware.auth import (
    enforce_authority_match,
    require_authority_identity,
    require_mtls,
)
from app.utils.noid import mint_ark_id, validate_name_checkdigit
from app.utils.auth_cache import check_authorization_cached
from app.models.states import ARKState
from app.repositories import ARKRepository, NoidCounterRepository
from app.models.requests import (
    ReserveARKRequest,
    ReserveBatchRequest,
    UpdateARKMetadataRequest,
)
from app.models.responses import (
    ARKResponse,
    ARKBatchResponse,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _normalize_wallet(value: Optional[str]) -> Optional[str]:
    """Normalize wallet addresses for case-insensitive comparison."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned.lower() if cleaned else None


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return True when integrity error is caused by uniqueness constraints."""
    detail = str(getattr(exc, "orig", exc))
    lowered = detail.lower()
    return "unique" in lowered or "duplicate" in lowered


def _validate_ark_checkdigit_if_enabled(naan: str, name: str) -> None:
    """Validate ARK name checkdigit when MINTER_NOID_CHECKDIGIT is enabled."""
    settings = get_settings()
    if not settings.minter_noid_checkdigit:
        return
    try:
        validate_name_checkdigit(naan, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ARK checkdigit: {exc}") from exc


def _extract_minimal_metadata(level1_json: Optional[dict]) -> Optional[dict]:
    """Return validated minimal (Level-1) metadata payload when available."""
    if not isinstance(level1_json, dict):
        return None
    return level1_json


@router.post(
    "",
    response_model=ARKResponse,
    response_model_exclude_none=True,
    status_code=201,
    summary="Reserve ARK",
    description="Reserve a new ARK identifier. Initial state is 'reserved'.",
)
async def reserve_ark(
    request: ReserveARKRequest,
    identity: dict = Depends(require_authority_identity),
    corelib_client: DARKCoreClient = Depends(get_corelib_client),
    db: Session = Depends(get_db),
) -> ARKResponse:
    """
    Reserve a single ARK.
    
    Generates a unique ID and persists in database.
    """
    enforce_authority_match(identity, request.authority_id)

    # 1. Validate authorization (with cache)
    if not check_authorization_cached(corelib_client, request.authority_id, request.naan):
        raise HTTPException(
            status_code=403,
            detail=f"Authority {request.authority_id} not authorized for NAAN {request.naan}"
        )
    
    # 2. Allocate deterministic counter and persist reservation
    settings = get_settings()
    ark_repo = ARKRepository(db)
    counter_repo = NoidCounterRepository(db)
    namespace_key = counter_repo.build_namespace_key(request.naan, settings.minter_shoulder)

    max_retries = 8
    full_ark = ""
    for attempt in range(max_retries):
        try:
            # Allocate next sequence number for this namespace.
            counter_value = counter_repo.allocate_next(namespace_key)

            # Build deterministic ARK from allocated counter.
            full_ark = mint_ark_id(
                naan=request.naan,
                counter=counter_value,
                shoulder=settings.minter_shoulder,
                min_length=settings.minter_noid_length,
                checkdigit=settings.minter_noid_checkdigit,
            )

            # Extract name from ARK (format: ark:{naan}/{name})
            name = full_ark.split("/", 1)[1] if "/" in full_ark else ""

            # Savepoint isolates ARK insert errors without rolling back allocated counter.
            with db.begin_nested():
                db_ark = ark_repo.create_reserved(
                    naan=request.naan,
                    name=name,
                    authority_id=request.authority_id,
                    client_item_id=None,
                )
                db.flush()

            db.commit()
            db.refresh(db_ark)

            logger.info(f"Reserved ARK: {db_ark.ark} for {request.authority_id}")

            return ARKResponse(
                ark=db_ark.ark,
                state=db_ark.state,
                target=db_ark.target,
            )

        except IntegrityError as e:
            # Uniqueness collisions are retried with a new counter value.
            if _is_unique_violation(e):
                logger.warning(
                    f"ARK insert unique collision for {full_ark} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                if attempt < max_retries - 1:
                    # Persist consumed counter allocation so retries advance.
                    db.commit()
                    continue

                db.commit()
                raise HTTPException(
                    status_code=409,
                    detail="Generated ARK already exists after retries. Please retry.",
                )

            db.rollback()
            logger.error(f"Database integrity error: {e}")
            raise HTTPException(status_code=500, detail=f"Database integrity error: {str(e.orig)}")
        except Exception as e:
            db.rollback()
            logger.error(f"Error creating ARK: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/batch",
    response_model=ARKBatchResponse,
    response_model_exclude_none=True,
    summary="Batch Reserve ARKs",
    description="Reserve multiple ARKs, optionally with initial targets.",
)
async def batch_reserve_ark(
    request: ReserveBatchRequest,
    identity: dict = Depends(require_authority_identity),
    corelib_client: DARKCoreClient = Depends(get_corelib_client),
    db: Session = Depends(get_db),
) -> ARKBatchResponse:
    """
    Reserve multiple ARKs with individual error handling.
    """
    enforce_authority_match(identity, request.authority_id)

    # Validate authorization once for the batch (with cache)
    if not check_authorization_cached(corelib_client, request.authority_id, request.naan):
        raise HTTPException(
            status_code=403,
            detail=f"Authority {request.authority_id} not authorized for NAAN {request.naan}"
        )
    
    settings = get_settings()
    ark_repo = ARKRepository(db)
    counter_repo = NoidCounterRepository(db)
    namespace_key = counter_repo.build_namespace_key(request.naan, settings.minter_shoulder)
    results = []
    errors = []

    max_retries_per_item = 8

    for idx, item in enumerate(request.items):
        item_saved = False
        last_error = None

        for attempt in range(max_retries_per_item):
            try:
                counter_value = counter_repo.allocate_next(namespace_key)
                full_ark = mint_ark_id(
                    naan=request.naan,
                    counter=counter_value,
                    shoulder=settings.minter_shoulder,
                    min_length=settings.minter_noid_length,
                    checkdigit=settings.minter_noid_checkdigit,
                )

                name = full_ark.split("/", 1)[1] if "/" in full_ark else ""

                with db.begin_nested():
                    db_ark = ark_repo.create_reserved(
                        naan=request.naan,
                        name=name,
                        authority_id=request.authority_id,
                        client_item_id=item.client_item_id,
                    )
                    db.flush()

                results.append(
                    ARKResponse(
                        ark=db_ark.ark,
                        state=db_ark.state,
                        target=db_ark.target,
                        client_item_id=db_ark.client_item_id,
                    )
                )
                item_saved = True
                break
            except IntegrityError as e:
                last_error = e
                if _is_unique_violation(e):
                    logger.warning(
                        f"Batch ARK unique collision for item {idx} "
                        f"(client_item_id={item.client_item_id}) "
                        f"attempt {attempt + 1}/{max_retries_per_item}"
                    )
                    continue
                break
            except Exception as e:
                last_error = e
                break

        if not item_saved:
            errors.append(
                {
                    "client_item_id": item.client_item_id,
                    "error": str(last_error) if last_error else "Unknown error",
                    "index": idx,
                }
            )

    try:
        db.commit()
        logger.info(f"Batch reserved {len(results)} ARKs for {request.authority_id} ({len(errors)} errors)")
    except Exception as e:
        db.rollback()
        logger.error(f"Error committing batch: {e}")
        raise HTTPException(status_code=500, detail=f"Error committing batch: {str(e)}")

    return ARKBatchResponse(
        results=results,
        errors=errors if errors else None,
    )


@router.get(
    "/{ark:path}",
    response_model=ARKResponse,
    response_model_exclude_none=True,
    summary="Get ARK",
    description="Retrieve ARK details.",
)
async def get_ark(
    ark: str = Path(..., description="Full ARK identifier (e.g. ark:/12345/xyz)"),
    cert_info: dict = Depends(require_mtls),
    corelib_client: DARKCoreClient = Depends(get_corelib_client),
    db: Session = Depends(get_db),
) -> ARKResponse:
    """
    Get ARK details from database or blockchain.
    """
    # Parse ARK to get NAAN/Name
    from app.repositories.ark_repository import parse_ark
    try:
        naan, name = parse_ark(ark)
        _validate_ark_checkdigit_if_enabled(naan, name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ARK format. Expected ark:NAAN/suffix")
    
    # 1. Try database first
    ark_repo = ARKRepository(db)
    db_ark = ark_repo.get_by_ark(ark)
    
    if db_ark:
        db_metadata = ark_repo.get_metadata_by_ark_id(db_ark.id)
        metadata_cid = db_metadata.level1_cid if db_metadata else None
        metadata_schema = db_metadata.original_schema if db_metadata else None
        minimal_metadata = _extract_minimal_metadata(
            db_metadata.level1_json if db_metadata else None
        )

        # Found in database
        if db_ark.state in [ARKState.RESERVED, ARKState.DRAFT]:
            # Return from DB directly (not yet on blockchain)
            return ARKResponse(
                ark=db_ark.ark,
                state=db_ark.state,
                target=db_ark.target,
                metadata_cid=metadata_cid,
                metadata_schema=metadata_schema,
                minimal_metadata=minimal_metadata,
            )
        elif db_ark.state == ARKState.PUBLISHED:
            # Combine DB metadata with blockchain data
            try:
                info = corelib_client.get_ark(naan, name)
                return ARKResponse(
                    ark=ark,
                    state=ARKState.PUBLISHED,
                    target=info.url,  # From blockchain
                    metadata_cid=info.cid,  # From blockchain
                    metadata_schema=metadata_schema,  # From DB
                    minimal_metadata=minimal_metadata,
                    level1_cid=db_metadata.level1_cid if db_metadata else None,
                    level2_cid=db_metadata.original_cid if db_metadata else None,
                    client_item_id=db_ark.client_item_id,
                )
            except Exception as e:
                # Blockchain query failed, return DB data
                logger.warning(f"Blockchain query failed for {ark}: {e}")
                return ARKResponse(
                    ark=db_ark.ark,
                    state=db_ark.state,
                    target=db_ark.target,
                    metadata_cid=metadata_cid,
                    metadata_schema=metadata_schema,
                    minimal_metadata=minimal_metadata,
                    level1_cid=db_metadata.level1_cid if db_metadata else None,
                    level2_cid=db_metadata.original_cid if db_metadata else None,
                    client_item_id=db_ark.client_item_id,
                )
        elif db_ark.state == ARKState.TOMBSTONE:
            # Tombstoned
            return ARKResponse(
                ark=db_ark.ark,
                state=db_ark.state,
                target=db_ark.target,
                metadata_cid=metadata_cid,
                metadata_schema=metadata_schema,
                minimal_metadata=minimal_metadata,
            )
    
    # 2. Fallback: Query blockchain (ARK might have been created outside this API)
    if not corelib_client.ark_exists(naan, name):
        raise HTTPException(status_code=404, detail="ARK not found")
    
    info = corelib_client.get_ark(naan, name)
    
    return ARKResponse(
        ark=ark,
        state=ARKState.PUBLISHED,
        target=info.url,
        metadata_cid=info.cid,
    )


@router.put(
    "/{ark:path}",
    response_model=ARKResponse,
    response_model_exclude_none=True,
    summary="Update Metadata & Move to DRAFT",
    description="Submit metadata and target. Transitions RESERVED -> DRAFT (ready for publication).",
)
async def update_ark_metadata(
    request: UpdateARKMetadataRequest,
    ark: str = Path(..., description="Full ARK identifier"),
    identity: dict = Depends(require_authority_identity),
    corelib_client: DARKCoreClient = Depends(get_corelib_client),
    db: Session = Depends(get_db),
    storage: MetadataStorage = Depends(get_metadata_storage),
) -> ARKResponse:
    """
    Update metadata and transition to DRAFT state.
    
    Stores metadata content immediately via storage backend.
    Does NOT publish to blockchain. That happens in a separate background process.
    """
    del storage
    enforce_authority_match(identity, request.authority_id)

    # Parse ARK using repository helper
    from app.repositories.ark_repository import parse_ark
    try:
        naan, name = parse_ark(ark)
        _validate_ark_checkdigit_if_enabled(naan, name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ARK format")
    
    # 1. Get ARK from database
    ark_repo = ARKRepository(db)
    db_ark = ark_repo.get_by_ark(ark)
    known_chain_cid: Optional[str] = None

    # 2. If local record is missing, import from blockchain as PUBLISHED.
    if not db_ark:
        if not corelib_client.ark_exists(naan, name):
            raise HTTPException(status_code=404, detail="ARK not found")

        try:
            chain_info = corelib_client.get_ark(naan, name)
            known_chain_cid = getattr(chain_info, "cid", None)
        except Exception as e:
            logger.error(f"Failed to fetch ARK from blockchain for local import {ark}: {e}")
            raise HTTPException(status_code=502, detail="Failed to read ARK from blockchain")

        try:
            authority_info = corelib_client.get_authority_by_uuid(request.authority_id)
        except Exception as e:
            logger.error(f"Failed to resolve authority {request.authority_id} during import of {ark}: {e}")
            raise HTTPException(status_code=502, detail="Failed to verify importing authority")

        if not getattr(authority_info, "active", False):
            raise HTTPException(
                status_code=403,
                detail=f"Authority {request.authority_id} is not active",
            )

        chain_owner = _normalize_wallet(getattr(chain_info, "owner", None))
        authority_wallet = _normalize_wallet(getattr(authority_info, "wallet_address", None))
        if not chain_owner or not authority_wallet or chain_owner != authority_wallet:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Authority {request.authority_id} does not own on-chain ARK {ark}"
                ),
            )

        try:
            with db.begin_nested():
                db_ark = ark_repo.create_published_import(
                    naan=naan,
                    name=name,
                    authority_id=request.authority_id,
                    target=getattr(chain_info, "url", None),
                )
                db.flush()
        except IntegrityError:
            # Lost race importing same on-chain ARK; fetch the row created by concurrent request.
            db_ark = ark_repo.get_by_ark(ark)
            if not db_ark:
                db.rollback()
                raise HTTPException(status_code=500, detail="Failed to import ARK from blockchain")

    # 3. Tombstone records cannot be modified.
    if db_ark.state == ARKState.TOMBSTONE:
        raise HTTPException(status_code=409, detail="ARK is tombstoned and cannot be updated")

    # 4. Validate ownership
    if db_ark.authority_id != request.authority_id:
        raise HTTPException(
            status_code=403,
            detail=f"Authority {request.authority_id} does not own this ARK"
        )
    
    # 5. Validate minimal metadata schema (Level 1)
    # request.minimal_metadata is a dict validated against Level1Metadata.
    try:
        # Auto-fill system fields that the client shouldn't strictly need to provide
        l1_data = request.minimal_metadata.copy()
        
        # Ensure ark matches the URL path
        l1_data["ark"] = ark
        
        # Set original_metadata reference (CID is null until worker processes it)
        l1_data["original_metadata"] = {
            "schema": request.metadata_schema,
            "media_type": request.metadata_media_type.split(";", 1)[0].strip().lower(),
            "cid": None
        }
        
        # Validate
        l1_model = Level1Metadata(**l1_data)
        
    except Exception as e:
        logger.error(f"Level 1 metadata validation failed: {e}")
        raise HTTPException(
            status_code=422,
            detail=f"Level 1 metadata validation failed: {e}"
        )

    # 6. Store metadata in DB (Level 1 + Level 2)
    # We do NOT store to IPFS here. The worker will do that.
    level1_json = l1_model.model_dump(mode="json", by_alias=True)
    try:
        original_media_type = request.metadata_media_type.split(";", 1)[0].strip().lower()
        db_metadata = ark_repo.create_or_update_metadata(
            ark_record_id=db_ark.id,
            level1_json=level1_json,
            original_content=request.original_metadata,
            original_schema=request.metadata_schema,
            original_media_type=original_media_type,
        )
    except Exception as e:
        logger.error(f"Failed to store metadata in DB for {ark}: {e}")
        raise HTTPException(status_code=500, detail="Database error storing metadata")
    
    # 7. Apply state-aware transition/update.
    # Note: We no longer pass metadata_cid/format to these methods.
    try:
        if db_ark.state == ARKState.RESERVED:
            # New local ARK: pending create_ark.
            db_ark = ark_repo.update_to_draft(
                ark=ark,
                target=request.target,
            )
        elif db_ark.state == ARKState.DRAFT:
            # Idempotent overwrite of pending create_ark payload.
            db_ark = ark_repo.update_draft_content(
                ark=ark,
                target=request.target,
            )
        else:
            # Existing on-chain ARK: queue update_ark via worker.
            db_ark = ark_repo.update_to_update(
                ark=ark,
                target=request.target,
            )

        db.commit()
        db.refresh(db_ark)
        db.refresh(db_metadata)
        
        logger.info(f"ARK {ark} updated to state {db_ark.state} (schema: {request.metadata_schema})")
        
        return ARKResponse(
            ark=db_ark.ark,
            state=db_ark.state,
            target=db_ark.target,
            metadata_cid=db_metadata.level1_cid or known_chain_cid,
            # Return new two-level metadata fields
            metadata_schema=db_metadata.original_schema,
            minimal_metadata=level1_json,
            level1_cid=db_metadata.level1_cid,
            level2_cid=db_metadata.original_cid,
            client_item_id=db_ark.client_item_id,
        )
            
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating ARK {ark}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{ark:path}",
    summary="Deactivate ARK (Tombstone)",
    description="Mark ARK as deleted/tombstone. Cannot be undone.",
)
async def delete_ark(
    ark: str = Path(..., description="Full ARK identifier"),
    identity: dict = Depends(require_authority_identity),
    corelib_client: DARKCoreClient = Depends(get_corelib_client),
    db: Session = Depends(get_db),
) -> None:
    """
    Tombstone an ARK (soft delete).
    """
    # Parse ARK
    from app.repositories.ark_repository import parse_ark
    try:
        naan, name = parse_ark(ark)
        _validate_ark_checkdigit_if_enabled(naan, name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ARK format")
    
    # Get ARK from database
    ark_repo = ARKRepository(db)
    db_ark = ark_repo.get_by_ark(ark)
    
    if not db_ark:
        raise HTTPException(status_code=404, detail="ARK not found")

    authenticated_authority = enforce_authority_match(identity, db_ark.authority_id)
    logger.info(f"Authenticated authority {authenticated_authority} requested tombstone for {ark}")
    
    # Update to TOMBSTONE state
    try:
        ark_repo.update_to_tombstone(ark)
        db.commit()
        
        logger.info(f"ARK {ark} marked as TOMBSTONE")
        
        # TODO: dark-core-lib needs delete_ark or tombstone_ark method
        # Currently not supported on-chain.
        logger.warning(f"Tombstone state saved in DB, but not yet propagated to blockchain")
        
        return None
    
    except Exception as e:
        db.rollback()
        logger.error(f"Error tombstoning ARK {ark}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
