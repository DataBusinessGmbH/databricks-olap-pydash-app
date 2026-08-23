# Entra ID Authentication Setup Guide

This guide explains how to set up Entra ID (Azure AD) authentication for the Databricks OLAP Pydash App when running in non-Databricks environments (e.g., on a VM).

## Overview

The app automatically detects when it's not running in a Databricks environment (by checking for the `DATABRICKS_TOKEN` environment variable). In this case, if Entra ID authentication is configured, users will be prompted to authenticate via Entra ID.

### Key Features

- **Automatic Environment Detection**: App detects Databricks vs. non-Databricks (VM) environments
- **Transparent OAuth2 Flow**: Users are redirected to Entra ID for login
- **Session Management**: Secure session cookies with 12-hour expiration
- **Backward Compatible**: Existing Databricks token-based authentication still works
- **No Changes to Business Logic**: Entra auth is purely an authentication layer

## Prerequisites

1. **Azure Tenant**: You need access to an Azure tenant/subscription
2. **App Registration**: Ability to create an Azure App Registration
3. **Permissions**: Ability to configure API permissions in Azure AD

## Step 1: Create Azure App Registration

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Azure Active Directory** → **App registrations**
3. Click **+ New registration**
4. Fill in the details:
   - **Name**: `Databricks OLAP Pydash App` (or your preferred name)
   - **Supported account types**: `Accounts in this organizational directory only`
   - **Redirect URI**: `Web` → `http://localhost:5000/auth/callback` (for local testing)
     - For production, use your actual deployment URL (e.g., `https://myapp.example.com/auth/callback`)
5. Click **Register**

## Step 2: Get Credentials

After registration, you'll see the app overview page:

