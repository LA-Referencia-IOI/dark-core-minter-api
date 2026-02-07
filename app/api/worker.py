"""
Worker monitoring endpoints.
"""

import logging
from typing import Dict, Any

from fastapi import APIRouter, Request, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Worker"])


@router.get("/status", response_model=Dict[str, Any])
async def get_worker_status(request: Request):
    """
    Get ARK publisher worker status and statistics.
    
    Returns:
        Worker statistics including:
        - enabled: Whether worker is running
        - stats: Processing statistics
        - recent_errors: Recent error messages
    """
    if not hasattr(request.app.state, "ark_publisher"):
        return {
            "enabled": False,
            "message": "Worker is disabled or not initialized"
        }
    
    try:
        publisher = request.app.state.ark_publisher
        stats = publisher.get_stats()
        
        return {
            "enabled": True,
            "stats": {
                "total_processed": stats["total_processed"],
                "total_succeeded": stats["total_succeeded"],
                "total_failed": stats["total_failed"],
                "total_permanent_failures": stats["total_permanent_failures"],
                "last_run_at": stats["last_run_at"],
                "last_run_duration": stats["last_run_duration"],
            },
            "recent_errors": stats["recent_errors"],
        }
    except Exception as e:
        logger.error(f"Error retrieving worker status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get worker status: {e}")
