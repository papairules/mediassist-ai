#!/usr/bin/env python3
"""Offline checks of the synthetic source relationships; run before Databricks ingestion."""
import csv, json
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def records(name):
 with (ROOT/'source_csv'/name).open(newline='',encoding='utf-8') as f: return list(csv.DictReader(f))

hospitals={r['hospital_id']:r for r in records('hospitals.csv')}
products={r['sku']:r for r in records('products.csv')}
orders={r['order_id']:r for r in records('orders.csv')}
lines={(r['order_id'],r['line_id']):r for r in records('order_lines.csv')}
shipments=records('shipments.csv')
eta=records('supplier_etas.csv')
assert len(hospitals)==5
assert all(r['hospital_id'] in hospitals for r in orders.values())
assert len(orders)==10 and len(lines)==11
assert all(k[0] in orders and v['sku'] in products for k,v in lines.items())
shipped=defaultdict(int)
for s in shipments:
 key=(s['order_id'],s['line_id'])
 assert key in lines, f'Shipment line missing: {key}'
 shipped[key]+=int(s['quantity_shipped'])
for k,line in lines.items():
 assert 0 <= shipped[k] <= int(line['quantity_ordered']), k
assert shipped['ORD-1001','1']==60
assert int(lines['ORD-1001','1']['quantity_ordered'])-shipped['ORD-1001','1']==40
assert shipped['ORD-1010','1']==30 and shipped['ORD-1010','2']==0
for e in eta: assert (e['order_id'],e['line_id']) in lines
assert len([e for e in eta if e['order_id']=='ORD-1002'])==2
assert any(not e['expected_supplier_restock_date'] for e in eta)
inv=records('inventory.csv')
assert next(int(i['quantity_on_hand'])-int(i['quantity_reserved']) for i in inv if i['region']=='East' and i['sku']=='BR-IV22-BLU-LF')==45
manifest=records('document_manifest.csv')
assert len(manifest)==13
assert all((ROOT/r['relative_path']).exists() for r in manifest)
assert any(r['status']=='superseded' for r in manifest)
assert any(r['sensitivity']=='restricted_pricing' for r in manifest)
scenario=json.loads((ROOT/'evaluation/golden_scenarios.json').read_text())
assert len(scenario['scenarios'])==13
print(f"PASS: {len(orders)} orders, {len(lines)} lines, {len(shipments)} shipment records, {len(eta)} supplier ETA records, {len(manifest)} PDFs, {len(scenario['scenarios'])} golden scenarios")
