# Power BI Setup

This folder contains a small Python client for the Power BI REST API.

## What is included

- `client.py`: authenticates with a service principal and calls common Power BI APIs.
- `check_powerbi_auth.py`: verifies authentication and lists the workspaces and reports the app can see.
- `.env.example`: the environment variables you need.

## Environment variables

Add these keys to the repo root `.env` file:

```env
POWERBI_TENANT_ID=your-tenant-id-or-tenant-name.onmicrosoft.com
POWERBI_CLIENT_ID=your-app-client-id
POWERBI_CLIENT_SECRET=your-app-client-secret
POWERBI_WORKSPACE_ID=
POWERBI_REPORT_ID=
POWERBI_DATASET_ID=
```

The client also falls back to the existing Microsoft variables automatically:

```env
MICROSOFT_TENANT_ID=...
MICROSOFT_CLIENT_ID=...
MICROSOFT_CLIENT_SECRET=...
```

That means if you're reusing the same Entra app as the SharePoint integration, you only need to add the Power BI resource IDs:

```env
POWERBI_WORKSPACE_ID=
POWERBI_REPORT_ID=
POWERBI_DATASET_ID=
```

## Verify authentication

Run:

```bash
.venv/bin/python -m powerbi.check_powerbi_auth
```

If the service principal is configured correctly, the script will print:

- The number of workspaces it can access.
- The first few workspace IDs and names.
- The reports inside the configured workspace, if `POWERBI_WORKSPACE_ID` is set.

## Next steps

Once authentication works, the next backend step is usually to expose an API route that returns:

- `reportId`
- `embedUrl`
- `accessToken`
- `tokenExpiration`

The frontend can then use `powerbi-client` to embed the report securely without exposing the app secret in the browser.
