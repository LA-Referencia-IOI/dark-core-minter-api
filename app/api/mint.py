"""
Mint endpoints.

Provides batch ARK minting (Section 8.4.2 of spec).
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException

from dark_orchestrator import DARKOrchestrator
from dark_orchestrator.exceptions import ARKError, AuthorityError, TransactionError

from app.config import get_settings, Settings
from app.dependencies import get_orchestrator
from app.middleware.auth import require_mtls
from app.models.requests import BatchMintRequest, MintItem
from app.models.responses import BatchMintResponse, MintItemResult

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/batch",
    response_model=BatchMintResponse,
    summary="Batch mint ARKs",
    description="Persist one or more ARK identifiers on-chain.",
)
async def batch_mint(
    request: BatchMintRequest,
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
    settings: Settings = Depends(get_settings),
) -> BatchMintResponse:
    """
    Mint multiple ARKs in a single batch.
    
    Each item is processed independently - failures for one item
    do not prevent others from succeeding.
    
    Returns:
        BatchMintResponse with per-item results
    """
    # Validate batch size
    if len(request.items) > settings.batch_size_limit:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(request.items)} exceeds limit of {settings.batch_size_limit}",
        )
    
    logger.info(f"Processing batch mint with {len(request.items)} items")
    
    results: list[MintItemResult] = []
    success_count = 0
    error_count = 0
    
    for item in request.items:
        result = await _process_mint_item(item, orchestrator)
        results.append(result)
        
        if result.status == "success":
            success_count += 1
        else:
            error_count += 1
    
    # Determine overall status
    if error_count == 0:
        status = "ok"
    elif success_count == 0:
        status = "error"
    else:
        status = "partial_success"
    
    logger.info(f"Batch mint complete: {success_count} success, {error_count} errors")
    
    return BatchMintResponse(
        status=status,
        results=results,
    )


async def _process_mint_item(
    item: MintItem,
    orchestrator: DARKOrchestrator,
) -> MintItemResult:
    """
    Process a single mint item.
    
    Handles all error cases and returns appropriate result.
    """
    try:
        # Check if ARK already exists
        if orchestrator.ark_exists(item.naan, item.name):
            logger.info(f"ARK already exists: ark:/{item.naan}/{item.name}")
            existing_ark = orchestrator.get_ark(item.naan, item.name)
            return MintItemResult(
                dark_id=existing_ark.ark_id,
                name=item.name,
                status="already_exists",
                error="ARK already exists with this NAAN/name combination",
            )
        
        # Create the ARK
        ark_info = orchestrator.create_ark(
            uuid=item.authority_id,
            naan=item.naan,
            name=item.name,
            url=item.url,
            cid=item.cid,
        )
        
        logger.info(f"Created ARK: {ark_info.ark_id}")
        
        return MintItemResult(
            dark_id=ark_info.ark_id,
            name=item.name,
            status="success",
            transaction_ref=None,  # Could extract from receipt if needed
        )
        
    except AuthorityError as e:
        logger.warning(f"Authorization error for {item.name}: {e}")
        return MintItemResult(
            name=item.name,
            status="authorization_error",
            error=str(e),
        )
    
    except TransactionError as e:
        logger.error(f"Transaction error for {item.name}: {e}")
        return MintItemResult(
            name=item.name,
            status="transient_error",
            error=str(e),
        )
    
    except ARKError as e:
        logger.error(f"ARK error for {item.name}: {e}")
        return MintItemResult(
            name=item.name,
            status="error",
            error=str(e),
        )
    
    except Exception as e:
        logger.exception(f"Unexpected error for {item.name}: {e}")
        return MintItemResult(
            name=item.name,
            status="error",
            error=f"Unexpected error: {str(e)}",
        )
