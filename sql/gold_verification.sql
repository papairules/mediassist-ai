-- Expected: 11 lines, 342 ordered, 192 shipped, 150 unshipped.
SELECT
  count(*) AS line_count,
  sum(quantity_ordered) AS ordered_quantity,
  sum(quantity_shipped) AS shipped_quantity,
  sum(quantity_unshipped) AS unshipped_quantity
FROM workspace.crestline_gold.order_line_fulfillment;

-- Expected: 8 revisions, 6 lines with evidence, 3 current unknown dates.
SELECT
  sum(supplier_eta_record_count) AS supplier_eta_revision_count,
  count_if(current_supplier_eta_record_id IS NOT NULL) AS lines_with_eta_evidence,
  count_if(
    current_supplier_eta_record_id IS NOT NULL
    AND current_supplier_restock_date IS NULL
  ) AS current_unknown_supplier_restock_dates
FROM workspace.crestline_gold.backorder_eta_evidence;

-- Expected: 30 region-product rows, 14 observations, 359 available units.
SELECT
  count(*) AS region_product_count,
  count_if(has_inventory_observation) AS observed_inventory_count,
  sum(quantity_available) AS observed_available_quantity
FROM workspace.crestline_gold.product_region_inventory;
