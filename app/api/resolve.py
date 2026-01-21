"""
Resolve endpoints.

Provides ARK resolution and retrieval.
"""

import logging
from fastapi import APIRouter, Depends, Path

from dark_orchestrator import DARKOrchestrator
from dark_orchestrator.exceptions import ARKError

from app.dependencies import get_orchestrator
from app.middleware.auth import require_mtls
from app.models.responses import (
    ARKResolveResponse,
    ARKInfoResponse,
    ARKExistsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/resolve/{naan}/{name}",
    response_model=ARKResolveResponse,
    summary="Resolve ARK to URL",
    description="Get the URL that an ARK resolves to.",
)
async def resolve_ark(
    naan: str = Path(..., description="NAAN"),
    name: str = Path(..., description="Name within NAAN"),
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
) -> ARKResolveResponse:
    """
    Resolve an ARK identifier to its URL and CID.
    
    Used by Resolver nodes to perform ARK resolution.
    """
    logger.info(f"Resolving ark:/{naan}/{name}")
    
    ark_info = orchestrator.get_ark(naan, name)
    
    return ARKResolveResponse(
        ark_id=ark_info.ark_id,
        url=ark_info.url,
        cid=ark_info.cid,
    )


@router.get(
    "/ark/{naan}/{name}",
    response_model=ARKInfoResponse,
    summary="Get full ARK info",
    description="Get complete information about an ARK.",
)
async def get_ark_info(
    naan: str = Path(..., description="NAAN"),
    name: str = Path(..., description="Name within NAAN"),
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
) -> ARKInfoResponse:
    """
    Get full ARK information including timestamps and owner.
    """
    logger.info(f"Getting ARK info: ark:/{naan}/{name}")
    
    ark_info = orchestrator.get_ark(naan, name)
    
    return ARKInfoResponse(
        ark_id=ark_info.ark_id,
        naan=ark_info.naan,
        name=ark_info.name,
        url=ark_info.url,
        cid=ark_info.cid,
        owner=ark_info.owner,
        created_at=ark_info.created_at,
        updated_at=ark_info.updated_at,
    )


@router.get(
    "/ark/{naan}/{name}/exists",
    response_model=ARKExistsResponse,
    summary="Check if ARK exists",
    description="Lightweight check for ARK existence.",
)
async def ark_exists(
    naan: str = Path(..., description="NAAN"),
    name: str = Path(..., description="Name within NAAN"),
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
) -> ARKExistsResponse:
    """
    Check if an ARK exists without fetching full details.
    """
    exists = orchestrator.ark_exists(naan, name)
    
    return ARKExistsResponse(
        exists=exists,
        ark_id=f"ark:/{naan}/{name}",
    )
