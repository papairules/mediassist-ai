"""Build the structure-aware Delta source for future Databricks AI Search."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


EXPECTED_GUIDE_ROWS = 11
EXPECTED_IFU_FACTS = 4
EXPECTED_CHUNKS = EXPECTED_GUIDE_ROWS + EXPECTED_IFU_FACTS
TARGET_TABLE = "document_retrieval_chunks"
TEMP_VIEW = "_mediassist_document_retrieval_chunks"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GUIDE_DOCUMENT_IDS = (
    "SUB_GUIDE_2025_V1_SUPERSEDED",
    "SUB_GUIDE_2026_V2_CURRENT",
    "SUB_SUPPLEMENT_SCANNED_2026",
)
IFU_DOCUMENT_IDS = (
    "IFU_AV-IV22-BLU-LF",
    "IFU_BR-IV20-PNK-LF",
    "IFU_BR-IV22-BLU-LF",
    "IFU_BR-IV22-BLU-LX",
)
GUIDE_ROW_STATUSES = (
    "APPROVED",
    "APPROVED (REVOKED)",
    "PENDING_REVIEW",
    "PENDING CLINICAL REVIEW",
    "REJECTED",
)
OUTPUT_COLUMNS = (
    "chunk_id",
    "chunk_type",
    "embedding_text",
    "source_record_id",
    "substitution_row_id",
    "ifu_fact_id",
    "table_number",
    "table_row_number",
    "document_id",
    "display_document_id",
    "document_version",
    "document_status",
    "effective_from",
    "page_number",
    "source_file",
    "content_sha256",
    "extraction_method",
    "extractor_name",
    "extractor_version",
    "extraction_status",
    "review_reasons",
    "requested_sku",
    "candidate_sku",
    "row_status",
    "required_attributes",
    "product_sku",
    "ifu_id",
    "revision",
    "gauge",
    "device_length_mm",
    "latex_free",
    "sterile",
    "single_use",
    "establishes_interchangeability",
    "clinical_note",
)


class RetrievalValidationError(RuntimeError):
    """Raised before publication when retrieval-source invariants fail."""


@dataclass(frozen=True)
class PreparedRetrieval:
    guide_rows: int
    ifu_facts: int
    total_chunks: int
    target_table: str


def _identifier(value: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise RetrievalValidationError(f"Unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def _qualified(*parts: str) -> str:
    return ".".join(_identifier(part) for part in parts)


def build_projection_sql(guide_table: str, ifu_table: str) -> str:
    """Return the deterministic 11-row plus 4-row structure-aware projection."""
    guide = f"""SELECT
  concat('substitution:', substitution_row_id) AS chunk_id,
  'substitution_guide_row' AS chunk_type,
  concat_ws('\\n',
    'Evidence type: substitution guide row',
    concat('Requested SKU: ', requested_sku),
    concat('Candidate SKU: ', candidate_sku),
    concat('Row status: ', row_status),
    CASE WHEN required_attributes IS NOT NULL
      THEN concat('Required attributes: ', required_attributes) END,
    CASE WHEN clinical_note IS NOT NULL
      THEN concat('Clinical note: ', clinical_note) END,
    concat('Document version: ', document_version),
    concat('Document status: ', document_status),
    concat('Effective date: ', CAST(effective_from AS STRING))
  ) AS embedding_text,
  substitution_row_id AS source_record_id,
  substitution_row_id,
  CAST(NULL AS STRING) AS ifu_fact_id,
  table_number,
  table_row_number,
  document_id,
  display_document_id,
  document_version,
  document_status,
  effective_from,
  page_number,
  source_file,
  content_sha256,
  extraction_method,
  extractor_name,
  extractor_version,
  extraction_status,
  review_reasons,
  requested_sku,
  candidate_sku,
  row_status,
  required_attributes,
  CAST(NULL AS STRING) AS product_sku,
  CAST(NULL AS STRING) AS ifu_id,
  CAST(NULL AS STRING) AS revision,
  CAST(NULL AS BIGINT) AS gauge,
  CAST(NULL AS BIGINT) AS device_length_mm,
  CAST(NULL AS BOOLEAN) AS latex_free,
  CAST(NULL AS BOOLEAN) AS sterile,
  CAST(NULL AS BOOLEAN) AS single_use,
  CAST(NULL AS BOOLEAN) AS establishes_interchangeability,
  clinical_note
