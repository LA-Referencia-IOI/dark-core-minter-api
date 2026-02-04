"""
Main API Router.

Aggregates all endpoint routers.
"""

from fastapi import APIRouter

from app.api.authority import router as authority_router
from app.api.arks import router as arks_router

api_router = APIRouter()

# Include all sub-routers
api_router.include_router(authority_router, prefix="/authority", tags=["Authority"])
api_router.include_router(arks_router, prefix="/arks", tags=["ARKs"])
