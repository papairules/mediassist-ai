# MediAssist AI — governed healthcare distribution intelligence

This project implements metadata-driven CSV ingestion into
`workspace.crestline_bronze` plus a minimal typed Silver batch under
`workspace.crestline_silver`, plus three focused SQL views under
`workspace.crestline_gold`. It continues through citation-ready document
extraction, deterministic retrieval and substitution validation, a LangGraph
orchestrator, and a Streamlit application for Databricks Apps. The
`crestline_synthetic_v1` directory is an immutable, explicitly synthetic
source fixture.

## Architecture

The deployed flow is intentionally layered:

```text
CSV/PDF fixtures
    -> Bronze Delta (Auto Loader, provenance, quarantine)
    -> Silver Delta (typed records, deduplication, referential checks)
    -> Gold SQL views (fulfillment, ETA evidence, regional inventory)
    -> RetrievalService (authorized SQL + Vector Search discovery)
    -> LangGraph (routing, scoped memory, deterministic decisions)
    -> Streamlit / Databricks Apps (presentation only)
```

Supplier restock estimates and carrier delivery estimates remain separate
evidence types. Neither is presented as a confirmed hospital delivery date.
Substitution results are never clinical approvals: every eligible result is
`ELIGIBLE_FOR_HUMAN_REVIEW` and requires qualified human clinical review.
The East/West selectors used by the prototype are `DEMO_ONLY` synthetic
scopes, not production authentication or authorization.

## Behavior

`src/crestline_bronze/sources.yml` registers the nine synthetic CSV sources, their raw columns,
natural keys, landing directories, and structural types. A single Python wheel
job loops through enabled sources. For each source it:

1. Uses Auto Loader to discover CSV files below
   `/Volumes/<catalog>/<schema>/<volume>/landing/<landing_subpath>`.
2. Reads every source column as a string and retains a JSON representation of
   the parsed source record.
3. Adds source path/name/size/modification time, file and record identifiers,
   ingestion timestamp, and run ID.
4. Applies structural checks only: malformed CSV, unexpected schema, missing
   required values, invalid declared types, and duplicate keys within a file.
5. Delta-merges valid records into the configured `*_raw` table and invalid
   records into `ingestion_quarantine`.

Auto Loader checkpoints prevent normal replay. Delta MERGE on `_record_id` and
`_quarantine_id` provides a second guard if a checkpoint is recreated. Source
values are not cast or reconciled in Bronze.

## Silver batch

The `silver_batch` wheel task reads all nine registered Bronze tables. It uses
the packaged Bronze registry as the only source of column types, required flags,
and business keys, and publishes the corresponding Silver tables after all
validation succeeds. The batch trims source strings, maps empty strings to
NULL, uses `try_cast` for declared types, rejects failed conversions and missing
required values, deterministically deduplicates by each registered business key,
and validates declared cross-source relationships.

Supplier ETA revisions remain distinct because their key is
`source_record_id`; inventory snapshots retain `snapshot_at` in their composite
key. Document versions, partial shipments, nullable supplier dates, and
category-dependent product attributes are not reconciled or interpreted.

Publication uses an independent atomic Delta `CREATE OR REPLACE TABLE` for each
table. All nine temporary results are validated before publication begins, but
Delta does not provide one transaction spanning nine tables. A mid-publication
infrastructure failure can therefore leave a mix of old and new table versions;
rerunning the deterministic batch repairs that state.

## Gold views

`src/crestline_gold/sql/gold_views.sql` contains exactly three version-controlled
Databricks SQL view definitions:

- `order_line_fulfillment` pre-aggregates shipments at order-line grain and
  retains every shipment in a sorted evidence array. Carrier estimates apply
  only to their dispatched shipment quantities.
- `backorder_eta_evidence` selects the latest complete supplier record by its
  source timestamps while retaining every revision. A NULL date on the newest
  record remains NULL; supplier restock estimates are never relabeled as
  hospital delivery dates.
- `product_region_inventory` selects one latest snapshot per warehouse and SKU
  before regional aggregation. Its region-product grid distinguishes a missing
  observation (NULL quantities) from an observed zero.

