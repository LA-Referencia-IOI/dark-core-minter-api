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
from app.utils.noid import mint_ark_id
from app.utils.auth_cache import check_authorization_cached
from app.models.states import ARKState
from app.repositories import ARKRepository
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
    
    # 2. Generate ID and Persist (with retry handling)
    settings = get_settings()
    ark_repo = ARKRepository(db)
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Generate unique name (mint_ark_id returns full ark, we extract name)
            full_ark = mint_ark_id(request.naan, settings.minter_shoulder)
            
            # Extract name from ARK (format: ark:{naan}/{name})
            name = full_ark.split("/", 1)[1] if "/" in full_ark else ""
            
            # Persist reservation in database (ark is computed property)
            db_ark = ark_repo.create_reserved(
                naan=request.naan,
                name=name,
                authority_id=request.authority_id,
                alternate_identifiers=request.alternate_identifiers,
                client_item_id=None,
            )
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
            db.rollback()
            # Log exact cause (UNIQUE, NOT NULL, CHECK, etc.)
            logger.error(f"IntegrityError detailed: {e.orig}")
            logger.warning(f"ARK insert failed: {full_ark} (attempt {attempt + 1}/{max_retries})")
            
            if attempt == max_retries - 1:
                # Determine if it's actually a collision or something else
                if "UNIQUE" in str(e.orig) or "unique" in str(e.orig).lower():
                     raise HTTPException(status_code=409, detail="Generated ARK already exists after retries. Please retry.")
                else:
                     # It's a schema violation (e.g. valid checks, not nulls)
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
    results = []
    errors = []
    
    for idx, item in enumerate(request.items):
        try:
            # Isolate each item in a savepoint so one failure does not rollback
            # already successful items in the same batch.
            with db.begin_nested():
                # 1. Generate ID
                full_ark = mint_ark_id(request.naan, settings.minter_shoulder)

                # 2. Extract name (format: ark:{naan}/{name})
                name = full_ark.split("/", 1)[1] if "/" in full_ark else ""

                # 3. Create reserved record (ark is computed property)
                db_ark = ark_repo.create_reserved(
                    naan=request.naan,
                    name=name,
                    authority_id=request.authority_id,
                    alternate_identifiers=item.alternate_identifiers,
                    client_item_id=item.client_item_id,
                )
                db.flush()

                batch_result = ARKResponse(
                    ark=db_ark.ark,
                    state=db_ark.state,
                    target=db_ark.target,
                    metadata_cid=db_ark.metadata_cid,
                    alternate_identifiers=db_ark.alternate_identifiers,
                    client_item_id=db_ark.client_item_id,
                )

            results.append(batch_result)

        except Exception as e:
            error_msg = str(e)
            logger.warning(
                f"Error creating ARK for item {idx} "
                f"(client_item_id={item.client_item_id}): {error_msg}"
            )
            errors.append({
                "client_item_id": item.client_item_id,
                "error": error_msg,
                "index": idx,
            })
    
    # Commit all successful items
    if len(results) > 0:
        try:
            db.commit()
            logger.info(f"Batch reserved {len(results)} ARKs for {request.authority_id} ({len(errors)} errors)")
        except Exception as e:
            db.rollback()
            logger.error(f"Error committing batch: {e}")
            raise HTTPException(status_code=500, detail=f"Error committing batch: {str(e)}")
    else:
        db.rollback()
    
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
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ARK format")
    
    # 1. Get ARK from database
    ark_repo = ARKRepository(db)
    db_ark = ark_repo.get_by_ark(ark)
    
    if not db_ark:
        raise HTTPException(status_code=404, detail="ARK not found")
    
    # 2. Validate state (must be RESERVED)
    if db_ark.state != ARKState.RESERVED:
        raise HTTPException(
            status_code=400,
            detail=f"ARK must be in RESERVED state to update, currently {db_ark.state}"
        )
    
    # 3. Validate ownership
    if db_ark.authority_id != request.authority_id:
        raise HTTPException(
            status_code=403,
            detail=f"Authority {request.authority_id} does not own this ARK"
        )
    
    # 4. Store metadata content and get CID
    try:
        metadata_cid = storage.store_metadata(request.metadata, request.metadata_format)
        logger.info(f"Stored metadata for {ark}, CID: {metadata_cid}")
    except StorageError as e:
        logger.error(f"Metadata storage failed for {ark}: {e}")
        raise HTTPException(status_code=500, detail=f"Metadata storage failed: {e}")
    
    # 5. Update to DRAFT with CID
    try:
        db_ark = ark_repo.update_to_draft(
            ark=ark,
            target=request.target,
            metadata_cid=metadata_cid,
            metadata_format=request.metadata_format,
            alternate_identifiers=request.alternate_identifiers,
        )
        db.commit()
        db.refresh(db_ark)
        
        logger.info(f"ARK {ark} updated to DRAFT state (format: {request.metadata_format})")
        
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
