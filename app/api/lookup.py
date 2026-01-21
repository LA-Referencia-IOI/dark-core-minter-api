"""
Lookup endpoints.

Provides URL-based ARK lookup (Section 8.4.1 of spec).
"""

import logging
from fastapi import APIRouter, Depends

from dark_orchestrator import DARKOrchestrator
from dark_orchestrator.exceptions import ARKError

from app.dependencies import get_orchestrator
from app.middleware.auth import require_mtls
from app.models.requests import URLLookupRequest
from app.models.responses import URLLookupResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/url",
    response_model=URLLookupResponse,
    summary="Lookup ARK by URL",
    description="Check if a dARK identifier exists for the given URL.",
)
async def lookup_by_url(
    request: URLLookupRequest,
    cert_info: dict = Depends(require_mtls),
    orchestrator: DARKOrchestrator = Depends(get_orchestrator),
) -> URLLookupResponse:
    """
    Lookup an existing ARK by URL.
    
    This is the primary deduplication endpoint used by Minters to check
    if an identifier already exists before creating a new one.
    
    Returns:
        URLLookupResponse indicating whether ARK exists and its details
    """
    logger.info(f"Looking up URL: {request.url}")
    
    # Note: The current orchestrator doesn't have a direct URL lookup.
    # We need to iterate through ARKs or add this to the smart contract.
    # For now, we'll return not found as a placeholder.
    # TODO: Implement URL-based lookup in smart contract or add index.
    
    # Placeholder implementation - in production, this would query
    # an index or the blockchain for URL mappings
    return URLLookupResponse(
        exists=False,
        dark_id=None,
        naan=None,
        name=None,
    )
