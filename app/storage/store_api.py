"""
HTTP metadata storage backend backed by dark-store-api.

Stores and retrieves raw metadata through the external storage service.
"""

import logging
from urllib.parse import quote

import httpx

from .base import MetadataStorage
from .exceptions import MetadataNotFoundError, StorageError


logger = logging.getLogger(__name__)


FORMAT_TO_CONTENT_TYPE = {
    "json": "application/json",
    "xml": "text/xml",
}


def _infer_format(content_type: str) -> str:
    """Infer metadata format from a MIME type."""
    normalized = content_type.lower()
    if "json" in normalized:
        return "json"
    if "xml" in normalized:
        return "xml"

    # Fallback for unknown/missing types.
    return "json"


def _extract_error_detail(response: httpx.Response) -> str:
    """Extract best-effort human-readable error details from a response."""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail

    text = response.text.strip()
    if text:
        return text
    return f"HTTP {response.status_code}"


class StoreApiMetadataStorage(MetadataStorage):
    """
    Metadata storage implementation that delegates to dark-store-api.

    Uses:
    - POST /v1/store to persist metadata and receive a CID
    - GET /v1/retrieve/{cid} to fetch raw metadata content
    - GET /health for backend health checks
    """

    def __init__(self, base_url: str, timeout_seconds: float = 10.0):
        """
        Initialize the store-api client backend.

        Args:
            base_url: Base URL for dark-store-api (e.g. http://localhost:8002)
            timeout_seconds: Request timeout for storage calls
        """
        if not base_url:
            raise ValueError("Store API base_url cannot be empty")

        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def store_metadata(self, content: str, format: str) -> str:
        """
        Store metadata through dark-store-api and return the resulting CID.
        """
        normalized_format = format.lower()
        content_type = FORMAT_TO_CONTENT_TYPE.get(normalized_format, "application/octet-stream")
        url = f"{self.base_url}/v1/store"

        try:
            response = httpx.post(
                url,
                content=content.encode("utf-8"),
                headers={"Content-Type": content_type},
                timeout=self.timeout_seconds,
            )
        except httpx.RequestError as exc:
            raise StorageError(f"Store API request failed: {exc}") from exc

        if response.status_code != 200:
            detail = _extract_error_detail(response)
            raise StorageError(
                f"Store API store failed ({response.status_code}): {detail}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise StorageError("Store API returned invalid JSON response") from exc

        cid = payload.get("cid") if isinstance(payload, dict) else None
        if not isinstance(cid, str) or not cid:
            raise StorageError("Store API response missing CID")

        return cid

    def get_metadata(self, cid: str) -> tuple[str, str]:
        """
        Retrieve metadata content by CID through dark-store-api.
        """
        encoded_cid = quote(cid, safe="")
        url = f"{self.base_url}/v1/retrieve/{encoded_cid}"

        try:
            response = httpx.get(url, timeout=self.timeout_seconds)
        except httpx.RequestError as exc:
            raise StorageError(f"Store API request failed: {exc}") from exc

        if response.status_code == 404:
            raise MetadataNotFoundError(f"Metadata not found for CID: {cid}")
        if response.status_code != 200:
            detail = _extract_error_detail(response)
            raise StorageError(
                f"Store API retrieve failed ({response.status_code}): {detail}"
            )

        content_type = response.headers.get("content-type", "application/octet-stream")
        content_type = content_type.split(";")[0].strip()
        format = _infer_format(content_type)

        return response.text, format

    def health_check(self) -> bool:
        """
        Check dark-store-api health endpoint.
        """
        url = f"{self.base_url}/health"
        try:
            response = httpx.get(url, timeout=self.timeout_seconds)
        except Exception as exc:
            logger.warning(f"Store API health check failed: {exc}")
            return False

        if response.status_code != 200:
            return False

        try:
            payload = response.json()
        except ValueError:
            return False

        if not isinstance(payload, dict):
            return False

        if "backend_healthy" in payload:
            return bool(payload["backend_healthy"])

        return payload.get("status") == "healthy"
