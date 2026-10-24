-- Expected: 15 chunks, 11 substitution rows, 4 IFU facts, 15 unique keys.
SELECT
  count(*) AS chunk_count,
  count_if(chunk_type = 'substitution_guide_row') AS substitution_chunk_count,
  count_if(chunk_type = 'ifu_product_fact') AS ifu_chunk_count,
  count(DISTINCT chunk_id) AS unique_chunk_count
FROM workspace.crestline_silver.document_retrieval_chunks;

-- Expected statuses: APPROVED=6, REJECTED=2, APPROVED (REVOKED)=1,
-- PENDING_REVIEW=1, PENDING CLINICAL REVIEW=1.
SELECT row_status, count(*) AS row_count
FROM workspace.crestline_silver.document_retrieval_chunks
WHERE chunk_type = 'substitution_guide_row'
GROUP BY row_status
ORDER BY row_status;

-- Expected: zero unsafe, incomplete, or semantically promoted chunks.
SELECT count(*) AS unsafe_or_incomplete_chunk_count
FROM workspace.crestline_silver.document_retrieval_chunks
WHERE chunk_id IS NULL
   OR source_record_id IS NULL
   OR document_id IS NULL
   OR document_version IS NULL
   OR document_status IS NULL
   OR effective_from IS NULL
   OR page_number < 1
   OR content_sha256 NOT RLIKE '^[0-9a-f]{64}$'
   OR extraction_method IS NULL
   OR extractor_name IS NULL
   OR extractor_version IS NULL
   OR extraction_status <> 'extracted'
   OR document_id RLIKE '^(CONTRACT|AMENDMENT)'
   OR (chunk_type = 'ifu_product_fact' AND establishes_interchangeability <> false)
   OR lower(embedding_text) RLIKE
      '(confidential negotiated pricing|pricing:[[:space:]]*confidential|contract[[:space:]]+price|unit[[:space:]]+price|negotiated[[:space:]]+price|\\$[[:space:]]*[0-9])';

-- Expected: zero duplicate stable chunk keys.
SELECT count(*) AS duplicate_chunk_key_count
FROM (
  SELECT chunk_id
  FROM workspace.crestline_silver.document_retrieval_chunks
  GROUP BY chunk_id
  HAVING count(*) > 1
) duplicates;

-- Expected: one row, exact pending status, OCR provenance, clinical review required.
SELECT
  chunk_id,
  row_status,
  extraction_method,
  review_reasons
FROM workspace.crestline_silver.document_retrieval_chunks
WHERE document_id = 'SUB_SUPPLEMENT_SCANNED_2026'
  AND row_status = 'PENDING CLINICAL REVIEW'
  AND extraction_method = 'tesseract_ocr'
  AND array_contains(review_reasons, 'clinical_review_required');

-- Expected value: true.
SHOW TBLPROPERTIES workspace.crestline_silver.document_retrieval_chunks
  ('delta.enableChangeDataFeed');