These views expose hospital and region routing keys but are not an authorization
boundary. The demo access fixture is not joined. Trusted identity filters and
Unity Catalog controls must be applied before downstream tools retrieve scoped
data. Product attributes and stock are evidence for evaluating a separately
sourced candidate; the SQL does not infer clinical or contractual approval.

The `gold_views` serverless wheel task publishes the definitions in dependency
order. View replacement is individually atomic, not a three-view transaction;
a failed run should be rerun. At larger scale, the evidence arrays and live view
aggregations may need bounded history or materialization, but neither is needed
for this synthetic assignment.

Read-only reconciliation queries are in `sql/gold_verification.sql`.

## Local document extraction

`crestline_documents` reconciles all PDFs against the CSV manifest and verifies
each SHA-256 before parsing. It writes four citation-ready JSONL datasets:
`document_pages`, `substitution_guide_rows`, `contract_clauses`, and
`ifu_product_facts`. Each record retains the canonical manifest ID, source file,
page, effective date, version, content hash, and extraction method.

Embedded PDF text and substitution tables are parsed deterministically. Table
cell line breaks are preserved, as are current, superseded, rejected, and
pending statuses. Contract amendments are emitted as scoped override clauses
linked to their base contract. Confidential pricing lines are redacted from
page retrieval text and are never emitted as contract facts.

The one image-only supplement uses local Tesseract only when its executable is
available. Tesseract is not assumed to exist on Databricks serverless. Without
it—or if OCR is incomplete—the page is marked `requires_review` or
`quarantined`, and no substitution fact is emitted. An injected managed OCR
adapter can replace this local command when the records are later published to
Databricks.

From the repository root:

```bash
PYTHONPATH=src python -m crestline_documents.main \
  --manifest crestline_synthetic_v1/source_csv/document_manifest.csv \
  --pdf-directory crestline_synthetic_v1/source_pdfs \
  --output-directory build/document_extraction \
  --fail-on-review
```

Use `--ocr-mode disabled` to verify fail-closed behavior. The command only
reads the synthetic source directory; generated JSONL is written beneath the
selected output directory and is ready for a later typed Delta publication
step.

## Publish document facts to Silver

Document publication is a separate, repeatable serverless batch. The local
preparation command re-runs extraction against the actual PDFs, verifies all 13
manifest SHA-256 values, enforces the four exact schemas and expected counts,
rejects duplicate fact keys or review records, and writes a content-addressed
artifact directory. The generated `publication_manifest.json` binds the four
JSONL hashes, row counts, key fields, source-manifest hash, and document
identities to one `artifact_set_id`.

```bash
PYTHONPATH=src python -m crestline_documents.prepare_main \
  --manifest crestline_synthetic_v1/source_csv/document_manifest.csv \
  --pdf-directory crestline_synthetic_v1/source_pdfs \
  --output-root build/document_publication
```

For the checked synthetic fixture, this deterministically produces artifact set:

```text
d2d0f6a447e4c51bba4c9f55ce5a5a8bed32c67d6ecf9de9a32fde7f727377c3
```

The preparation command requires local Tesseract for the one image-only page.
It fails instead of creating a publishable set if OCR is unavailable, any
document is incomplete, any review status remains, or sensitive pricing is
detected. The JSONL contains only redacted page retrieval text and scoped
contract clauses; it never contains raw contract pages.

After approval, stage the immutable files in the existing Unity Catalog Volume.
Upload `publication_manifest.json` last so an incomplete directory cannot look
ready. These commands use the existing CLI profile and contain no credentials:

