# Databricks OLAP Pydash App

A Dash-based web application for OLAP analysis on Metric Views with support for both Databricks and non-Databricks environments.

## ✨ Features

- **OLAP Analysis**: Interactive multi-dimensional data analysis
- **Flexible Backend**: Works with Databricks SQL Warehouses
- **Metric Views**: Define and analyze business metrics
- **Reports**: Create and manage custom report definitions
- **Grid UI**: AG Grid-powered interactive data visualization
- **Authentication**: Automatic Entra ID login for non-Databricks environments

## 🚀 Quick Start

### For Databricks Environment

If running within Databricks with `DATABRICKS_TOKEN` set, simply start the app:

```bash
python3 app.py
```

### For Non-Databricks Environment (VM, On-Premises)

If running on a VM without Databricks credentials:

1. **Setup Azure App Registration** (5 minutes)
   - Go to https://portal.azure.com
   - Create new app registration
   - Get Tenant ID and Client ID
   - See `ENTRA_SETUP.md` for detailed steps

2. **Configure Environment Variables**
   ```bash
   export ENTRA_TENANT_ID="your-tenant-id"
   export ENTRA_CLIENT_ID="your-client-id"
   export ENTRA_REDIRECT_URI="http://localhost:5000/auth/callback"
   ```

3. **Start the App**
   ```bash
   python3 app.py
   ```

4. **Login via Entra ID**
   - You'll be automatically redirected to Entra ID login
   - After login, the app works normally

See `quick_start.md` for a 5-minute setup guide.

## 📋 Configuration

### Environment Variables

#### Databricks Configuration
- `DATABRICKS_HOST`: Databricks workspace host
- `DATABRICKS_HTTP_PATH`: SQL Warehouse HTTP path
- `DATABRICKS_TOKEN`: Access token (if running in Databricks)
- `DATABRICKS_DEFAULT_CATALOG`: Default catalog to use
- `DATABRICKS_DEFAULT_SCHEMA`: Default schema to use

#### Entra ID Authentication (for non-Databricks environments)
- `ENTRA_TENANT_ID`: Azure tenant ID
- `ENTRA_CLIENT_ID`: Azure app registration client ID
- `ENTRA_REDIRECT_URI`: OAuth2 callback URL
- `ENTRA_CLIENT_SECRET`: (optional) Client secret for confidential clients
- `FLASK_SECRET_KEY`: (optional) Session encryption key

#### Application Configuration
- `APP_LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)
- `PYDASH_APP_REPORTS_TABLE_NAME`: Reports table name in Databricks
- `PYDASH_APP_REPORTING_CATALOGS`: Comma-separated catalogs for reports

See `.env example` for detailed configuration template.

## 🔐 Authentication

### Automatic Environment Detection

The app automatically detects where it's running:

| Environment | Detection Method | Auth Required |
|---|---|---|
| Databricks | `DATABRICKS_TOKEN` header/env | No (uses token) |
| VM / On-Premises | No token found | Yes (Entra ID) |

### Entra ID Authentication Flow

1. User accesses app without token
2. App checks if Entra is configured
3. If configured → Redirect to Entra ID login
4. User logs in with Azure credentials
5. Token stored in secure session
6. App fully functional

### Session Security

- HttpOnly cookies (JavaScript cannot access)
- Secure flag (HTTPS only in production)
- SameSite=Lax (CSRF protection)
- 12-hour expiration
- Server-side token storage

## 📚 Documentation

### Getting Started
- **[quick_start.md](quick_start.md)** - 5-minute setup guide
- **[ENTRA_SETUP.md](ENTRA_SETUP.md)** - Complete Entra ID setup guide

### For Developers
- **[IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)** - Technical details (in session folder)
- **[VERIFICATION_CHECKLIST.md](VERIFICATION_CHECKLIST.md)** - QA results (in session folder)

### Architecture
- **[plan.md](plan.md)** - Original implementation plan (in session folder)

## 🏗️ Architecture

### Components

```
┌─────────────────────────────────────────────┐
│          Flask/Dash Web Application         │
├─────────────────────────────────────────────┤
│ Frontend: Dash UI with AG Grid              │
├─────────────────────────────────────────────┤
│ Backend Layer:                              │
│ ├─ app.py - Application routes & UI        │
│ ├─ auth.py - Entra ID authentication       │
│ ├─ db.py - Databricks SQL backend          │
│ ├─ model.py - Metric view definitions      │
│ └─ query_planner.py - OLAP query planning  │
├─────────────────────────────────────────────┤
│ External Services:                          │
│ ├─ Databricks SQL Warehouse                │
│ └─ Azure Entra ID (OAuth2)                 │
└─────────────────────────────────────────────┘
```

### Key Modules

- **src/auth.py**: Entra ID authentication
  - EntraAuthConfig: Configuration management
  - EntraAuthManager: OAuth2 flow handling
  - Environment detection functions

- **src/db.py**: Databricks SQL backend
  - DatabricksSqlBackend: Connection management
  - Graceful token error handling

- **app.py**: Flask/Dash application
  - Session configuration and middleware
  - Authentication routes (/auth/*)
  - Dash callbacks for UI interactivity

## 🛠️ Development

### Setup Development Environment

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure .env
cp ".env example" .env
# Edit .env with your credentials
```