FROM {guide_table}"""

    ifu = f"""SELECT
  concat('ifu:', ifu_fact_id) AS chunk_id,
  'ifu_product_fact' AS chunk_type,
  concat_ws('\\n',
    'Evidence type: manufacturer IFU product fact',
    concat('Product SKU: ', product_sku),
    concat('IFU ID: ', ifu_id),
    concat('Revision: ', revision),
    concat('Gauge: ', CAST(gauge AS STRING), 'G'),
    concat('Device length: ', CAST(device_length_mm AS STRING), ' mm'),
    concat('Latex-free: ', CASE WHEN latex_free THEN 'true' ELSE 'false' END),
    concat('Sterile: ', CASE WHEN sterile THEN 'true' ELSE 'false' END),
    concat('Single use: ', CASE WHEN single_use THEN 'true' ELSE 'false' END),
    concat('Clinical note: ', clinical_note),
    concat('Establishes interchangeability: ',
      CASE WHEN establishes_interchangeability THEN 'true' ELSE 'false' END),
    concat('Document version: ', document_version),
    concat('Document status: ', document_status),
    concat('Effective date: ', CAST(effective_from AS STRING))
  ) AS embedding_text,
  ifu_fact_id AS source_record_id,
  CAST(NULL AS STRING) AS substitution_row_id,
  ifu_fact_id,
  CAST(NULL AS BIGINT) AS table_number,
  CAST(NULL AS BIGINT) AS table_row_number,
  document_id,
  display_document_id,
  document_version,
  document_status,
  effective_from,
  page_number,
  source_file,
  content_sha256,
  extraction_method,
  extractor_name,
  extractor_version,
  extraction_status,
  review_reasons,
  CAST(NULL AS STRING) AS requested_sku,
  CAST(NULL AS STRING) AS candidate_sku,
  CAST(NULL AS STRING) AS row_status,
  CAST(NULL AS STRING) AS required_attributes,
  product_sku,
  ifu_id,
  revision,
  gauge,
  device_length_mm,
  latex_free,
  sterile,
  single_use,
  establishes_interchangeability,
  clinical_note