```bash
ARTIFACT_SET_ID=d2d0f6a447e4c51bba4c9f55ce5a5a8bed32c67d6ecf9de9a32fde7f727377c3
LOCAL_ARTIFACT_ROOT=build/document_publication
REMOTE_ARTIFACT_ROOT=dbfs:/Volumes/workspace/crestline_bronze/mediassist_ingestion/document_artifacts

databricks fs mkdir "${REMOTE_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}" \
  --profile "abinroy90@gmail.com"
databricks fs cp "${LOCAL_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/document_pages.jsonl" \
  "${REMOTE_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/document_pages.jsonl" \
  --profile "abinroy90@gmail.com"
databricks fs cp "${LOCAL_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/substitution_guide_rows.jsonl" \
  "${REMOTE_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/substitution_guide_rows.jsonl" \
  --profile "abinroy90@gmail.com"
databricks fs cp "${LOCAL_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/contract_clauses.jsonl" \
  "${REMOTE_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/contract_clauses.jsonl" \
  --profile "abinroy90@gmail.com"
databricks fs cp "${LOCAL_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/ifu_product_facts.jsonl" \
  "${REMOTE_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/ifu_product_facts.jsonl" \
  --profile "abinroy90@gmail.com"
databricks fs cp "${LOCAL_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/publication_manifest.json" \
  "${REMOTE_ARTIFACT_ROOT}/${ARTIFACT_SET_ID}/publication_manifest.json" \
  --profile "abinroy90@gmail.com"
```

Do not use `--overwrite` for a content-addressed directory. If that path already
exists, compare or remove the incomplete remote directory only after explicit
review; a differing file under the same ID is a failed integrity check.

Validate and deploy the updated bundle, then run the one publication job:

```bash
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
databricks bundle validate -t dev --profile "abinroy90@gmail.com"
databricks bundle deploy -t dev --profile "abinroy90@gmail.com"
databricks bundle run document_publication -t dev \
  --profile "abinroy90@gmail.com" \
  --params artifact_set_id=d2d0f6a447e4c51bba4c9f55ce5a5a8bed32c67d6ecf9de9a32fde7f727377c3
```

The job rechecks the artifact-set identity, every JSONL hash, exact schema,
counts, document/source hashes, unique fact keys, redaction, and scanned-row
status before preparing any table write. It then uses deterministic
`CREATE OR REPLACE TABLE ... USING DELTA` statements for exactly four tables in
`workspace.crestline_silver`. Rerunning the same artifact set replaces each
table with the same rows and cannot append duplicates.

All four inputs are validated and converted to Spark DataFrames before the
first replacement. Unity Catalog/Delta does not provide a transaction spanning
four tables, so an infrastructure failure during the replacement loop can
still leave a mix of table versions. The content-addressed rerun is the repair
procedure, and each table records `mediassist.artifact_set_id` as a Delta table
property for reconciliation.

Required permissions are `USE CATALOG` on `workspace`, `USE SCHEMA` and
`CREATE TABLE`/`MODIFY` on `workspace.crestline_silver`, `READ VOLUME` on the
existing ingestion Volume, plus permission to create/run the bundle job. No new
schema, Volume, compute, or cloud resource is provisioned.

## Prepare document retrieval chunks

The `document_retrieval_chunks` serverless wheel task reads only
`substitution_guide_rows` and `ifu_product_facts`. It creates exactly 15
structure-aware records: 11 complete guide rows and four complete IFU facts.
Each `embedding_text` keeps the SKU pair, exact evidence status, required
attributes, and clinical note together; no contract clause, document page,
hospital identifier, or pricing content is selected.

Stable chunk keys are derived from the already deterministic source fact keys
(`substitution:<substitution_row_id>` or `ifu:<ifu_fact_id>`). Count,
document-distribution, provenance, status, scanned-OCR,
non-interchangeability, unique-key, and sensitive-content checks run before the
target is replaced.
The resulting Delta table has Change Data Feed enabled for a later Standard
Delta Sync index. This job prepares the source table only; it does not create a
Vector Search endpoint or index.

After the four document tables have been published, the approved deployment and
run commands are:

```bash
databricks bundle validate -t dev --profile "abinroy90@gmail.com"
databricks bundle deploy -t dev --profile "abinroy90@gmail.com"
databricks bundle run document_retrieval_chunks -t dev \
  --profile "abinroy90@gmail.com"
```

Read-only post-run checks are in `sql/document_retrieval_verification.sql`.
The task requires `USE CATALOG` on `workspace`, `USE SCHEMA`, `SELECT`,
`CREATE TABLE`, and `MODIFY` on `workspace.crestline_silver`, plus permission to
deploy and run the bundle job. A later Vector Search principal will separately
need `SELECT` on the prepared table.

## Deterministic retrieval and validation

