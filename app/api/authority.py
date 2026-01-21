"""
Authority endpoints.

Provides authority information and authorization checks.
"""

import logging
from fastapi import APIRouter, Depends, Path

from dark_orchestrator import DARKOrchestrator
from dark_orchestrator.exceptions import AuthorityError

from app.dependencies import get_orchestrator
from app.middleware.auth import require_mtls
from app.models.responses import (
    AuthorityResponse,
    AuthorityNAANsResponse,
    AuthorityAuthorizedResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/{uuid}",
    response_model=AuthorityResponse,
    summary="Get authority info",
    description="Get information about a registered authority.",
)
async def get_authority(
    uuid: str = Path(..., description="Authority UUID"),
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
) -> AuthorityResponse:
    """
    Get authority information by UUID.
    """
    logger.info(f"Getting authority: {uuid}")
    
    authority = orchestrator.get_authority_by_uuid(uuid)
    
    return AuthorityResponse(
        uuid=authority.uuid,
        wallet_address=authority.wallet_address,
        naans=authority.naans,
        active=authority.active,
    )


@router.get(
    "/{uuid}/naans",
    response_model=AuthorityNAANsResponse,
    summary="List authorized NAANs",
    description="Get list of NAANs an authority is authorized to use.",
)
async def get_authority_naans(
    uuid: str = Path(..., description="Authority UUID"),
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
) -> AuthorityNAANsResponse:
    """
    Get list of authorized NAANs for an authority.
    """
    logger.info(f"Getting NAANs for authority: {uuid}")
    
    naans = orchestrator.get_authorized_naans(uuid)
    
    return AuthorityNAANsResponse(
        uuid=uuid,
        naans=naans,
    )


@router.get(
    "/{uuid}/authorized/{naan}",
    response_model=AuthorityAuthorizedResponse,
    summary="Check NAAN authorization",
    description="Check if an authority is authorized for a specific NAAN.",
)
async def check_authority_authorization(
    uuid: str = Path(..., description="Authority UUID"),
    naan: str = Path(..., description="NAAN to check"),
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
) -> AuthorityAuthorizedResponse:
    """
    Check if authority is authorized for a specific NAAN.
    """
    logger.info(f"Checking authorization: {uuid} for NAAN {naan}")
    
    authorized = orchestrator.is_authorized_for_naan(uuid, naan)
    
    return AuthorityAuthorizedResponse(
        uuid=uuid,
        naan=naan,
        authorized=authorized,
    )
