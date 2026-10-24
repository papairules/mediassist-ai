CREATE OR REPLACE VIEW {{catalog}}.{{gold_schema}}.`order_line_fulfillment` AS
WITH shipment_rollup AS (
  SELECT
    s.order_id,
    s.line_id,
    count(*) AS shipment_count,
    sum(s.quantity_shipped) AS quantity_shipped,
    sum(
      CASE
        WHEN lower(s.carrier_status) = 'delivered'
          AND s.delivered_at IS NOT NULL
        THEN s.quantity_shipped
        ELSE CAST(0 AS BIGINT)
      END
    ) AS quantity_carrier_reported_delivered,
    max(s.last_scan_at) AS latest_carrier_scan_at,
    sort_array(
      collect_list(
        named_struct(
          'shipment_id', s.shipment_id,
          'quantity_shipped', s.quantity_shipped,
          'carrier', s.carrier,
          'tracking_id', s.tracking_id,
          'carrier_status', s.carrier_status,
          'last_scan_at', s.last_scan_at,
          'carrier_estimated_arrival', s.carrier_estimated_arrival,
          'delivered_at', s.delivered_at,
          'record_id', s._record_id,
          'file_id', s._file_id,
          'source_file_name', s._source_file_name,
          'source_file_modified_at', s._source_file_modified_at,
          'bronze_ingested_at', s._ingested_at
        )
      ),
      true
    ) AS shipment_evidence
  FROM {{catalog}}.{{silver_schema}}.`shipments` AS s
  GROUP BY s.order_id, s.line_id
),
line_facts AS (
  SELECT
    h.hospital_id,
    h.hospital_name,
    h.region,
    h.contract_id,
    o.order_id,
    ol.line_id,
    o.placed_at,
    o.erp_loaded_at,
    ol.sku,
    p.brand,
    p.category,
    p.gauge,
    p.color,
    p.length_mm,
    p.latex_free,
    p.sterile,
    p.manufacturer_status,
    ol.uom,
    ol.quantity_ordered,
    coalesce(sr.quantity_shipped, CAST(0 AS BIGINT)) AS quantity_shipped,
    coalesce(
      sr.quantity_carrier_reported_delivered,
      CAST(0 AS BIGINT)
    ) AS quantity_carrier_reported_delivered,
    ol.quantity_ordered
      - coalesce(sr.quantity_shipped, CAST(0 AS BIGINT)) AS quantity_unshipped,
    coalesce(sr.shipment_count, CAST(0 AS BIGINT)) AS shipment_count,
    sr.latest_carrier_scan_at,
    sr.shipment_evidence,
    h._source_id AS hospital_source_id,
    h._record_id AS hospital_record_id,
    h._source_file_name AS hospital_source_file_name,
    h._ingested_at AS hospital_ingested_at,
    o._source_id AS order_source_id,
    o._record_id AS order_record_id,
    o._source_file_name AS order_source_file_name,
    o._ingested_at AS order_ingested_at,
    ol._source_id AS order_line_source_id,
    ol._record_id AS order_line_record_id,
    ol._source_file_name AS order_line_source_file_name,
    ol._ingested_at AS order_line_ingested_at,
    p._source_id AS product_source_id,
    p._record_id AS product_record_id,
    p._source_file_name AS product_source_file_name,
    p._ingested_at AS product_ingested_at
  FROM {{catalog}}.{{silver_schema}}.`order_lines` AS ol
  JOIN {{catalog}}.{{silver_schema}}.`orders` AS o
    ON ol.order_id = o.order_id
  JOIN {{catalog}}.{{silver_schema}}.`hospitals` AS h
    ON o.hospital_id = h.hospital_id
  JOIN {{catalog}}.{{silver_schema}}.`products` AS p
    ON ol.sku = p.sku
  LEFT JOIN shipment_rollup AS sr
    ON ol.order_id = sr.order_id
    AND ol.line_id = sr.line_id
)
SELECT
  line_facts.*,
  CASE
    WHEN quantity_shipped > quantity_ordered THEN 'over_shipped_data_issue'
    WHEN quantity_shipped = 0 THEN 'not_shipped'
    WHEN quantity_shipped < quantity_ordered THEN 'partially_shipped'
    WHEN quantity_carrier_reported_delivered >= quantity_ordered THEN 'fully_delivered'
    ELSE 'fully_shipped'
  END AS line_fulfillment_state
FROM line_facts
;