`crestline_retrieval.RetrievalService` is the read-only boundary for future
agent tools. It exposes exact order visibility, supplier ETA evidence, regional
inventory, authorized contract rules, hybrid document discovery, and
deterministic substitution verification. SQL values use named parameters, and
the Databricks SDK adapter accepts an already authenticated `WorkspaceClient`
plus an existing SQL warehouse ID; credentials are never stored by the package.

The caller must construct `AuthorizationContext` from trusted server-side
identity and policy data. Hospital and region filters are applied in SQL before
rows are returned. Contract clauses are queried only after hospital
authorization, use an explicit pricing-free projection, and are rejected if a
pricing pattern nevertheless crosses the boundary. The Vector Search method
queries only the existing 15-record public-product index with `HYBRID` search.
Its score is labeled `discovery_score` and is never consumed by substitution
verification.

`verify_substitution` independently retrieves the exact SKU pair, current and
superseded guide evidence, both current IFUs, hospital contract/amendments,
regional inventory, and—when a conditional amendment requires it—the exact
order-line backorder. It returns only `ELIGIBLE_FOR_HUMAN_REVIEW`, `BLOCKED`,
`INSUFFICIENT_EVIDENCE`, or `ACCESS_DENIED`. Eligibility still requires human
clinical review; question wording, semantic similarity, historical approval,
and claimed emergency or patient exceptions cannot alter the outcome.

Runtime integration still requires an application/service principal with
`SELECT` on the three Gold views and four document tables, permission to query
the existing Vector Search index, and `CAN USE` on an existing SQL warehouse.
The application must derive `AuthorizationContext` from authenticated claims or
a real policy store—not `rep_access_demo_only`, request text, or LLM output.
Install the adapter outside a Databricks runtime with
`pip install '.[retrieval]'`; existing data jobs do not need that optional SDK
dependency.

The read-only live smoke test uses a fixed authorization context named
`DEMO_ONLY_live_integration_test`; it is intentionally independent of the demo
access table and must not be reused for production authorization. From the
repository root, after installing the optional retrieval dependency, run:

```bash
PYTHONPATH=src python -m crestline_retrieval.live_integration \
  --profile "abinroy90@gmail.com" \
  --warehouse-id "d249a9e5325ec021" \
  --vector-index "workspace.crestline_silver.document_retrieval_chunks_index"
```

The command issues only parameterized `SELECT` statements and one HYBRID index
query. It prints six PASS/FAIL lines plus a summary and exits nonzero if any
scenario or citation assertion fails. Identical reads are cached for the life
of the process to avoid repeating guide, IFU, contract, or inventory queries.

## Local LangGraph agent

`crestline_agent.MediAssistAgent` is one explicit LangGraph state machine:

```text
authorization-scope guard -> intent router -> one typed retrieval tool -> response
```

It routes order tracking, supplier ETA/backorders, inventory, contract rules,
substitution verification, public document discovery, and clarification. The
existing `RetrievalService` remains the only business-logic layer. Databricks
model serving is used narrowly for JSON intent/slot classification when the
deterministic router cannot classify a request; model text is never used as an
answer or as approval evidence.

An in-memory LangGraph checkpointer is keyed by `thread_id` and retains only
short-term entity references and safe rendered responses. Raw request text and
transient slots are cleared at the end of a turn. A fingerprint binds each
thread to the trusted principal/hospital/region scope that created it. The full
`AuthorizationContext` is injected afresh through LangGraph runtime context on
every request and is never loaded from checkpoint state. Reusing a thread under
a different scope returns `ACCESS_DENIED` before any retrieval call.

Install the local agent dependencies:

```bash
python -m pip install '.[agent]'
```

Choose an already deployed Databricks chat-model endpoint; the project does not
assume or provision one. Then start the East synthetic demo scope:

```bash
export DATABRICKS_LLM_ENDPOINT="<existing-databricks-chat-endpoint>"
PYTHONPATH=src python -m crestline_agent.cli \
  --profile "abinroy90@gmail.com" \
  --warehouse-id "d249a9e5325ec021" \
  --vector-index "workspace.crestline_silver.document_retrieval_chunks_index" \
  --demo-scope east
```

