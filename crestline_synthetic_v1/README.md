# Crestline Medical Distribution - synthetic source package v1

**All data is fictional**. Nothing is a real medical recommendation, hospital contract, clinical equivalence assertion, customer record, or supplier commitment.

## Why this package exists

Build a Databricks-native order support copilot grounded in the FDE assignment. Sources intentionally reflect the mix the assignment describes: ERP snapshots, unreliable supplier ETA CSVs, table-centric substitution PDFs, scanned pages, hospital contracts/amendments, IFUs, and low-quality historic support tickets.

**Fixed evaluation clock:** `2026-10-22T23:00:00Z`. Use this to evaluate freshness tests reproducibly.

## Source files and their intended authority

- `source_csv/orders.csv`, `order_lines.csv`, `shipments.csv`, `inventory.csv`: represent current ERP exports; import to Delta Bronze then harmonize to Silver. Order line shipped quantities should be *derived* from shipments and ordered amounts, not generated using an LLM. All ERP snapshots are timestamped.
- `source_csv/supplier_etas.csv`: nightly supplier data; values are **unverified restock estimates**, NOT guaranteed hospital delivery dates. Newer records supersede older records for the same order line; missing/stale records are valid test conditions.
- `source_csv/products.csv`: canonical item attributes for filtering; should be cross-checked with current IFUs.
- `source_csv/hospitals.csv`: synthetic hospital master used for tenancy and routing.
- `source_pdfs/SUB_GUIDE_*.pdf`: extract table rows with page references, including a deliberately obsolete guide.
- `source_pdfs/CONTRACT_*.pdf`, `AMENDMENT_*.pdf`: hospital-specific confidential terms. Parse to secure structured facts; **never put pricing in the globally searchable index**.
- `source_pdfs/IFU_*.pdf`: product specifications; not proof of clinical equivalence.
- `source_pdfs/SUB_SUPPLEMENT_SCANNED_2026.pdf`: intentionally an image-only PDF for an OCR fallback test.
- `source_tickets/salesforce_resolved_tickets.jsonl`: lower-trust historic replies, including incorrect statements. Can help vocabulary/intent classification, never the source of truth for ETA or clinical approval.
- `source_csv/rep_access_DEMO_ONLY.csv`: mock principal-to-hospital grants for negative tests. This is **not real identity**. A production app must derive principal claims from authenticated identity and enforce UC row/column policies / on-behalf-of auth as supported.
- `evaluation/golden_scenarios.json`: independent answer expectations and safety tests. **Do not embed or ingest this into retrieval**.

## Important data contracts

1. **Order status:** `quantity_unshipped = quantity_ordered - SUM(quantity_shipped)`. Ensure shipment records are not duplicated. Delivered units are a subset of shipped units based on verified carrier status.
2. **Date types:** differentiate carrier-estimated arrival of already-dispatched units from supplier restock estimates for unshipped units. Neither is an unconditional hospital delivery guarantee.
3. **Substitutions:** current effective approved pair + matching required attributes + hospital contract (including effective amendment) + available stock + human clinical review. An APPROVED guide row only means eligible for consideration.
4. **Sensitive contracts:** filter by authorized hospital *before* assembling model context, searching, or returning data. In the prototype show deny tests for rep_west viewing H-E101.
5. **Source precedence:** ERP shipment events > historical tickets for factual status; current substitution guide > superseded guide; effective signed amendments override the exact contract clauses they amend; no LLM-authored sources.

## Dataset limitations

Small synthetic corpus, not 8,000 IFUs or 400 real contracts. No live Salesforce API, carrier API, true IAM identities, clinical validation, or empirically calibrated ETA confidence. Cost and production security must be treated as design work until proven in an authorized deployment.

## How to use

1. Study `docs/DATA_DICTIONARY.md` and `docs/SCENARIO_MATRIX.md` first.
2. Load ERP-source CSVs into Databricks Bronze Delta, preserving load time and identifiers.
3. Parse PDFs into document and extraction layers, retaining version, page and provenance.
4. Build Silver order states and approved-candidate facts from structured tables + documents.
5. Only then implement governed retrieval and LangGraph routing.

To regenerate: install `reportlab` and `pillow`; run `python generate_dataset.py` in the package root. Regeneration is deterministic at the content level, though PDF binary metadata may vary.