FROM {ifu_table}"""
    return f"{guide}\nUNION ALL\n{ifu}"


def build_source_required_check_sql(table: str, record_id: str) -> str:
    required = (
        record_id,
        "document_id",
        "display_document_id",
        "document_version",
        "document_status",
        "effective_from",
        "page_number",
        "source_file",
        "content_sha256",
        "extraction_method",
        "extractor_name",
        "extractor_version",
        "extraction_status",
        "review_reasons",
        "clinical_note",
    )
    missing = " OR ".join(f"{_identifier(column)} IS NULL" for column in required)
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {table}\n"
        f"WHERE {missing}\n"
        "  OR page_number < 1\n"
        "  OR content_sha256 NOT RLIKE '^[0-9a-f]{64}$'\n"
        "  OR extraction_status <> 'extracted'"
    )


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def build_guide_semantics_check_sql(table: str) -> str:
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {table}\n"
        "WHERE requested_sku IS NULL\n"
        "  OR candidate_sku IS NULL\n"
        f"  OR row_status NOT IN ({_quoted(GUIDE_ROW_STATUSES)})\n"
        f"  OR document_id NOT IN ({_quoted(GUIDE_DOCUMENT_IDS)})\n"
        "  OR document_status NOT IN ('current', 'superseded')\n"
        "  OR (document_status = 'superseded' AND document_id <> "
        "'SUB_GUIDE_2025_V1_SUPERSEDED')\n"
        "  OR (document_id = 'SUB_GUIDE_2025_V1_SUPERSEDED' "
        "AND document_status <> 'superseded')\n"
        "  OR (row_status = 'APPROVED (REVOKED)' AND document_status <> 'superseded')"
    )


def build_ifu_semantics_check_sql(table: str) -> str:
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {table}\n"
        "WHERE product_sku IS NULL\n"
        "  OR ifu_id IS NULL\n"
        "  OR revision IS NULL\n"
        "  OR gauge IS NULL\n"
        "  OR device_length_mm IS NULL\n"
        "  OR latex_free IS NULL\n"
        "  OR sterile IS NULL\n"
        "  OR single_use IS NULL\n"
        "  OR establishes_interchangeability IS NULL\n"
        "  OR establishes_interchangeability <> false\n"
        "  OR document_status <> 'current'\n"
        f"  OR document_id NOT IN ({_quoted(IFU_DOCUMENT_IDS)})"
    )


def build_source_distribution_check_sql(table: str, source: str) -> str:
    """Require the exact manifest-backed document distribution for this fixture."""
    conditions: tuple[str, ...]
    if source == "guide":
        conditions = (
            "count(*) = 11",
            "count_if(document_id = 'SUB_GUIDE_2025_V1_SUPERSEDED') = 3",
            "count_if(document_id = 'SUB_GUIDE_2026_V2_CURRENT') = 7",
            "count_if(document_id = 'SUB_SUPPLEMENT_SCANNED_2026') = 1",
        )
    elif source == "ifu":
        conditions = ("count(*) = 4",) + tuple(
            f"count_if(document_id = '{document_id}') = 1"
            for document_id in IFU_DOCUMENT_IDS
        )
    else:
        raise RetrievalValidationError(f"Unknown retrieval source: {source!r}")
    predicate = "\n    AND ".join(conditions)
    return (
        "SELECT CASE WHEN\n"
        f"    {predicate}\n"
        "  THEN 0 ELSE 1 END AS violation_count\n"
        f"FROM {table}"
    )


def build_scanned_safety_check_sql(table: str) -> str:
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {table}\n"
        "WHERE document_id = 'SUB_SUPPLEMENT_SCANNED_2026'\n"
        "  AND (row_status <> 'PENDING CLINICAL REVIEW'\n"
        "    OR extraction_method <> 'tesseract_ocr'\n"
        "    OR NOT array_contains(review_reasons, 'clinical_review_required'))"
    )


def build_retrieval_integrity_check_sql(view: str) -> str:
    return (
        "SELECT count(*) AS violation_count\n"
        f"FROM {_identifier(view)}\n"
        "WHERE chunk_id IS NULL\n"
        "  OR embedding_text IS NULL\n"
        "  OR trim(embedding_text) = ''\n"
        "  OR chunk_type NOT IN ('substitution_guide_row', 'ifu_product_fact')\n"
        "  OR document_id RLIKE '^(CONTRACT|AMENDMENT)'\n"
        "  OR lower(embedding_text) RLIKE "
        "'(confidential negotiated pricing|pricing:[[:space:]]*confidential|"
        "contract[[:space:]]+price|unit[[:space:]]+price|negotiated[[:space:]]+price|"
        "\\$[[:space:]]*[0-9])'"
    )


def build_duplicate_check_sql(view: str) -> str:
    return (
        "SELECT count(*) AS violation_count\n"
        "FROM (\n"
        "  SELECT chunk_id\n"
        f"  FROM {_identifier(view)}\n"
        "  GROUP BY chunk_id\n"
        "  HAVING count(*) > 1\n"
        ") duplicates"
    )


def _scalar(spark: Any, sql: str, field: str) -> int:
    row = spark.sql(sql).first()
    if row is None:
        raise RetrievalValidationError(f"Validation query returned no result: {sql}")
    try:
        value = row[field]
    except (KeyError, TypeError):
        value = row[0]
    return int(value)


def _require_zero(spark: Any, name: str, sql: str) -> None:
    violations = _scalar(spark, sql, "violation_count")
    if violations:
        raise RetrievalValidationError(f"{name} failed with {violations} violation(s)")


def run_retrieval_preparation(
    spark: Any,
    catalog: str = "workspace",
    silver_schema: str = "crestline_silver",
) -> PreparedRetrieval:
    """Validate both document sources, then replace one CDF-enabled Delta table."""
    guide_table = _qualified(catalog, silver_schema, "substitution_guide_rows")
    ifu_table = _qualified(catalog, silver_schema, "ifu_product_facts")
    target = _qualified(catalog, silver_schema, TARGET_TABLE)
    spark.conf.set("spark.sql.session.timeZone", "UTC")

    guide_count = _scalar(
        spark,
        f"SELECT count(*) AS source_count FROM {guide_table}",
        "source_count",
    )
    ifu_count = _scalar(
        spark,
        f"SELECT count(*) AS source_count FROM {ifu_table}",
        "source_count",
    )
    if guide_count != EXPECTED_GUIDE_ROWS or ifu_count != EXPECTED_IFU_FACTS:
        raise RetrievalValidationError(
            "Document retrieval requires exactly "
            f"{EXPECTED_GUIDE_ROWS} guide rows and {EXPECTED_IFU_FACTS} IFU facts; "
            f"found {guide_count} and {ifu_count}"
        )

    checks = (
        (
            "substitution_guide_rows.required_provenance",
            build_source_required_check_sql(guide_table, "substitution_row_id"),
        ),
        (
            "ifu_product_facts.required_provenance",
            build_source_required_check_sql(ifu_table, "ifu_fact_id"),
        ),
        (
            "substitution_guide_rows.document_distribution",
            build_source_distribution_check_sql(guide_table, "guide"),
        ),
        ("substitution_guide_rows.semantics", build_guide_semantics_check_sql(guide_table)),
        ("substitution_guide_rows.scanned_safety", build_scanned_safety_check_sql(guide_table)),
        (
            "ifu_product_facts.document_distribution",
            build_source_distribution_check_sql(ifu_table, "ifu"),
        ),
        ("ifu_product_facts.semantics", build_ifu_semantics_check_sql(ifu_table)),
    )
    for name, sql in checks:
        _require_zero(spark, name, sql)

    spark.sql(
        f"CREATE OR REPLACE TEMP VIEW {_identifier(TEMP_VIEW)} AS\n"
        f"{build_projection_sql(guide_table, ifu_table)}"
    )
    total = _scalar(
        spark,
        f"SELECT count(*) AS retrieval_count FROM {_identifier(TEMP_VIEW)}",
        "retrieval_count",
    )
    if total != EXPECTED_CHUNKS:
        raise RetrievalValidationError(
            f"Expected {EXPECTED_CHUNKS} retrieval chunks; found {total}"
        )
    _require_zero(
        spark,
        "document_retrieval_chunks.duplicate_keys",
        build_duplicate_check_sql(TEMP_VIEW),
    )
    _require_zero(
        spark,
        "document_retrieval_chunks.integrity",
        build_retrieval_integrity_check_sql(TEMP_VIEW),
    )

    projection = ", ".join(_identifier(column) for column in OUTPUT_COLUMNS)
    spark.sql(
        f"CREATE OR REPLACE TABLE {target}\n"
        "USING DELTA\n"
        "TBLPROPERTIES (\n"
        "  'delta.enableChangeDataFeed' = 'true',\n"
        "  'mediassist.retrieval_chunk_version' = '1'\n"
        ")\n"
        f"AS SELECT {projection} FROM {_identifier(TEMP_VIEW)}"
    )
    return PreparedRetrieval(guide_count, ifu_count, total, target)
