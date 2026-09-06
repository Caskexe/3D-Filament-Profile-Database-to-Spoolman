#!/usr/bin/env python3
"""
Import a 3D Filament Profile Database (spooldb) export -- https://3dfilamentprofiles.com -- (my-spools.json) into a Spoolman instance.

Spoolman has three linked entities and this script builds them in order:
	vendor (brand) -> filament (material + colour) -> spool (a physical roll)

3D Filament Profile Database's own "filament_id" already groups spools that share one material and
colour, so that grouping is reused here rather than recalculating it.

Spoolman requires a density and a diameter on every filament, which spooldb
does not export. Sensible defaults are used below (1.75mm diameter, and a
density looked up by material) and can be edited before running if your own
filament differs.

Usage:
	python import-spooldb-to-spoolman.py --url http://192.168.0.50:7912 --file my-spools.json
	python import-spooldb-to-spoolman.py --url http://192.168.0.50:7912 --file my-spools.json --dry-run

If --url is left out you will be prompted for it. --dry-run prints what would
be created without writing anything to Spoolman, which is worth doing first.

By CASK.exe https://github.com/CASKexe
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

# rough density in g/cm3 by base material, used only when Spoolman needs a value
# and spooldb didn't supply one -- edit these if your own filament is known to differ
DENSITY_BY_MATERIAL = {
	"PLA": 1.24,
	"PLA+/PRO": 1.24,
	"PETG": 1.27,
	"ABS": 1.04,
	"ASA": 1.05,
	"TPU": 1.21,
	"NYLON": 1.14,
	"PC": 1.20,
}
DEFAULT_DENSITY = 1.24
DEFAULT_DIAMETER = 1.75


def api_request(base_url, method, path, body=None):
	# thin wrapper around urllib so the script has no third-party dependencies
	url = base_url.rstrip("/") + path
	data = json.dumps(body).encode("utf-8") if body is not None else None
	req = urllib.request.Request(url, data=data, method=method)
	req.add_header("Content-Type", "application/json")
	try:
		with urllib.request.urlopen(req) as resp:
			raw = resp.read()
			return json.loads(raw) if raw else None
	except urllib.error.HTTPError as e:
		detail = e.read().decode("utf-8", errors="replace")
		raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def find_vendor(base_url, name):
	vendors = api_request(base_url, "GET", "/api/v1/vendor")
	for v in vendors:
		if v["name"].strip().lower() == name.strip().lower():
			return v
	return None


def create_vendor(base_url, name):
	return api_request(base_url, "POST", "/api/v1/vendor", {"name": name})


def find_filament(base_url, vendor_id, material, name):
	filaments = api_request(base_url, "GET", "/api/v1/filament")
	for f in filaments:
		same_vendor = f.get("vendor", {}) and f["vendor"].get("id") == vendor_id
		if same_vendor and f.get("material") == material and f.get("name") == name:
			return f
	return None


def normalise_material(material):
	return (material or "").strip().upper()


def density_for(material):
	return DENSITY_BY_MATERIAL.get(normalise_material(material), DEFAULT_DENSITY)


def build_filament_name(row):
	# spooldb's material_type is closer to a variant name (Basic, Rapid, Matte...)
	# This will just combine it with colour so the Spoolman filament name stays readable
	material_type = (row.get("material_type") or "").strip()
	colour = (row.get("color") or "").strip()
	return f"{material_type} {colour}".strip() or colour or "Filament"


def build_spool_comment(row):
	# fold the spooldb-only fields that don't map cleanly onto Spoolman fields
	# into a comment, rather than dropping them
	bits = []
	if row.get("notes"):
		bits.append(row["notes"])
	props = row.get("spool_properties") or {}
	if props.get("purchase_notes"):
		bits.append(f"Purchased: {props['purchase_notes']}")
	if props.get("purchase_date"):
		bits.append(f"Purchase date: {props['purchase_date']}")
	if row.get("td_value") is not None:
		bits.append(f"TD value: {row['td_value']}")
	bits.append(f"spooldb: {row.get('spool_url')}")
	return " | ".join(bits)


def group_by_filament_id(rows):
	groups = {}
	for row in rows:
		groups.setdefault(row["filament_id"], []).append(row)
	return groups


def main():
	parser = argparse.ArgumentParser(description="Import a spooldb export into Spoolman")
	parser.add_argument("--url", help="Base URL of your Spoolman instance, e.g. http://192.168.0.50:7912")
	parser.add_argument("--file", default="my-spools.json", help="Path to the spooldb export")
	parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing to Spoolman")
	args = parser.parse_args()

	base_url = args.url or input("Spoolman base URL (e.g. http://192.168.0.50:7912): ").strip()

	with open(args.file, "r", encoding="utf-8") as f:
		rows = json.load(f)

	print(f"Loaded {len(rows)} spools from {args.file}")

	# quick connectivity check before doing anything else
	if not args.dry_run:
		try:
			api_request(base_url, "GET", "/api/v1/vendor")
		except Exception as e:
			print(f"Could not reach Spoolman at {base_url}: {e}")
			sys.exit(1)

	vendor_cache = {}  # brand name -> vendor id
	filament_cache = {}  # spooldb filament_id -> spoolman filament id

	created_vendors = created_filaments = created_spools = 0
	failed_spools = 0

	for spooldb_filament_id, group in group_by_filament_id(rows).items():
		sample = group[0]
		brand = sample["brand"].strip()
		material = sample["material"].strip()
		filament_name = build_filament_name(sample)

		# resolve or create the vendor
		if brand not in vendor_cache:
			if args.dry_run:
				print(f"[dry run] would ensure vendor exists: {brand}")
				vendor_cache[brand] = -1
			else:
				vendor = find_vendor(base_url, brand)
				if vendor is None:
					vendor = create_vendor(base_url, brand)
					created_vendors += 1
					print(f"Created vendor: {brand}")
				vendor_cache[brand] = vendor["id"]
		vendor_id = vendor_cache[brand]

		# resolve or create the filament (one per spooldb filament_id)
		if spooldb_filament_id not in filament_cache:
			# use the largest remaining_grams seen in the group as a stand-in for
			# the nominal full-spool weight, since spooldb doesn't export that
			nominal_weight = max((r.get("remaining_grams") or 0) for r in group) or 1000

			colour_hexes = [c.strip() for c in (sample.get("rgb") or "").split(",") if c.strip()]
			filament_body = {
				"name": filament_name,
				"vendor_id": vendor_id,
				"material": material,
				"density": density_for(material),
				"diameter": DEFAULT_DIAMETER,
				"weight": nominal_weight,
			}
			if colour_hexes:
				filament_body["color_hex"] = colour_hexes[0].lstrip("#")
				if len(colour_hexes) > 1:
					filament_body["multi_color_hexes"] = ",".join(c.lstrip("#") for c in colour_hexes)

			if args.dry_run:
				print(f"[dry run] would ensure filament exists: {brand} {filament_name} ({material})")
				filament_cache[spooldb_filament_id] = -1
			else:
				filament = find_filament(base_url, vendor_id, material, filament_name)
				if filament is None:
					filament = api_request(base_url, "POST", "/api/v1/filament", filament_body)
					created_filaments += 1
					print(f"Created filament: {brand} {filament_name} ({material})")
				filament_cache[spooldb_filament_id] = filament["id"]
		filament_id = filament_cache[spooldb_filament_id]

		# create one Spoolman spool per spooldb spool row
		for row in group:
			props = row.get("spool_properties") or {}
			spool_body = {
				"filament_id": filament_id,
				"remaining_weight": row.get("remaining_grams"),
				"location": row.get("location"),
				"lot_nr": row.get("short_code"),
				"comment": build_spool_comment(row),
			}
			if props.get("purchase_price") is not None:
				spool_body["price"] = props["purchase_price"]

			if args.dry_run:
				print(f"[dry run] would create spool {row.get('short_code')} ({row.get('remaining_grams')}g)")
				continue

			try:
				api_request(base_url, "POST", "/api/v1/spool", spool_body)
				created_spools += 1
			except RuntimeError as e:
				failed_spools += 1
				print(f"Failed to create spool {row.get('short_code')}: {e}")

	print()
	print("Done.")
	if not args.dry_run:
		print(f"Vendors created: {created_vendors}")
		print(f"Filaments created: {created_filaments}")
		print(f"Spools created: {created_spools}")
		if failed_spools:
			print(f"Spools failed: {failed_spools} -- see messages above")


if __name__ == "__main__":
	main()
