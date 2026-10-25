# Temporary Streamlit Community Cloud deployment

This guide describes a temporary, public demo mirror of MediAssist AI. It does
not change the deployed Databricks App (`mediassist-ai` v5) and uses synthetic
East/West data only. The public app is not production authentication or a
clinical approval system.

## Deployment coordinates

- GitHub repository: `https://github.com/papairules/mediassist-ai.git`
- Branch: `main`
- Streamlit entry point: `src/crestline_ui/app.py`
- Python: select **3.12** in Streamlit Community Cloud Advanced settings.
- Dependencies: root `requirements.txt`; it installs the project as a regular
  package and pins the validated UI/runtime dependency versions.

Do not commit a secrets file. Community Cloud stores the values in the app's
Secrets settings. The repository may remain private while the deployed app is
made public for the interview.

## Required Streamlit secrets

Paste this shape into the Community Cloud Secrets field, replacing values in
the deployment console only:

```toml
MEDIASSIST_HOSTED_MODE = "streamlit_cloud"
DATABRICKS_HOST = "https://<workspace-host>"
DATABRICKS_CLIENT_ID = "<dedicated-demo-service-principal-client-id>"
DATABRICKS_CLIENT_SECRET = "<dedicated-demo-oauth-secret>"
MEDIASSIST_WAREHOUSE_ID = "d249a9e5325ec021"
MEDIASSIST_VECTOR_INDEX = "workspace.crestline_silver.document_retrieval_chunks_index"
DATABRICKS_LLM_ENDPOINT = "databricks-qwen3-next-80b-a3b-instruct"
MEDIASSIST_BUILD_ID = "streamlit-demo-v5"
```

`MEDIASSIST_HOSTED_MODE` is required. In this mode the application never uses
`DATABRICKS_CONFIG_PROFILE`, the developer CLI profile, or local credentials.
The OAuth secret is used only in memory to construct the Databricks SDK client;
it is not displayed or logged.

## Databricks permissions

Create a dedicated temporary service principal for this external host. Do not
reuse the Databricks Apps service-principal secret. Grant only:

- Workspace assignment.
- `CAN_USE` on SQL warehouse `d249a9e5325ec021`.
- `SELECT` on the three Gold views.
- `SELECT` on the required Silver document tables.
- Query/`SELECT` access to
  `workspace.crestline_silver.document_retrieval_chunks_index`.
- `CAN_QUERY` on the configured Qwen serving endpoint.

Grant no table writes, DDL, job deployment, volume, or workspace-management
permissions. Confirm that the workspace accepts HTTPS requests from Community
Cloud; private-link-only or IP-allowlisted workspaces may require a different
hosting option.

## Community Cloud steps

1. Connect the GitHub account with access to the repository.
2. Create an app from `main` and select `src/crestline_ui/app.py`.
3. Select Python 3.12 in Advanced settings.
4. Paste the secrets above into the Secrets field.
5. Deploy, then set the app sharing mode to public.
6. Exercise the read-only scenarios: ORD-1001, ORD-1003 unknown ETA, H-E101
   brand lock, H-E102 conditional amendment, and the blocked latex candidate.
7. Confirm that the UI labels the East/West selector `DEMO_ONLY`, displays
   concise outcomes first, and keeps detailed evidence under `View supporting
   evidence`.

The app applies a prompt-size cap and a per-browser-session request window.
These controls reduce accidental or single-session overuse but cannot enforce a
global anonymous-user quota across many browsers or IPs. Monitor Databricks
warehouse, Vector Search, and serving-endpoint usage during the demo.

## Shutdown and credential revocation

After the interview:

1. Delete the Community Cloud app from its app settings.
2. Remove the app's stored secrets.
3. Revoke/delete the temporary OAuth secret.
4. Remove the service principal's workspace assignment and object permissions.
5. Delete the temporary GitHub branch/repository if one was created.

The existing Databricks App and its v5 deployment remain independent.
