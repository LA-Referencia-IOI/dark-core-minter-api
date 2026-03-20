"""
mTLS Authentication Middleware.

Validates client certificates when mTLS is enabled.
"""

import logging
import re
import ssl
from typing import Optional

from fastapi import Request, HTTPException, Depends

from app.config import get_settings, Settings

logger = logging.getLogger(__name__)

AUTHORITY_ID_HEADER_CANDIDATES = (
    "X-Authority-Id",
    "X-Authority-UUID",
)


def _clean_authority_id(value: Optional[str]) -> Optional[str]:
    """Normalize authority-id values extracted from headers or cert metadata."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _extract_authority_id_from_dn(dn: Optional[str]) -> Optional[str]:
    """Best-effort extraction of authority identity from certificate subject DN."""
    if not dn:
        return None

    for pattern in (
        r"(?:^|,)\s*UID\s*=\s*([^,]+)",
        r"(?:^|,)\s*SERIALNUMBER\s*=\s*([^,]+)",
        r"(?:^|,)\s*CN\s*=\s*([^,]+)",
    ):
        match = re.search(pattern, dn, flags=re.IGNORECASE)
        if match:
            return _clean_authority_id(match.group(1))
    return None


def extract_authority_id(request: Request, cert_info: Optional[dict] = None) -> Optional[str]:
    """Extract authority identity from trusted headers or certificate metadata."""
    for header_name in AUTHORITY_ID_HEADER_CANDIDATES:
        authority_id = _clean_authority_id(request.headers.get(header_name))
        if authority_id:
            return authority_id

    if cert_info:
        return _extract_authority_id_from_dn(cert_info.get("dn"))

    return None


class MTLSAuthenticator:
    """
    mTLS authentication handler.
    
    When mTLS is enabled, validates that requests include a valid client certificate
    signed by the trusted CA.
    """
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.mtls_enabled
    
    def get_client_cert_info(self, request: Request) -> Optional[dict]:
        """
        Extract client certificate information from the request.
        
        Note: This requires running with uvicorn SSL context or behind
        a reverse proxy that passes certificate info in headers.
        
        Args:
            request: FastAPI request object
            
        Returns:
            Dictionary with certificate info, or None if no cert
        """
        # When running behind a reverse proxy (nginx, haproxy), 
        # the cert info is typically in headers
        client_cert_header = request.headers.get("X-SSL-Client-Cert")
        client_cert_dn = request.headers.get("X-SSL-Client-DN")
        client_cert_verify = request.headers.get("X-SSL-Client-Verify")
        
        if client_cert_verify:
            return {
                "verified": client_cert_verify == "SUCCESS",
                "dn": client_cert_dn,
                "cert": client_cert_header,
            }
        
        # Direct uvicorn SSL - check transport
        if hasattr(request, "scope") and "transport" in request.scope:
            transport = request.scope.get("transport")
            if transport and hasattr(transport, "get_extra_info"):
                ssl_object = transport.get_extra_info("ssl_object")
                if ssl_object:
                    peer_cert = ssl_object.getpeercert()
                    if peer_cert:
                        return {
                            "verified": True,
                            "dn": str(peer_cert.get("subject", "")),
                            "cert": peer_cert,
                        }
        
        return None
    
    async def __call__(self, request: Request) -> Optional[dict]:
        """
        Authenticate the request.
        
        Args:
            request: FastAPI request object
            
        Returns:
            Client certificate info if authenticated
            
        Raises:
            HTTPException: If mTLS is enabled and authentication fails
        """
        if not self.enabled:
            # mTLS disabled - allow all requests (development mode)
            logger.debug("mTLS disabled, skipping authentication")
            return None
        
        cert_info = self.get_client_cert_info(request)
        
        if not cert_info:
            logger.warning("mTLS enabled but no client certificate provided")
            raise HTTPException(
                status_code=401,
                detail="Client certificate required",
                headers={"WWW-Authenticate": "ClientCertificate"},
            )
        
        if not cert_info.get("verified"):
            logger.warning(f"Client certificate verification failed: {cert_info}")
            raise HTTPException(
                status_code=403,
                detail="Invalid client certificate",
            )
        
        logger.info(f"Client authenticated: {cert_info.get('dn')}")
        return cert_info


def get_mtls_authenticator() -> MTLSAuthenticator:
    """Get mTLS authenticator instance."""
    return MTLSAuthenticator(get_settings())


async def require_mtls(
    request: Request,
    authenticator: MTLSAuthenticator = Depends(get_mtls_authenticator),
) -> Optional[dict]:
    """
    Dependency that requires mTLS authentication.
    
    Use this in route definitions to enforce mTLS:
    
        @app.post("/mint/batch")
        async def batch_mint(
            request: BatchMintRequest,
            cert_info: dict = Depends(require_mtls),
        ):
            ...
    """
    return await authenticator(request)


async def require_authority_identity(
    request: Request,
    authenticator: MTLSAuthenticator = Depends(get_mtls_authenticator),
) -> dict:
    """
    Resolve an authenticated authority identity for mutating endpoints.

    Behavior:
    - When mTLS is enabled: require a valid client certificate and extract the
      authority ID from trusted headers or the certificate DN.
    - When mTLS is disabled: require an explicit authority header so requests
      are not anonymous in local/dev mode.
    """
    cert_info = await authenticator(request)
    authority_id = extract_authority_id(request, cert_info)

    if authority_id:
        return {
            "authority_id": authority_id,
            "auth_mode": "mtls" if authenticator.enabled else "header",
            "cert": cert_info,
        }

    if authenticator.enabled:
        raise HTTPException(
            status_code=401,
            detail=(
                "Authority identity required. Provide a trusted authority header "
                "or include it in the certificate subject."
            ),
        )

    header_names = ", ".join(AUTHORITY_ID_HEADER_CANDIDATES)
    raise HTTPException(
        status_code=401,
        detail=f"Authority identity required. Set one of: {header_names}",
    )


def enforce_authority_match(identity: dict, authority_id: str) -> str:
    """Ensure the authenticated authority matches the authority declared in the request."""
    requested = _clean_authority_id(authority_id)
    authenticated = _clean_authority_id(identity.get("authority_id") if identity else None)

    if not authenticated:
        raise HTTPException(status_code=401, detail="Missing authenticated authority identity")

    if requested != authenticated:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Authenticated authority '{authenticated}' does not match "
                f"requested authority '{requested}'"
            ),
        )

    return authenticated


def create_ssl_context(settings: Settings) -> Optional[ssl.SSLContext]:
    """
    Create SSL context for uvicorn when mTLS is enabled.
    
    Args:
        settings: Application settings
        
    Returns:
        SSLContext configured for mTLS, or None if disabled
    """
    if not settings.mtls_enabled:
        return None
    
    settings.validate_mtls_config()
    
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=settings.tls_cert_file,
        keyfile=settings.tls_key_file,
    )
    context.load_verify_locations(cafile=settings.tls_ca_file)
    context.verify_mode = ssl.CERT_REQUIRED
    
    logger.info("SSL context created with mTLS enabled")
    return context