The CLI scope is labeled `DEMO_ONLY` and is rebuilt on every turn. A deployed
application must replace it with authorization derived from authenticated
server-side identity and policy data. Neither checkpoint memory, request text,
the LLM, nor `rep_access_demo_only` is a production authorization source.

## Streamlit workspace

The Streamlit interface is a thin presentation layer over the same
`MediAssistAgent` and `RetrievalService`; it does not duplicate retrieval or
substitution rules. Each browser session receives its own LangGraph agent and
thread ID. Changing the East/West `DEMO_ONLY` scope starts a new conversation,
and trusted authorization is rebuilt on every request rather than read from
chat state.

Install the UI dependencies and start the local workspace:

```bash
python -m pip install '.[ui]'
export DATABRICKS_LLM_ENDPOINT="<existing-databricks-chat-endpoint>"
PYTHONPATH=src streamlit run src/crestline_ui/app.py
```

For local development, the app keeps the authenticated CLI profile
`abinroy90@gmail.com`, warehouse `d249a9e5325ec021`, and existing retrieval
index as defaults. Override them with `DATABRICKS_CONFIG_PROFILE`,
`MEDIASSIST_WAREHOUSE_ID`, and `MEDIASSIST_VECTOR_INDEX`. A configured status
means the required settings are present; it is not a substitute for a live
health check.

## Databricks Apps deployment

The root `app.yaml` starts Streamlit on `0.0.0.0:8000`. It sets
`PYTHONPATH=src` so application modules resolve from the immutable release
source ahead of any previously installed distribution, and resolves the
warehouse, AI Search index, and model
serving endpoint from managed app-resource keys. `requirements.txt` installs
this repository as a normal package with its `ui` extra; it does not depend on
the local virtual environment or an editable install. The deployment manifest
defines its release-local Python path explicitly.

At process startup, the app logs only code-provenance diagnostics: the loaded
`crestline_agent.graph` path and SHA-256, installed package version, and a
non-sensitive build identifier. Set `MEDIASSIST_BUILD_ID` to a release label
containing only letters, numbers, dots, underscores, or hyphens; otherwise the
identifier is derived from the package version and graph hash. Credentials,
authorization state, hospital data, and conversations are never included.

At runtime, Databricks Apps injects the dedicated app service principal's OAuth
credentials. When `DATABRICKS_CLIENT_ID` is present, the UI constructs
`WorkspaceClient()` without a named profile, token, or secret. The developer's
CLI profile remains only a local/deployment credential and is never configured
inside the app.

`conf/databricks_app_resources.json` is the complete least-privilege app
resource definition. It binds:

- SQL warehouse `d249a9e5325ec021` with `CAN_USE`.
- Serving endpoint `databricks-qwen3-next-80b-a3b-instruct` with `CAN_QUERY`.
- AI Search index
  `workspace.crestline_silver.document_retrieval_chunks_index` with `SELECT`.
  AI Search indexes are Unity Catalog table securables, so this binding also
  supplies the index's three-part name to `MEDIASSIST_VECTOR_INDEX`.
- `SELECT` on the three Gold views and three Silver document tables read by
  `RetrievalService`.

The principal also needs `USE CATALOG` on `workspace` and `USE SCHEMA` on
`crestline_gold` and `crestline_silver`. Adding these Unity Catalog resources
through Databricks Apps grants the parent privileges when the deploying user
has sufficient `MANAGE` rights. The deployer therefore needs `CAN MANAGE` on
the app, warehouse, and serving endpoint, plus `MANAGE` (or equivalent grant
authority) on the catalog, schemas, views/tables, and AI Search index. No
data-plane `MODIFY`, job-run, Volume-write, or infrastructure-creation
permission is required. If the AI Search index uses a separately permissioned
custom or route-optimized query embedding endpoint, also grant the app service principal
`CAN QUERY` on that endpoint; the existing managed embedding configuration
should be confirmed once in the workspace.

The East/West selector remains explicitly `DEMO_ONLY` synthetic scope. It is
rebuilt on every request and is not production identity authorization. Before
production use, replace it with policy derived from authenticated server-side
identity; never use chat text, checkpoint state, or
`rep_access_demo_only` as an authorization source.

