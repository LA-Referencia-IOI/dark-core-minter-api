"""
Tests for StoreApiMetadataStorage backend.
"""

from unittest.mock import patch
from typing import Any, Dict, Optional

import httpx
import pytest

from dark_core_lib.metadata import (
    MetadataNotFoundError,
    StorageError,
    StoreApiMetadataStorage,
    get_metadata_storage,
)


def _response(
    method: str,
    url: str,
    status_code: int,
    *,
    json_payload=None,
    content: str = "",
    headers: Optional[Dict[str, Any]] = None,
) -> httpx.Response:
    request = httpx.Request(method, url)
    if json_payload is not None:
        return httpx.Response(
            status_code=status_code,
            request=request,
            json=json_payload,
            headers=headers,
        )
    return httpx.Response(
        status_code=status_code,
        request=request,
        content=content.encode("utf-8"),
        headers=headers,
    )


class TestStoreApiMetadataStorage:
    def test_factory_creates_store_api_backend(self):
        storage = get_metadata_storage(
            "store_api",
            store_api_url="http://store-api:8002",
            timeout_seconds=1.5,
        )

        assert isinstance(storage, StoreApiMetadataStorage)
        assert storage.base_url == "http://store-api:8002"
        assert storage.timeout_seconds == 1.5

    def test_store_metadata_returns_cid(self):
        storage = StoreApiMetadataStorage("http://store-api:8002", timeout_seconds=3.0)

        with patch("dark_core_lib.metadata.httpx.post") as mock_post:
            mock_post.return_value = _response(
                "POST",
                "http://store-api:8002/v1/store",
                200,
                json_payload={"cid": "bafy-test", "size": 10, "content_type": "application/json"},
            )

            cid = storage.store_metadata('{"title":"demo"}', "json")

        assert cid == "bafy-test"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == "http://store-api:8002/v1/store"
        assert call_args.kwargs["timeout"] == 3.0
        assert call_args.kwargs["headers"]["Content-Type"] == "application/json"

    def test_store_metadata_raises_on_error_status(self):
        storage = StoreApiMetadataStorage("http://store-api:8002")

        with patch("dark_core_lib.metadata.httpx.post") as mock_post:
            mock_post.return_value = _response(
                "POST",
                "http://store-api:8002/v1/store",
                500,
                json_payload={"detail": "backend failed"},
            )

            with pytest.raises(StorageError, match="Store API store failed"):
                storage.store_metadata('{"title":"demo"}', "json")

    def test_store_metadata_raises_on_request_error(self):
        storage = StoreApiMetadataStorage("http://store-api:8002")

        with patch("dark_core_lib.metadata.httpx.post") as mock_post:
            mock_post.side_effect = httpx.RequestError(
                "network down",
                request=httpx.Request("POST", "http://store-api:8002/v1/store"),
            )

            with pytest.raises(StorageError, match="Store API request failed"):
                storage.store_metadata('{"title":"demo"}', "json")

    def test_get_metadata_returns_content_and_format(self):
        storage = StoreApiMetadataStorage("http://store-api:8002", timeout_seconds=2.0)

        with patch("dark_core_lib.metadata.httpx.get") as mock_get:
            mock_get.return_value = _response(
                "GET",
                "http://store-api:8002/v1/retrieve/cid-1",
                200,
                content="<record>ok</record>",
                headers={"content-type": "text/xml; charset=utf-8"},
            )

            content, fmt = storage.get_metadata("cid-1")

        assert content == "<record>ok</record>"
        assert fmt == "xml"
        mock_get.assert_called_once_with(
            "http://store-api:8002/v1/retrieve/cid-1",
            timeout=2.0,
        )

    def test_get_metadata_not_found(self):
        storage = StoreApiMetadataStorage("http://store-api:8002")

        with patch("dark_core_lib.metadata.httpx.get") as mock_get:
            mock_get.return_value = _response(
                "GET",
                "http://store-api:8002/v1/retrieve/missing",
                404,
                json_payload={"detail": "not found"},
            )

            with pytest.raises(MetadataNotFoundError):
                storage.get_metadata("missing")

    def test_health_check_returns_true_when_backend_healthy(self):
        storage = StoreApiMetadataStorage("http://store-api:8002")

        with patch("dark_core_lib.metadata.httpx.get") as mock_get:
            mock_get.return_value = _response(
                "GET",
                "http://store-api:8002/health",
                200,
                json_payload={"status": "healthy", "backend_healthy": True},
            )

            assert storage.health_check() is True

    def test_health_check_returns_false_on_request_error(self):
        storage = StoreApiMetadataStorage("http://store-api:8002")

        with patch("dark_core_lib.metadata.httpx.get") as mock_get:
            mock_get.side_effect = httpx.RequestError(
                "connection refused",
                request=httpx.Request("GET", "http://store-api:8002/health"),
            )

            assert storage.health_check() is False
