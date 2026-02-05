"""
Configuration settings for dARK Core API.

Uses pydantic-settings for environment variable loading and validation.
"""

from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # API Server
    minter_api_host: str = "0.0.0.0"
    minter_api_port: int = 8001
    
    # Batch processing
    batch_size_limit: int = 100
    
    # Minter Configuration
    minter_shoulder: str = ""
    
    # mTLS Configuration
    mtls_enabled: bool = False
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    tls_ca_file: Optional[str] = None
    
    # Blockchain Connection (same vars as dark-orchestrator)
    dark_rpc_url: str = "http://localhost:8545"
    dark_chain_id: int = 1337
    dark_authority_address: str = ""
    dark_contract_address: str = ""
    dark_admin_private_key: str = ""
    
    def validate_blockchain_config(self) -> None:
        """Validate that blockchain configuration is complete."""
        missing = []
        if not self.dark_authority_address:
            missing.append("DARK_AUTHORITY_ADDRESS")
        if not self.dark_contract_address:
            missing.append("DARK_CONTRACT_ADDRESS")
        if not self.dark_admin_private_key:
            missing.append("DARK_ADMIN_PRIVATE_KEY")
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    
    def validate_mtls_config(self) -> None:
        """Validate mTLS configuration when enabled."""
        if not self.mtls_enabled:
            return
        
        missing = []
        if not self.tls_cert_file:
            missing.append("TLS_CERT_FILE")
        if not self.tls_key_file:
            missing.append("TLS_KEY_FILE")
        if not self.tls_ca_file:
            missing.append("TLS_CA_FILE")
        
        if missing:
            raise ValueError(f"mTLS enabled but missing: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
