"""
Tests for mTLS authentication middleware.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.middleware.auth import (
    MTLSAuthenticator,
    enforce_authority_match,
    extract_authority_id,
    get_mtls_authenticator,
    require_authority_identity,
    require_mtls,
)


class TestMTLSAuthenticator:
    """Tests for MTLSAuthenticator class."""
    
    @pytest.fixture
    def mock_settings_enabled(self):
        """Create mock settings with mTLS enabled."""
        settings = MagicMock()
        settings.mtls_enabled = True
        return settings
    
    @pytest.fixture
    def mock_settings_disabled(self):
        """Create mock settings with mTLS disabled."""
        settings = MagicMock()
        settings.mtls_enabled = False
        return settings
    
    @pytest.fixture
    def authenticator_enabled(self, mock_settings_enabled):
        """Create authenticator with mTLS enabled."""
        return MTLSAuthenticator(mock_settings_enabled)
    
    @pytest.fixture
    def authenticator_disabled(self, mock_settings_disabled):
        """Create authenticator with mTLS disabled."""
        return MTLSAuthenticator(mock_settings_disabled)
    
    def test_init_with_mtls_enabled(self, mock_settings_enabled):
        """Test initialization with mTLS enabled."""
        auth = MTLSAuthenticator(mock_settings_enabled)
        assert auth.enabled is True
    
    def test_init_with_mtls_disabled(self, mock_settings_disabled):
        """Test initialization with mTLS disabled."""
        auth = MTLSAuthenticator(mock_settings_disabled)
        assert auth.enabled is False
    
    # Tests for get_client_cert_info
    
    def test_get_cert_info_from_proxy_headers(self, authenticator_enabled):
        """Test extracting cert info from reverse proxy headers."""
        request = MagicMock()
        request.headers = {
            "X-SSL-Client-Verify": "SUCCESS",
            "X-SSL-Client-DN": "CN=test-client,O=Test Org",
            "X-SSL-Client-Cert": "-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----",
        }
        
        cert_info = authenticator_enabled.get_client_cert_info(request)
        
        assert cert_info is not None
        assert cert_info["verified"] is True
        assert cert_info["dn"] == "CN=test-client,O=Test Org"
    
    def test_get_cert_info_verification_failed(self, authenticator_enabled):
        """Test extracting cert info when verification failed."""
        request = MagicMock()
        request.headers = {
            "X-SSL-Client-Verify": "FAILED",
            "X-SSL-Client-DN": "CN=test-client",
        }
        
        cert_info = authenticator_enabled.get_client_cert_info(request)
        
        assert cert_info is not None
        assert cert_info["verified"] is False
    
    def test_get_cert_info_no_headers(self, authenticator_enabled):
        """Test when no certificate headers are present."""
        request = MagicMock()
        request.headers = {}
        request.scope = {}  # No transport
        
        cert_info = authenticator_enabled.get_client_cert_info(request)
        
        assert cert_info is None
    
    def test_get_cert_info_from_ssl_transport(self, authenticator_enabled):
        """Test extracting cert info from SSL transport (uvicorn direct)."""
        request = MagicMock()
        request.headers = {}  # No proxy headers
        
        # Mock SSL transport
        mock_transport = MagicMock()
        mock_ssl_object = MagicMock()
        mock_ssl_object.getpeercert.return_value = {
            "subject": (
                (("commonName", "test-client"),),
                (("organizationName", "Test Org"),),
            ),
        }
        mock_transport.get_extra_info.return_value = mock_ssl_object
        
        request.scope = {"transport": mock_transport}
        
        cert_info = authenticator_enabled.get_client_cert_info(request)
        
        assert cert_info is not None
        assert cert_info["verified"] is True
    
    # Tests for __call__ (authentication)
    
    @pytest.mark.asyncio
    async def test_call_mtls_disabled_allows_all(self, authenticator_disabled):
        """Test that disabled mTLS allows all requests."""
        request = MagicMock()
        request.headers = {}
        
        result = await authenticator_disabled(request)
        
        assert result is None  # No cert info, but allowed
    
    @pytest.mark.asyncio
    async def test_call_mtls_enabled_no_cert_raises_401(self, authenticator_enabled):
        """Test that enabled mTLS without cert raises 401."""
        request = MagicMock()
        request.headers = {}
        request.scope = {}
        
        with pytest.raises(HTTPException) as exc_info:
            await authenticator_enabled(request)
        
        assert exc_info.value.status_code == 401
        assert "certificate required" in exc_info.value.detail.lower()
    
    @pytest.mark.asyncio
    async def test_call_mtls_enabled_invalid_cert_raises_403(self, authenticator_enabled):
        """Test that enabled mTLS with invalid cert raises 403."""
        request = MagicMock()
        request.headers = {
            "X-SSL-Client-Verify": "FAILED",
            "X-SSL-Client-DN": "CN=bad-client",
        }
        
        with pytest.raises(HTTPException) as exc_info:
            await authenticator_enabled(request)
        
        assert exc_info.value.status_code == 403
        assert "invalid" in exc_info.value.detail.lower()
    
    @pytest.mark.asyncio
    async def test_call_mtls_enabled_valid_cert_returns_info(self, authenticator_enabled):
        """Test that enabled mTLS with valid cert returns cert info."""
        request = MagicMock()
        request.headers = {
            "X-SSL-Client-Verify": "SUCCESS",
            "X-SSL-Client-DN": "CN=valid-client,O=Test Org",
            "X-SSL-Client-Cert": "cert_data",
        }
        
        result = await authenticator_enabled(request)
        
        assert result is not None
        assert result["verified"] is True
        assert result["dn"] == "CN=valid-client,O=Test Org"


class TestGetMTLSAuthenticator:
    """Tests for get_mtls_authenticator factory function."""
    
    def test_returns_authenticator(self):
        """Test that factory returns an MTLSAuthenticator instance."""
        with patch('app.middleware.auth.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(mtls_enabled=False)
            
            auth = get_mtls_authenticator()
            
            assert isinstance(auth, MTLSAuthenticator)


class TestRequireMTLS:
    """Tests for require_mtls dependency."""
    
    @pytest.mark.asyncio
    async def test_calls_authenticator(self):
        """Test that require_mtls calls the authenticator."""
        mock_request = MagicMock()
        mock_authenticator = MagicMock()
        mock_authenticator.return_value = AsyncMock(return_value={"verified": True})()
        
        result = await require_mtls(mock_request, mock_authenticator)
        
        # The authenticator should have been called
        mock_authenticator.assert_called_once_with(mock_request)


class TestAuthorityIdentityHelpers:
    """Tests for authority identity extraction and matching helpers."""

    def test_extract_authority_id_from_header(self):
        request = MagicMock()
        request.headers = {"X-Authority-Id": "test-uuid"}

        assert extract_authority_id(request) == "test-uuid"

    def test_extract_authority_id_from_dn(self):
        request = MagicMock()
        request.headers = {}
        cert_info = {"dn": "CN=test-client,UID=test-uuid,O=Test Org"}

        assert extract_authority_id(request, cert_info) == "test-uuid"

    def test_enforce_authority_match_success(self):
        identity = {"authority_id": "test-uuid"}

        assert enforce_authority_match(identity, "test-uuid") == "test-uuid"

    def test_enforce_authority_match_failure(self):
        identity = {"authority_id": "other-uuid"}

        with pytest.raises(HTTPException) as exc_info:
            enforce_authority_match(identity, "test-uuid")

        assert exc_info.value.status_code == 403


class TestRequireAuthorityIdentity:
    """Tests for the mutating-endpoint identity dependency."""

    @pytest.mark.asyncio
    async def test_disabled_requires_header(self):
        request = MagicMock()
        request.headers = {"X-Authority-Id": "test-uuid"}
        authenticator = MagicMock()
        authenticator.enabled = False
        authenticator.return_value = AsyncMock(return_value=None)()

        identity = await require_authority_identity(request, authenticator)

        assert identity["authority_id"] == "test-uuid"
        assert identity["auth_mode"] == "header"

    @pytest.mark.asyncio
    async def test_disabled_missing_header_raises_401(self):
        request = MagicMock()
        request.headers = {}
        authenticator = MagicMock()
        authenticator.enabled = False
        authenticator.return_value = AsyncMock(return_value=None)()

        with pytest.raises(HTTPException) as exc_info:
            await require_authority_identity(request, authenticator)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_enabled_uses_cert_and_dn(self):
        request = MagicMock()
        request.headers = {}
        authenticator = MagicMock()
        authenticator.enabled = True
        authenticator.return_value = AsyncMock(
            return_value={"verified": True, "dn": "CN=test,UID=test-uuid,O=Test Org"}
        )()

        identity = await require_authority_identity(request, authenticator)

        assert identity["authority_id"] == "test-uuid"
        assert identity["auth_mode"] == "mtls"


class TestMTLSIntegration:
    """Integration tests for mTLS middleware with FastAPI app."""
    
    @pytest.fixture
    def app_with_mtls_enabled(self):
        """Create a test app with mTLS enabled."""
        from fastapi import FastAPI, Depends
        
        app = FastAPI()
        
        @app.get("/protected")
        async def protected_endpoint(cert_info: dict = Depends(require_mtls)):
            return {"authenticated": True, "cert": cert_info}
        
        return app
    
    def test_endpoint_without_cert_when_disabled(self, app_with_mtls_enabled):
        """Test accessing endpoint when mTLS is disabled."""
        with patch('app.middleware.auth.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(mtls_enabled=False)
            
            client = TestClient(app_with_mtls_enabled)
            response = client.get("/protected")
            
            assert response.status_code == 200
    
    def test_endpoint_without_cert_when_enabled(self, app_with_mtls_enabled):
        """Test accessing endpoint without cert when mTLS is enabled."""
        with patch('app.middleware.auth.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(mtls_enabled=True)
            
            client = TestClient(app_with_mtls_enabled)
            response = client.get("/protected")
            
            assert response.status_code == 401
    
    def test_endpoint_with_valid_cert_headers(self, app_with_mtls_enabled):
        """Test accessing endpoint with valid cert headers."""
        with patch('app.middleware.auth.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(mtls_enabled=True)
            
            client = TestClient(app_with_mtls_enabled)
            response = client.get(
                "/protected",
                headers={
                    "X-SSL-Client-Verify": "SUCCESS",
                    "X-SSL-Client-DN": "CN=test-client",
                }
            )
            
            assert response.status_code == 200
            data = response.json()
            assert data["authenticated"] is True
    
    def test_endpoint_with_invalid_cert_headers(self, app_with_mtls_enabled):
        """Test accessing endpoint with invalid cert headers."""
        with patch('app.middleware.auth.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock(mtls_enabled=True)
            
            client = TestClient(app_with_mtls_enabled)
            response = client.get(
                "/protected",
                headers={
                    "X-SSL-Client-Verify": "FAILED",
                    "X-SSL-Client-DN": "CN=bad-client",
                }
            )
            
            assert response.status_code == 403
