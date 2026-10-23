# MediAssist AI — Phase 1 Bronze ingestion

This project implements metadata-driven CSV ingestion into
`workspace.crestline_bronze`. It intentionally contains no Silver/Gold business
logic, document extraction, agent code, or application code. The original
`crestline_synthetic_v1` package is treated as an immutable source fixture.

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

## Local validation

```bash
pytest
python3 -m compileall -q src tests
mypy src tests
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

PySpark and Delta Lake are Databricks runtime dependencies and are imported only
inside runtime functions. Local unit tests validate the registry, fixture
headers, identity behavior, and generated validation predicates without needing
a local Spark installation.

## Deployment prerequisites

- Renewed CLI authentication for profile `abinroy90@gmail.com`.
- Existing catalog and schema: `workspace.crestline_bronze`.
- An existing Unity Catalog Volume, by default
  `workspace.crestline_bronze.mediassist_ingestion`.
- Permission to read/write that Volume, create/modify Bronze Delta tables, and
  create/run a serverless Databricks Job.
- Serverless Spark support for Auto Loader and `availableNow`.

The bundle does not create the schema or Volume and contains no credentials.

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