CREATE OR REPLACE VIEW {{catalog}}.{{gold_schema}}.`backorder_eta_evidence` AS
WITH ranked_eta AS (
  SELECT
    e.*,
    row_number() OVER (
      PARTITION BY e.order_id, e.line_id
      ORDER BY
        e.supplier_reported_at DESC,
        e.ingested_at DESC,
        e._ingested_at DESC,
        e.source_record_id DESC
    ) AS current_record_rank
  FROM {{catalog}}.{{silver_schema}}.`supplier_etas` AS e
),
current_eta AS (
  SELECT *
  FROM ranked_eta
  WHERE current_record_rank = 1
),
eta_history AS (
  SELECT
    e.order_id,
    e.line_id,
    count(*) AS supplier_eta_record_count,
    sort_array(
      collect_list(
        named_struct(
          'supplier_reported_at', e.supplier_reported_at,
          'source_ingested_at', e.ingested_at,
          'source_record_id', e.source_record_id,
          'expected_supplier_restock_date', e.expected_supplier_restock_date,
          'date_classification', e.date_classification,
          'source_note', e.source_note,
          'record_id', e._record_id,
          'file_id', e._file_id,
          'source_file_name', e._source_file_name,
          'source_file_modified_at', e._source_file_modified_at,
          'bronze_ingested_at', e._ingested_at
        )
      ),
      false
    ) AS supplier_eta_revision_history
  FROM {{catalog}}.{{silver_schema}}.`supplier_etas` AS e
  GROUP BY e.order_id, e.line_id
)
SELECT
  f.hospital_id,
  f.hospital_name,
  f.region,
  f.order_id,
  f.line_id,
  f.sku,
  f.uom,
  f.quantity_ordered,
  f.quantity_shipped,
  f.quantity_carrier_reported_delivered,
  f.quantity_unshipped,
  f.quantity_unshipped > 0 AS has_unshipped_quantity,
  f.line_fulfillment_state,
  f.shipment_count,
  f.latest_carrier_scan_at,
  f.shipment_evidence,
  coalesce(h.supplier_eta_record_count, CAST(0 AS BIGINT))
    AS supplier_eta_record_count,
  c.source_record_id AS current_supplier_eta_record_id,
  c.expected_supplier_restock_date AS current_supplier_restock_date,
  c.supplier_reported_at AS current_supplier_reported_at,
  c.ingested_at AS current_supplier_eta_ingested_at,
  c.date_classification AS current_date_classification,
  c.source_note AS current_source_note,
  c._source_id AS current_supplier_eta_source_id,
  c._record_id AS current_supplier_eta_bronze_record_id,
  c._file_id AS current_supplier_eta_file_id,
  c._source_file_name AS current_supplier_eta_source_file_name,
  c._source_file_modified_at AS current_supplier_eta_source_file_modified_at,
  c._ingested_at AS current_supplier_eta_bronze_ingested_at,
  h.supplier_eta_revision_history,
  f.order_record_id,
  f.order_line_record_id,
  f.product_record_id
FROM {{catalog}}.{{gold_schema}}.`order_line_fulfillment` AS f
LEFT JOIN current_eta AS c
  ON f.order_id = c.order_id
  AND f.line_id = c.line_id
LEFT JOIN eta_history AS h
  ON f.order_id = h.order_id
  AND f.line_id = h.line_id
;

CREATE OR REPLACE VIEW {{catalog}}.{{gold_schema}}.`product_region_inventory` AS
WITH ranked_inventory AS (
  SELECT
    i.*,
    row_number() OVER (
      PARTITION BY i.warehouse_id, i.sku
      ORDER BY
        i.snapshot_at DESC,
        i._ingested_at DESC,
        i._record_id DESC
    ) AS current_snapshot_rank
  FROM {{catalog}}.{{silver_schema}}.`inventory_snapshots` AS i
),
current_inventory AS (
  SELECT *
  FROM ranked_inventory
  WHERE current_snapshot_rank = 1
),
inventory_rollup AS (
  SELECT
    i.region,
    i.sku,
    count(*) AS warehouse_count,
    sum(i.quantity_on_hand) AS quantity_on_hand,
    sum(i.quantity_reserved) AS quantity_reserved,
    sum(i.quantity_on_hand - i.quantity_reserved) AS quantity_available,
    min(i.snapshot_at) AS earliest_current_snapshot_at,
    max(i.snapshot_at) AS latest_current_snapshot_at,
    sort_array(
      collect_list(
        named_struct(
          'warehouse_id', i.warehouse_id,
          'snapshot_at', i.snapshot_at,
          'quantity_on_hand', i.quantity_on_hand,
          'quantity_reserved', i.quantity_reserved,
          'quantity_available', i.quantity_on_hand - i.quantity_reserved,
          'record_id', i._record_id,
          'file_id', i._file_id,
          'source_file_name', i._source_file_name,
          'source_file_modified_at', i._source_file_modified_at,
          'bronze_ingested_at', i._ingested_at
        )
      ),
      true
    ) AS warehouse_inventory_evidence
  FROM current_inventory AS i
  GROUP BY i.region, i.sku
),
regions AS (
  SELECT DISTINCT h.region
  FROM {{catalog}}.{{silver_schema}}.`hospitals` AS h
),
product_region_grid AS (
  SELECT
    r.region,
    p.*
  FROM regions AS r
  CROSS JOIN {{catalog}}.{{silver_schema}}.`products` AS p
)
SELECT
  g.region,
  g.sku,
  g.brand,
  g.category,
  g.gauge,
  g.color,
  g.length_mm,
  g.latex_free,
  g.sterile,
  g.manufacturer_status,
  i.sku IS NOT NULL AS has_inventory_observation,
  i.warehouse_count,
  i.quantity_on_hand,
  i.quantity_reserved,
  i.quantity_available,
  i.earliest_current_snapshot_at,
  i.latest_current_snapshot_at,
  i.warehouse_inventory_evidence,
  g._source_id AS product_source_id,
  g._record_id AS product_record_id,
  g._file_id AS product_file_id,
  g._source_file_name AS product_source_file_name,
  g._source_file_modified_at AS product_source_file_modified_at,
  g._ingested_at AS product_ingested_at
FROM product_region_grid AS g
LEFT JOIN inventory_rollup AS i
  ON g.region = i.region
  AND g.sku = i.sku
;