### Create and deploy after approval

These commands intentionally use the developer's OAuth profile only to perform
deployment. The first command creates the stopped app and its dedicated service
principal/resource grants; it does not forward user credentials:

```bash
databricks apps create mediassist-ai \
  --json @conf/databricks_app_resources.json \
  --no-compute \
  --profile "abinroy90@gmail.com"
```

If the app already exists, apply the complete resource definition with
`apps update` instead of `apps create` (the update API is full replacement):

```bash
databricks apps update mediassist-ai \
  --json @conf/databricks_app_resources.json \
  --profile "abinroy90@gmail.com"
```

After running the local validation commands in the next section, sync an
immutable release folder, deploy a snapshot, and start the app:

```bash
databricks sync . \
  /Workspace/Users/abinroy90@gmail.com/mediassist-ai/releases/<release-id> \
  --exclude-from conf/databricks_app_sync_excludes.txt \
  --profile "abinroy90@gmail.com"
databricks apps deploy mediassist-ai \
  --source-code-path /Workspace/Users/abinroy90@gmail.com/mediassist-ai/releases/<release-id> \
  --mode SNAPSHOT \
  --profile "abinroy90@gmail.com"
databricks apps start mediassist-ai --profile "abinroy90@gmail.com"
databricks apps get mediassist-ai --profile "abinroy90@gmail.com"
databricks apps logs mediassist-ai --profile "abinroy90@gmail.com"
```

Use a unique version or commit SHA for `<release-id>` and retain the prior
release folder. To roll back, inspect deployment history and redeploy the prior
immutable source snapshot; this does not delete the app or its resource
bindings:

```bash
databricks apps list-deployments mediassist-ai --profile "abinroy90@gmail.com"
databricks apps deploy mediassist-ai \
  --source-code-path /Workspace/Users/abinroy90@gmail.com/mediassist-ai/releases/<previous-release-id> \
  --mode SNAPSHOT \
  --profile "abinroy90@gmail.com"
```

## Golden conversational evaluation

For a temporary public, synthetic-data-only mirror on Streamlit Community
Cloud, see [the Community Cloud deployment guide](docs/deployment/streamlit_community_cloud.md).

The packaged `crestline_eval/golden_scenarios.json` is the canonical,
version-controlled demonstration suite. It covers order quantities, the
supplier-restock-versus-delivery distinction, multi-turn candidate discovery,
H-E101 and H-E102 contract behavior, rejected latex evidence, unknown dates,
East/West boundaries, hospital-context changes, and new-session isolation.
Assertions target structured retrieval results, trusted call parameters,
reason codes, evidence identifiers, and forbidden safety claims rather than
exact response wording.

Run the deterministic local suite from the repository root. Critical scenarios
run three times with fresh LangGraph threads, and reports are written as JSON
and Markdown:

```bash
PYTHONPATH=src python -m crestline_eval.runner \
  --repetitions 3 \
  --report-directory build/evaluation/local
```

Live evaluation is explicitly opt-in and read-only. It rebuilds the named
`DEMO_ONLY` authorization scope on every turn, uses only parameterized SQL,
HYBRID Vector Search, and the configured classifier endpoint, and caches
identical reads within the run. It does not use a typed hospital identifier as
authorization and does not create or modify workspace resources:

```bash
PYTHONPATH=src python -m crestline_eval.runner \
  --live \
  --profile "abinroy90@gmail.com" \
  --warehouse-id "d249a9e5325ec021" \
  --vector-index "workspace.crestline_silver.document_retrieval_chunks_index" \
  --llm-endpoint "databricks-qwen3-next-80b-a3b-instruct" \
  --repetitions 3 \
  --report-directory build/evaluation/live
```

Either mode exits nonzero if a business fact, outcome, citation, SKU direction,
scope boundary, context transition, unknown-date rule, forbidden claim, or
repeated-run stability signature fails. Latency is reported for observation but
does not determine pass/fail. Semantic matches remain discovery evidence only.

The installed console entry point `mediassist_evaluate` accepts the same
arguments.

## Local validation

