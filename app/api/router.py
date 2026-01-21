"""
Main API Router.

Aggregates all endpoint routers.
"""

from fastapi import APIRouter

from app.api.mint import router as mint_router
from app.api.authority import router as authority_router

api_router = APIRouter()

# Include all sub-routers
api_router.include_router(mint_router, prefix="/mint", tags=["Mint"])
api_router.include_router(authority_router, prefix="/authority", tags=["Authority"])