### Running in Development

```bash
# With debug mode
export APP_LOG_LEVEL=DEBUG
python3 app.py
```

### Running Tests

```bash
# Quick auth module test
python3 -c "
from src.auth import detect_databricks_environment
print(f'In Databricks: {detect_databricks_environment()}')
"
```

## 📦 Dependencies

### Core
- `dash` - Web framework
- `pandas` - Data manipulation
- `flask` - Web server

### Database
- `databricks-sql-connector` - Databricks SQL connection
- `databricks-sdk` - Databricks SDK

### Authentication
- `msal` - Microsoft Authentication Library
- `azure-core` - Azure SDK core

### Utilities
- `python-dotenv` - Environment variable loading
- `PyYAML` - YAML configuration parsing
- `dash-ag-grid` - Advanced grid component

## 🚀 Deployment

### Production Checklist

- [ ] HTTPS enabled
- [ ] Secure Flask secret key set
- [ ] Databricks credentials configured
- [ ] Entra ID app registration created
- [ ] Redirect URI registered in Azure
- [ ] Log level set appropriately
- [ ] Session timeout configured
- [ ] Database backups enabled

### Docker Deployment

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV FLASK_SECRET_KEY=""
ENV DATABRICKS_HOST=""
ENV ENTRA_TENANT_ID=""

EXPOSE 5000
CMD ["python3", "app.py"]
```

## 🐛 Troubleshooting

### Common Issues

**"Entra authentication not configured"**
- Ensure ENTRA_TENANT_ID, ENTRA_CLIENT_ID, and ENTRA_REDIRECT_URI are set

**"AADSTS50011: Reply URL mismatch"**
- Verify ENTRA_REDIRECT_URI matches Azure App Registration settings exactly

**"Blank page after Entra login"**
- Check browser console for JavaScript errors
- Check app logs with DEBUG logging enabled

### Debug Logging

```bash
export APP_LOG_LEVEL=DEBUG
python3 app.py

# In another terminal
tail -f app.log
```

### Check Authentication Status

```bash
# After app is running
curl http://localhost:5000/auth/status
```

## 📖 Documentation Links

- [Azure AD Documentation](https://docs.microsoft.com/en-us/azure/active-directory/)
- [MSAL for Python](https://github.com/AzureAD/microsoft-authentication-library-for-python)
- [Databricks SQL Connector](https://docs.databricks.com/dev-tools/python-sql-connector.html)
- [Dash Framework](https://dash.plotly.com/)

## 📝 License

[Add your license here]

## 🤝 Contributing

[Add contribution guidelines here]

## 📧 Support

For issues and questions:
1. Check [ENTRA_SETUP.md](ENTRA_SETUP.md) troubleshooting section
2. Enable DEBUG logging
3. Review application logs
4. Check Azure AD sign-in logs

---

**Version**: 1.0  
**Last Updated**: August 2026  
**Status**: Production Ready