```bash
python -m pip install '.[dev]'
pytest
python3 -m compileall -q src tests
mypy src tests
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

The final v4 validation completed with **168 automated tests passing** and
**36/36 live golden evaluation checks passing**. Generated JSON and Markdown
evaluation reports are intentionally excluded from Git by the `reports/`
entry in `.gitignore`.

PySpark and Delta Lake are Databricks runtime dependencies and are imported only
inside runtime functions. Local unit tests validate the registry, fixture
headers, identity behavior, and generated validation predicates without needing
a local Spark installation.

## Deployment prerequisites

- Renewed Databricks CLI authentication. Supply the operator's profile with
  `--profile <profile-name>` on deployment commands; the bundle no longer embeds
  a personal profile.
- Existing catalog and schemas: `workspace.crestline_bronze`,
  `workspace.crestline_silver`, and `workspace.crestline_gold`.
- An existing Unity Catalog Volume, by default
  `workspace.crestline_bronze.mediassist_ingestion`.
- Permission to read/write that Volume, read Bronze and Silver tables,
  create/replace Gold views, create/replace Silver Delta tables, and create/run
  serverless Databricks Jobs.
- Serverless Spark support for Auto Loader and `availableNow`.

The bundle does not create the schema or Volume and contains no credentials.
This is a synthetic FDE prototype, not a production authorization or clinical
decision system: it has no live ERP, carrier, Salesforce, or IAM integration.
Production use requires identity-derived authorization, governed data policies,
qualified clinical review workflows, operational monitoring, and controlled
model/data change management.

## Validate and deploy after approval

Renew and verify authentication:

```bash
databricks auth login --host https://dbc-290bff82-e0e2.cloud.databricks.com --profile abinroy90@gmail.com
databricks current-user me --profile abinroy90@gmail.com
databricks schemas get workspace.crestline_bronze --profile abinroy90@gmail.com
databricks volumes list workspace crestline_bronze --profile abinroy90@gmail.com
```

Validate and deploy the bundle:

```bash
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
databricks bundle validate -t dev --profile abinroy90@gmail.com
databricks bundle deploy -t dev --profile abinroy90@gmail.com
```

Upload each CSV to its registered landing subdirectory. For example:

```bash
databricks fs cp crestline_synthetic_v1/source_csv/orders.csv \
  dbfs:/Volumes/workspace/crestline_bronze/mediassist_ingestion/landing/orders/orders.csv \
  --profile abinroy90@gmail.com
```

Use the analogous directory for each source. The demo access filename maps to
the registered directory `rep_access_demo_only`.

Run every registered source or one source:

```bash
databricks bundle run bronze_ingestion -t dev --profile abinroy90@gmail.com
databricks bundle run bronze_ingestion -t dev --profile abinroy90@gmail.com \
  --params source_id=orders
databricks bundle run silver_batch -t dev --profile abinroy90@gmail.com
databricks bundle run gold_views -t dev --profile abinroy90@gmail.com
```

Remote validation, provisioning, upload, deployment, and execution require
separate approval.

## Bootstrap synthetic CSVs into the Volume

`crestline_bronze.bootstrap` reads the same source registry used by ingestion.
Before contacting Databricks it verifies that all nine registered `source_file`
values exist under the local source directory. It then creates missing landing
subdirectories and uploads each file through the Databricks CLI profile without
reading or storing credentials.

Existing remote files are downloaded with `databricks fs cat` and compared byte
for byte. Identical files are logged and skipped. A same-named file with
different content is treated as a failure unless `--overwrite-changed` is
explicitly supplied.

From the repository root, the approved upload command is:

```bash
PYTHONPATH=src python -m crestline_bronze.bootstrap \
  --config src/crestline_bronze/sources.yml \
  --source-directory crestline_synthetic_v1/source_csv \
  --volume-root dbfs:/Volumes/workspace/crestline_bronze/mediassist_ingestion \
  --profile abinroy90@gmail.com
```

The safer default command never overwrites changed content. After reviewing a
reported difference, an intentional replacement can be requested by adding:

```bash
--overwrite-changed
```

The utility logs every created/existing directory, uploaded/skipped file,
failure, and final count summary. It never writes to the local synthetic source
directory.
