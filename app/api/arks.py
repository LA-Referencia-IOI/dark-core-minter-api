"""
ARK Native REST Endpoints.

Implements the ARK lifecycle: reserved -> draft -> published -> tombstone.
"""

import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Path, Body
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from dark_orchestrator import DARKOrchestrator
from dark_orchestrator.exceptions import ARKError, AuthorityError

from app.dependencies import get_orchestrator, get_db, get_metadata_storage
from app.storage.base import MetadataStorage
from app.storage.exceptions import StorageError
from app.middleware.auth import require_mtls
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
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> ARKResponse:
    """
    Reserve a single ARK.
    
    Generates a unique ID and persists in database.
    """
    # 1. Validate authorization (with cache)
    if not check_authorization_cached(orchestrator, request.authority_id, request.naan):
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
                    alternate_identifiers=request.alternate_identifiers,
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
                metadata_cid=db_ark.metadata_cid,
                alternate_identifiers=db_ark.alternate_identifiers,
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
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> ARKBatchResponse:
    """
    Reserve multiple ARKs with individual error handling.
    """
    # Validate authorization once for the batch (with cache)
    if not check_authorization_cached(orchestrator, request.authority_id, request.naan):
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
                        alternate_identifiers=item.alternate_identifiers,
                        client_item_id=item.client_item_id,
                    )
                    db.flush()

                results.append(
                    ARKResponse(
                        ark=db_ark.ark,
                        state=db_ark.state,
                        target=db_ark.target,
                        metadata_cid=db_ark.metadata_cid,
                        alternate_identifiers=db_ark.alternate_identifiers,
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
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
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
        # Found in database
        if db_ark.state in [ARKState.RESERVED, ARKState.DRAFT]:
            # Return from DB directly (not yet on blockchain)
            return ARKResponse(
                ark=db_ark.ark,
                state=db_ark.state,
                target=db_ark.target,
                metadata_cid=db_ark.metadata_cid,
                metadata_format=db_ark.metadata_format,
                alternate_identifiers=db_ark.alternate_identifiers,
            )
        elif db_ark.state == ARKState.PUBLISHED:
            # Combine DB metadata with blockchain data
            try:
                info = orchestrator.get_ark(naan, name)
                return ARKResponse(
                    ark=ark,
                    state=ARKState.PUBLISHED,
                    target=info.url,  # From blockchain
                    metadata_cid=info.cid,  # From blockchain
                    metadata_format=db_ark.metadata_format,  # From DB
                    alternate_identifiers=db_ark.alternate_identifiers,  # From DB
                )
            except Exception as e:
                # Blockchain query failed, return DB data
                logger.warning(f"Blockchain query failed for {ark}: {e}")
                return ARKResponse(
                    ark=db_ark.ark,
                    state=db_ark.state,
                    target=db_ark.target,
                    metadata_cid=db_ark.metadata_cid,
                    metadata_format=db_ark.metadata_format,
                    alternate_identifiers=db_ark.alternate_identifiers,
                )
        elif db_ark.state == ARKState.TOMBSTONE:
            # Tombstoned
            return ARKResponse(
                ark=db_ark.ark,
                state=db_ark.state,
                target=db_ark.target,
                metadata_cid=db_ark.metadata_cid,
                metadata_format=db_ark.metadata_format,
                alternate_identifiers=db_ark.alternate_identifiers,
            )
    
    # 2. Fallback: Query blockchain (ARK might have been created outside this API)
    if not orchestrator.ark_exists(naan, name):
        raise HTTPException(status_code=404, detail="ARK not found")
    
    info = orchestrator.get_ark(naan, name)
    
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
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
    storage: MetadataStorage = Depends(get_metadata_storage),
) -> ARKResponse:
    """
    Update metadata and transition to DRAFT state.
    
    Stores metadata content immediately via storage backend.
    Does NOT publish to blockchain. That happens in a separate background process.
    """
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

    # 2. If local record is missing, import from blockchain as PUBLISHED.
    if not db_ark:
        if not orchestrator.ark_exists(naan, name):
            raise HTTPException(status_code=404, detail="ARK not found")

        try:
            chain_info = orchestrator.get_ark(naan, name)
        except Exception as e:
            logger.error(f"Failed to fetch ARK from blockchain for local import {ark}: {e}")
            raise HTTPException(status_code=502, detail="Failed to read ARK from blockchain")

        try:
            with db.begin_nested():
                db_ark = ark_repo.create_published_import(
                    naan=naan,
                    name=name,
                    authority_id=request.authority_id,
                    target=getattr(chain_info, "url", None),
                    metadata_cid=getattr(chain_info, "cid", None),
                    metadata_format=None,
                    alternate_identifiers=None,
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
    
    # 5. Store metadata content and get CID
    try:
        metadata_cid = storage.store_metadata(request.metadata, request.metadata_format)
        logger.info(f"Stored metadata for {ark}, CID: {metadata_cid}")
    except StorageError as e:
        logger.error(f"Metadata storage failed for {ark}: {e}")
        raise HTTPException(status_code=500, detail=f"Metadata storage failed: {e}")
    
    # 6. Apply state-aware transition/update.
    try:
        if db_ark.state == ARKState.RESERVED:
            # New local ARK: pending create_ark.
            db_ark = ark_repo.update_to_draft(
                ark=ark,
                target=request.target,
                metadata_cid=metadata_cid,
                metadata_format=request.metadata_format,
                alternate_identifiers=request.alternate_identifiers,
            )
        elif db_ark.state == ARKState.DRAFT:
            # Idempotent overwrite of pending create_ark payload.
            db_ark = ark_repo.update_draft_content(
                ark=ark,
                target=request.target,
                metadata_cid=metadata_cid,
                metadata_format=request.metadata_format,
                alternate_identifiers=request.alternate_identifiers,
            )
        else:
            # Existing on-chain ARK: queue update_ark via worker.
            db_ark = ark_repo.update_to_update(
                ark=ark,
                target=request.target,
                metadata_cid=metadata_cid,
                metadata_format=request.metadata_format,
                alternate_identifiers=request.alternate_identifiers,
            )

        db.commit()
        db.refresh(db_ark)
        
        logger.info(f"ARK {ark} updated to state {db_ark.state} (format: {request.metadata_format})")
        
        return ARKResponse(
            ark=db_ark.ark,
            state=db_ark.state,
            target=db_ark.target,
            metadata_cid=db_ark.metadata_cid,
            metadata_format=db_ark.metadata_format,
            alternate_identifiers=db_ark.alternate_identifiers,
        )
    
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
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
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
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
    
    # Validate ownership (extract from cert_info)
    # Note: cert_info comes from mTLS middleware, contains certificate subject info
    # For now, we'll allow any authenticated user to tombstone (adjust as needed)
    # TODO: Extract authority_id from cert and validate ownership
    
    # Update to TOMBSTONE state
    try:
        ark_repo.update_to_tombstone(ark)
        db.commit()
        
        logger.info(f"ARK {ark} marked as TOMBSTONE")
        
        # TODO: Orchestrator needs delete_ark or tombstone_ark method
        # Currently not supported on-chain.
        logger.warning(f"Tombstone state saved in DB, but not yet propagated to blockchain")
        
        return None
    
    except Exception as e:
        db.rollback()
        logger.error(f"Error tombstoning ARK {ark}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
