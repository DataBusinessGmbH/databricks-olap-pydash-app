"""
src/auth.py
-----------
Entra ID authentication module for non-Databricks environments.

Handles:
- Detection of Databricks vs. non-Databricks (VM) environments
- Entra ID OAuth2 authentication flow
- Token management and session persistence
"""

import os
import logging
from typing import Optional, Tuple
from flask import request as flask_request
from msal import ConfidentialClientApplication
import json

def _setup_logger() -> logging.Logger:
    level_name = os.getenv("APP_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger(__name__)
    
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
    
    logger.setLevel(level)
    return logger

LOGGER = _setup_logger()


class EntraAuthConfig:
    """Configuration for Entra ID authentication."""
    
    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ):
        self.tenant_id = tenant_id or os.getenv("ENTRA_TENANT_ID", "").strip()
        self.client_id = client_id or os.getenv("ENTRA_CLIENT_ID", "").strip()
        self.client_secret = client_secret or os.getenv("ENTRA_CLIENT_SECRET", "").strip()
        self.redirect_uri = redirect_uri or os.getenv("ENTRA_REDIRECT_URI", "").strip()
    
    def is_complete(self) -> bool:
        """Check if all required Entra config is present."""
        return bool(self.tenant_id and self.client_id and self.redirect_uri)
    
    def missing_fields(self) -> list[str]:
        """Return list of missing required fields."""
        missing = []
        if not self.tenant_id:
            missing.append("ENTRA_TENANT_ID")
        if not self.client_id:
            missing.append("ENTRA_CLIENT_ID")
        if not self.redirect_uri:
            missing.append("ENTRA_REDIRECT_URI")
        return missing


class EntraAuthManager:
    """Manages Entra ID authentication flow."""
    
    DATABRICKS_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"
    
    def __init__(self, config: Optional[EntraAuthConfig] = None):
        self.config = config or EntraAuthConfig()
        self._app = None
        self._token_cache = {}
        
        if self.config.is_complete():
            self._initialize_app()
        else:
            missing = self.config.missing_fields()
            LOGGER.warning(
                "Entra authentication not fully configured. Missing: %s",
                ", ".join(missing),
            )
    
    def _initialize_app(self) -> None:
        """Initialize the MSAL ConfidentialClientApplication."""
        try:
            authority = f"https://login.microsoftonline.com/{self.config.tenant_id}"
            LOGGER.debug("Initializing with: tenant=%s, client_id=%s, secret_len=%d", 
                        self.config.tenant_id, self.config.client_id, 
                        len(self.config.client_secret) if self.config.client_secret else 0)
            
            self._app = ConfidentialClientApplication(
                client_id=self.config.client_id,
                client_credential=self.config.client_secret,
                authority=authority,
            )
            LOGGER.info(
                "Entra auth manager initialized for tenant %s, client %s",
                self.config.tenant_id,
                self.config.client_id,
            )
        except Exception as e:
            LOGGER.error("Failed to initialize Entra auth: %s", str(e), exc_info=True)
            self._app = None
    
    def is_configured(self) -> bool:
        """Check if Entra auth is fully configured."""
        return self._app is not None
    
    def get_auth_url(self, state: Optional[str] = None) -> str:
        """
        Generate Entra ID login URL.
        
        Args:
            state: Optional state parameter for CSRF protection
        
        Returns:
            Login URL to redirect user to
        """
        if not self.is_configured():
            LOGGER.error("Entra auth manager not configured")
            raise RuntimeError("Entra authentication not configured")
        
        try:
            auth_url = self._app.get_authorization_request_url(
                scopes=[self.DATABRICKS_SCOPE],
                redirect_uri=self.config.redirect_uri,
                state=state,
            )
            LOGGER.info("Generated Entra auth URL")
            return auth_url
        except Exception as e:
            LOGGER.error("Failed to generate auth URL: %s", str(e), exc_info=True)
            raise
    
    def exchange_code_for_token(self, code: str) -> Optional[dict]:
        """
        Exchange authorization code for access token.
        
        Args:
            code: Authorization code from Entra callback
        
        Returns:
            Token dict with access_token, or None if failed
        """
        if not self.is_configured():
            LOGGER.error("Entra auth manager not configured")
            raise RuntimeError("Entra authentication not configured")
        
        try:
            LOGGER.debug("Exchanging code for token. Client ID: %s, Redirect URI: %s", 
                        self.config.client_id, self.config.redirect_uri)
            LOGGER.debug("Client secret length: %d chars", len(self.config.client_secret or ""))
            
            token_response = self._app.acquire_token_by_authorization_code(
                code=code,
                scopes=[self.DATABRICKS_SCOPE],
                redirect_uri=self.config.redirect_uri,
            )
            
            if "error" in token_response:
                LOGGER.error(
                    "Failed to exchange code for token: %s",
                    token_response.get("error_description", token_response.get("error")),
                )
                return None
            
            LOGGER.info("Successfully exchanged code for Entra token")
            
            # Cache the token with user info
            if "access_token" in token_response:
                self._token_cache["access_token"] = token_response["access_token"]
                self._token_cache["expires_on"] = token_response.get("expires_on")
            
            return token_response
        except Exception as e:
            LOGGER.error("Exception during code exchange: %s", str(e), exc_info=True)
            return None
    
    def get_cached_access_token(self) -> Optional[str]:
        """Get cached Entra access token."""
        return self._token_cache.get("access_token")
    
    def clear_cache(self) -> None:
        """Clear cached tokens."""
        self._token_cache.clear()
        LOGGER.info("Cleared auth token cache")


def detect_databricks_environment() -> bool:
    """
    Detect if running in a Databricks environment.
    
    Returns True if a Databricks token is found (in headers or env),
    indicating we're in a Databricks environment and don't need Entra auth.
    
    Returns False if no token is found, indicating we're in a non-Databricks
    environment (e.g., VM) and need to force Entra authentication.
    """
    LOGGER.debug("detect_databricks_environment called")
    try:
        # Check request headers first (for multi-tenant deployments)
        token = (flask_request.headers.get("x-forwarded-access-token") or "").strip()
        if token:
            LOGGER.debug("Found Databricks token in request headers - running in Databricks environment")
            return True
    except RuntimeError:
        # No active request context; continue to env var check
        pass
    
    # Check environment variable
    token = (os.getenv("DATABRICKS_TOKEN") or "").strip()
    if token:
        LOGGER.debug("Found DATABRICKS_TOKEN env var - running in Databricks environment")
        return True
    
    LOGGER.debug("No Databricks token found - running in non-Databricks environment (e.g., VM)")
    return False


def is_entra_auth_required() -> bool:
    """
    Determine if Entra authentication is required.
    
    Returns True if:
    1. Not in Databricks environment, AND
    2. Entra auth is configured
    
    Returns False otherwise (use existing Databricks token flow).
    """
    LOGGER.debug("is_entra_auth_required called")
    
    if detect_databricks_environment():
        LOGGER.debug("Databricks environment detected - Entra auth not required")
        return False
    
    config = EntraAuthConfig()
    if not config.is_complete():
        LOGGER.warning(
            "Non-Databricks environment detected but Entra auth not configured. Missing: %s",
            ", ".join(config.missing_fields()),
        )
        return False
    
    LOGGER.debug("Non-Databricks environment detected and Entra auth is configured")
    return True