1. **Tenant ID**: Copy from "Directory (tenant) ID"
2. **Client ID**: Copy from "Application (client) ID"
3. **Client Secret** (optional, recommended):
   - Go to **Certificates & secrets**
   - Click **+ New client secret**
   - Set expiration to 24 months or longer
   - Copy the secret value (you'll only see it once!)

## Step 3: Configure API Permissions

1. In the app registration, go to **API permissions**
2. Click **+ Add a permission**
3. Search for and select the **Databricks** application (if available in your tenant)
   - Alternatively, use the `.default` scope provided by the app
4. Select the required scopes and click **Add permissions**
5. Click **Grant admin consent** (if you have tenant admin permissions)

## Step 4: Set Environment Variables

Create or update your `.env` file with the credentials obtained above:

```bash
# Databricks Configuration
export DATABRICKS_HOST="adb-xxxx.azuredatabricks.net"
export DATABRICKS_HTTP_PATH="/sql/1.0/warehouses/xxxxx"

# Entra ID Authentication Configuration
export ENTRA_TENANT_ID="your-tenant-id-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export ENTRA_CLIENT_ID="your-client-id-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export ENTRA_CLIENT_SECRET="your-client-secret-xxxxxxxxxxxxxxxxxxxxxx"
export ENTRA_REDIRECT_URI="http://localhost:5000/auth/callback"

# Optional: Flask session encryption key (auto-generated if not set)
export FLASK_SECRET_KEY="your-secure-random-key-here"

# Optional: Log level
export APP_LOG_LEVEL="INFO"
```

### Important Notes

- **ENTRA_CLIENT_SECRET**: While marked optional, it's recommended for production deployments
- **ENTRA_REDIRECT_URI**: Must match exactly what's configured in the Azure App Registration
- **FLASK_SECRET_KEY**: If not set, a random key is generated (but won't persist across restarts)
- **DATABRICKS_CREDENTIALS**: Still required - Entra tokens are exchanged for Databricks tokens

## Step 5: Test the Setup

### Local Testing

1. Ensure you've set all required environment variables in `.env`
2. Start the app:
   ```bash
   python3 app.py
   ```
3. Open your browser to `http://localhost:5000`
4. You should be redirected to the Entra ID login page
5. After successful login, you'll be redirected back to the app

### Verify Authentication

Check the endpoint `/auth/status` to see authentication status:

```bash
curl http://localhost:5000/auth/status
```

Expected response:
```json
{
  "in_databricks": false,
  "entra_configured": true,
  "entra_authenticated": true
}
```

## Troubleshooting

### Issue: "Entra authentication not configured"

**Cause**: One or more required environment variables are missing

**Solution**: Verify all three required variables are set:
- `ENTRA_TENANT_ID`
- `ENTRA_CLIENT_ID`
- `ENTRA_REDIRECT_URI`

Check logs for which variable is missing:
```bash
grep "Missing:" app.log
```

### Issue: Redirect URI mismatch

**Error**: `AADSTS50011: The reply URL specified in the request does not match the reply URLs configured for the application`

**Cause**: The ENTRA_REDIRECT_URI doesn't match Azure App Registration settings

**Solution**:
1. Go to Azure Portal → App Registration → Authentication
2. Ensure "Web" redirect URI exactly matches your `ENTRA_REDIRECT_URI`
3. Restart the app after fixing

### Issue: "Invalid state parameter"

**Cause**: CSRF protection caught a mismatch

**Solution**: This shouldn't happen in normal operation. Try:
1. Clear browser cookies
2. Restart the app
3. Try logging in again

### Issue: Blank login page after Entra redirect

**Cause**: JavaScript might be disabled or there's a JavaScript error

**Solution**:
1. Check browser console for errors
2. Enable JavaScript
3. Check app logs: `grep "error" app.log`

## Production Deployment

### Multi-Environment Setup

For production deployments using different URLs per environment:

```bash
# Development
export ENTRA_REDIRECT_URI="http://localhost:5000/auth/callback"

# Staging
export ENTRA_REDIRECT_URI="https://staging.myapp.example.com/auth/callback"

# Production
export ENTRA_REDIRECT_URI="https://myapp.example.com/auth/callback"
```

Register all redirect URIs in Azure App Registration:

1. Go to **Authentication** → **Web**
2. Add all URLs under "Redirect URIs"

### Security Best Practices

1. **Use HTTPS**: Production deployments must use HTTPS
2. **Strong Secret**: Use a strong, randomly generated secret for `FLASK_SECRET_KEY`
3. **Tenant Restrictions**: Consider using Azure AD Conditional Access
4. **Monitor**: Log and monitor authentication failures
5. **Token Rotation**: Regularly rotate client secrets (every 6-12 months)
6. **RBAC**: Implement role-based access control at the Databricks level

### Dockerfile Configuration

When deploying with Docker:

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Environment variables should be provided via docker-compose or K8s secrets
ENV FLASK_SECRET_KEY=""
ENV DATABRICKS_HOST=""
ENV DATABRICKS_HTTP_PATH=""
ENV DATABRICKS_DEFAULT_CATALOG=""
ENV DATABRICKS_DEFAULT_SCHEMA=""
ENV ENTRA_TENANT_ID=""
ENV ENTRA_CLIENT_ID=""
ENV ENTRA_CLIENT_SECRET=""
ENV ENTRA_REDIRECT_URI=""

EXPOSE 5000
CMD ["python3", "app.py"]
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      User in Non-Databricks Env             │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
                    ┌────────────────┐
                    │  App detects:  │
                    │ - No DATABRICKS│
                    │   _TOKEN       │
                    │ - Entra config │
                    │   present      │
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐
                    │  Redirect to  │
                    │  Entra Login  │
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐         ┌──────────────┐
                    │  User logs in  │────────▶│   Entra ID   │
                    │  via Entra     │         │    (Azure)   │
                    └────────┬───────┘         └──────────────┘
                             │
                             ▼
                    ┌────────────────┐
                    │  Return auth   │
                    │  code to app   │
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐         ┌──────────────┐
                    │  Exchange code │────────▶│ Entra Token  │
                    │  for Entra     │         │  Endpoint    │
                    │  token         │         └──────────────┘
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐
                    │  Store token   │
                    │  in session    │
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐
                    │  Redirect to   │
                    │  app (/)       │
                    └────────┬───────┘
                             │
                             ▼
                    ┌────────────────┐
                    │  App is now    │
                    │  ready to use  │
                    │  (continue     │
                    │  Databricks    │
                    │  flow)         │
                    └────────────────┘
```

## API Reference

### Authentication Endpoints

#### GET `/auth/login`
Initiates Entra ID login flow.

**Response**: Redirects to Entra ID authorization endpoint

#### GET `/auth/callback`
Handles Entra ID OAuth2 callback.

**Query Parameters**:
- `code`: Authorization code from Entra
- `state`: CSRF protection state
- `error` (optional): Error code if login failed
- `error_description` (optional): Error description

**Response**: Redirects to `/` on success, returns error JSON on failure

#### GET `/auth/logout`
Clears user session and logs out.

**Response**: Redirects to `/`

#### GET `/auth/status`
Returns current authentication status.

**Response**:
```json
{
  "in_databricks": false,
  "entra_configured": true,
  "entra_authenticated": true
}
```

## Environment Detection Logic

The app uses the following logic to detect Databricks environments:

1. **Check request headers**: Look for `x-forwarded-access-token` header
2. **Check environment variables**: Look for `DATABRICKS_TOKEN`
3. **If either is present**: Assume running in Databricks environment
4. **If neither is present**: Assume running in non-Databricks environment (VM)
5. **If Entra configured**: Require Entra authentication

## Session Security

- **Secure Cookies**: Cookies are marked `HttpOnly` and `Secure`
- **CSRF Protection**: State parameter validates OAuth2 flow
- **Session Expiration**: 12 hours default (configurable)
- **Token Storage**: Tokens stored in encrypted Flask sessions (server-side)

## Additional Resources

- [Azure AD OAuth 2.0 Documentation](https://docs.microsoft.com/en-us/azure/active-directory/develop/v2-oauth2-auth-code-flow)
- [Azure Identity SDK for Python](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/identity/azure-identity)
- [Databricks Accounts API](https://docs.databricks.com/api/account)
- [Flask Sessions](https://flask.palletsprojects.com/en/2.3.x/config/#SESSION_COOKIE_SECURE)

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review app logs: `tail -f app.log`
3. Enable debug logging: `export APP_LOG_LEVEL=DEBUG`
4. Check Entra sign-in logs in Azure Portal
