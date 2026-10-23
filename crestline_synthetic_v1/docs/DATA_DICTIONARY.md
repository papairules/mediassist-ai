# Data dictionary and modeling rationale

| Source | Natural grain / key | Important fields | Why we need it |
|---|---|---|---|
| hospitals.csv | 1 hospital_id | hospital name, region, contract_id | Link order to contract and scope authorization |
| rep_access_DEMO_ONLY.csv | principal_id + hospital_id | role, region | Test allowed and forbidden queries (demo only) |
| products.csv | 1 SKU | brand, gauge, length, latex_free, sterile | Exact product-code lookup and deterministic substitute filters |
| orders.csv | 1 order_id | hospital_id, placed_at, erp_loaded_at | Identify order owner and snapshot age |
| order_lines.csv | order_id + line_id | sku, quantity_ordered | One order may contain multiple items; keep correct grain |
| shipments.csv | 1 shipment_id | order_id, line_id, shipped qty, carrier_status, last_scan_at, carrier_estimated_arrival | Partial fulfillment and delivery evidence; avoid treating shipment as whole order |
| inventory.csv | warehouse_id + sku + snapshot_at | region, on_hand, reserved | Availability (`on_hand - reserved`), freshness and region |
| supplier_etas.csv | 1 source_record_id | order_id, line_id, restock date, supplier_reported_at | Contradiction, null ETA, staleness; restock vs customer delivery distinction |
| document_manifest.csv | 1 document_id | path, doc_type, region, hospital_id, effective_from, status, sensitivity, sha256 | Document provenance, version control and sensitive indexing policies |
| PDF substitution guide | doc_id + source page + requested SKU + candidate SKU | approval status, specs, clinical note | Document extraction and pair validation; do not substitute from similarity alone |
| PDF hospital contract/amendment | hospital_id + clause_id + effective range | brand-lock / exception / confidential pricing | Apply active contract terms with amendment precedence; protect confidential data |
| PDF IFU | doc_id + SKU + section | physical attributes, cautions | Semantic retrieval and structured attribute cross-check; no inferred equivalence |
| ticket JSONL | ticket_id | hospital, question, reply, timestamp, quality | Distinguish plausible-but-wrong responses from authoritative evidence |

## Proposed Databricks data layers

- **Bronze**: raw imports with `source_file`, `source_record_id`, `ingested_at`, extraction status; PDF files in a governed Unity Catalog Volume.
- **Silver**: cleaned order events, item facts, document pages, table rows, effective-dated approval pairs, permitted hospital/brand clauses. Never use a raw LLM extraction as a clinical approval without validation.
- **Gold**: `order_line_status`, `backorder_evidence`, `eligible_substitution_candidates`, `access_safe_citations`. These are *derived* for live queries, not synthetic authoritative input.
- **Search**: non-sensitive, version-filtered product/guide/IFU chunks with keyword + semantic search, page references, doc status. Contracts/pricing stay in separately governed data paths.

## Why PDF tables need special chunking

Each approval row must keep requested SKU, candidate SKU, approval status, required dimensions, latex property, clinical note, `document_id`, version and page as a single retrievable unit. Generic token chunking could break one row across chunks. For contracts, chunk by clause/amendment and associate hospital_id and effective dates. For IFUs, chunk by section. Use exact SKU filtering alongside semantic retrieval for descriptive queries.

## Security nuance

The `rep_access_DEMO_ONLY.csv` source provides fixtures to test policy logic. It is insufficient proof of production-grade row-level security. The demonstration must also prevent the user from changing their apparent identity through the chat prompt or UI request. Production authentication, UC permissions and audit trails are distinct work items.
