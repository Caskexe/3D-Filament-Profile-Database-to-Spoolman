#!/usr/bin/env python3
"""
Version 1.1 23 Sep 2026

Import a 3D Filament Profile Database (spooldb) export -- https://3dfilamentprofiles.com -- (my-spools.json) into a Spoolman instance.

Spoolman has three linked entities and this script builds them in order:
	vendor (brand) -> filament (material + colour) -> spool (a physical roll)

3D Filament Profile Database's own "filament_id" already groups spools that share one material and
colour, so that grouping is reused here rather than recalculating it.

Spoolman requires a density and a diameter on every filament, which spooldb
does not export. Sensible defaults are used below (1.75mm diameter, and a
density looked up by material) and can be edited before running if your own
filament differs.

Duplicate protection:
	vendors   matched by name, ignoring case and extra spaces
	filaments matched by vendor, material and name, ignoring case and extra spaces
	spools    matched by lot number (spooldb short code) or by the spooldb URL
	          stored in the spool comment
	export    repeated rows in the spooldb file itself are only imported once
The script can be rerun as often as needed without creating duplicates.

Usage: (Replace URL with your Spoolman installation)
	python import-spooldb-to-spoolman.py --url http://192.168.0.__:7912 --file my-spools.json
	python import-spooldb-to-spoolman.py --url http://192.168.0.__:7912 --file my-spools.json --dry-run

If --url is left out you will be prompted for it. --dry-run prints what would
be created without writing anything to Spoolman, which is worth doing first.

By CASK.exe https://github.com/CASKexe
"""

import argparse
import json
import re
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
	"PA": 1.14,
	"PA (NYLON)": 1.14,
	"PC": 1.20,
}
DEFAULT_DENSITY = 1.24
DEFAULT_DIAMETER = 1.75

# direction used for multi-colour filament: "coaxial" suits silk dual/tri-colour,
# "longitudinal" suits gradient or rainbow spools that change colour along their length
MULTI_COLOUR_DIRECTION = "coaxial"

# pulls the spooldb URL back out of a spool comment written by this script
SPOOLDB_URL_PATTERN = re.compile(r"spooldb:\s*(\S+)")


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


def norm(text):
	# case-insensitive, whitespace-collapsed key used for all duplicate matching
	return " ".join((text or "").split()).lower()


def filament_key(vendor_id, material, name):
	return (vendor_id, norm(material), norm(name))


def spool_url_from_comment(comment):
	match = SPOOLDB_URL_PATTERN.search(comment or "")
	return match.group(1) if match else None


def load_existing(base_url):
	# read everything already in Spoolman once, rather than querying per item
	vendors = api_request(base_url, "GET", "/api/v1/vendor") or []
	filaments = api_request(base_url, "GET", "/api/v1/filament") or []
	spools = api_request(base_url, "GET", "/api/v1/spool?allow_archived=true") or []

	vendor_ids = {norm(v["name"]): v["id"] for v in vendors}

	filament_ids = {}
	for f in filaments:
		vendor = f.get("vendor") or {}
		filament_ids[filament_key(vendor.get("id"), f.get("material"), f.get("name"))] = f["id"]

	# a spool counts as existing if either its lot number or its spooldb URL is known
	lot_nrs = set()
	spool_urls = set()
	for s in spools:
		if s.get("lot_nr"):
			lot_nrs.add(s["lot_nr"])
		url = spool_url_from_comment(s.get("comment"))
		if url:
			spool_urls.add(url)

	return vendor_ids, filament_ids, lot_nrs, spool_urls


def normalise_material(material):
	return (material or "").strip().upper()


def density_for(material):
	return DENSITY_BY_MATERIAL.get(normalise_material(material), DEFAULT_DENSITY)


def build_filament_name(row):
	# spooldb's material_type is closer to a variant name (Basic, Rapid, Matte...)
	# so combine it with colour to keep the Spoolman filament name readable
	material_type = (row.get("material_type") or "").strip()
	colour = (row.get("color") or "").strip()
	return f"{material_type} {colour}".strip() or colour or "Filament"


def apply_colours(filament_body, rgb):
	# Spoolman accepts either a single colour or a multi-colour set, never both
	colour_hexes = [c.strip().lstrip("#") for c in (rgb or "").split(",") if c.strip()]
	if len(colour_hexes) == 1:
		filament_body["color_hex"] = colour_hexes[0]
	elif len(colour_hexes) > 1:
		filament_body["multi_color_hexes"] = ",".join(colour_hexes)
		filament_body["multi_color_direction"] = MULTI_COLOUR_DIRECTION


def build_spool_comment(row):
	# fold the spooldb-only fields that don't map cleanly onto Spoolman fields
	# into a comment, rather than dropping them; the spooldb URL goes last and
	# doubles as the duplicate check on later runs
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


def dedupe_rows(rows):
	# drop repeated rows within the export itself, keyed on URL then short code
	seen = set()
	unique = []
	for row in rows:
		key = row.get("spool_url") or row.get("short_code")
		if key and key in seen:
			continue
		if key:
			seen.add(key)
		unique.append(row)
	return unique


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
		all_rows = json.load(f)

	rows = dedupe_rows(all_rows)
	print(f"Loaded {len(all_rows)} spools from {args.file}")
	if len(rows) != len(all_rows):
		print(f"Ignored {len(all_rows) - len(rows)} repeated rows in the export")

	# load current Spoolman contents; a dry run also reads them when reachable,
	# so its output reflects what a real run would actually create
	try:
		vendor_ids, filament_ids, lot_nrs, spool_urls = load_existing(base_url)
	except Exception as e:
		if not args.dry_run:
			print(f"Could not reach Spoolman at {base_url}: {e}")
			sys.exit(1)
		print(f"Could not reach Spoolman ({e}); dry run will assume it is empty")
		vendor_ids, filament_ids, lot_nrs, spool_urls = {}, {}, set(), set()

	filament_cache = {}  # spooldb filament_id -> spoolman filament id
	next_fake_id = -1  # placeholder ids handed out during a dry run

	created_vendors = created_filaments = created_spools = 0
	skipped_spools = failed_spools = failed_filaments = 0

	for spooldb_filament_id, group in group_by_filament_id(rows).items():
		sample = group[0]
		brand = sample["brand"].strip()
		material = sample["material"].strip()
		filament_name = build_filament_name(sample)

		try:
			# resolve or create the vendor
			vendor_id = vendor_ids.get(norm(brand))
			if vendor_id is None:
				if args.dry_run:
					print(f"[dry run] would create vendor: {brand}")
					vendor_id = next_fake_id
					next_fake_id -= 1
				else:
					vendor_id = api_request(base_url, "POST", "/api/v1/vendor", {"name": brand})["id"]
					print(f"Created vendor: {brand}")
				vendor_ids[norm(brand)] = vendor_id
				created_vendors += 1

			# resolve or create the filament (one per spooldb filament_id)
			if spooldb_filament_id not in filament_cache:
				key = filament_key(vendor_id, material, filament_name)
				filament_id = filament_ids.get(key)
				if filament_id is None:
					# use the largest remaining_grams seen in the group as a stand-in for
					# the nominal full-spool weight, since spooldb doesn't export that
					nominal_weight = max((r.get("remaining_grams") or 0) for r in group) or 1000

					filament_body = {
						"name": filament_name,
						"vendor_id": vendor_id,
						"material": material,
						"density": density_for(material),
						"diameter": DEFAULT_DIAMETER,
						"weight": nominal_weight,
					}
					apply_colours(filament_body, sample.get("rgb"))

					if args.dry_run:
						print(f"[dry run] would create filament: {brand} {filament_name} ({material})")
						filament_id = next_fake_id
						next_fake_id -= 1
					else:
						filament_id = api_request(base_url, "POST", "/api/v1/filament", filament_body)["id"]
						print(f"Created filament: {brand} {filament_name} ({material})")
					filament_ids[key] = filament_id
					created_filaments += 1
				filament_cache[spooldb_filament_id] = filament_id
			filament_id = filament_cache[spooldb_filament_id]

		except RuntimeError as e:
			# a bad vendor or filament shouldn't stop the whole import; skip its spools
			failed_filaments += 1
			failed_spools += len(group)
			print(f"Failed to create {brand} {filament_name}: {e}")
			continue

		# create one Spoolman spool per spooldb spool row
		for row in group:
			short_code = row.get("short_code")
			spool_url = row.get("spool_url")

			# skip spools already in Spoolman
			if (short_code and short_code in lot_nrs) or (spool_url and spool_url in spool_urls):
				skipped_spools += 1
				print(f"Skipped existing spool {short_code}")
				continue

			props = row.get("spool_properties") or {}
			spool_body = {
				"filament_id": filament_id,
				"remaining_weight": row.get("remaining_grams"),
				"location": row.get("location"),
				"lot_nr": short_code,
				"comment": build_spool_comment(row),
			}
			if props.get("purchase_price") is not None:
				spool_body["price"] = props["purchase_price"]

			if args.dry_run:
				print(f"[dry run] would create spool {short_code} ({row.get('remaining_grams')}g)")
			else:
				try:
					api_request(base_url, "POST", "/api/v1/spool", spool_body)
				except RuntimeError as e:
					failed_spools += 1
					print(f"Failed to create spool {short_code}: {e}")
					continue

			# record it so a repeat within this run is also caught
			if short_code:
				lot_nrs.add(short_code)
			if spool_url:
				spool_urls.add(spool_url)
			created_spools += 1

	# summary
	prefix = "[dry run] would create" if args.dry_run else "Created"
	print()
	print("Done.")
	print(f"{prefix} vendors: {created_vendors}")
	print(f"{prefix} filaments: {created_filaments}")
	print(f"{prefix} spools: {created_spools}")
	print(f"Spools skipped (already in Spoolman): {skipped_spools}")
	if failed_filaments:
		print(f"Filaments failed: {failed_filaments} -- see messages above")
	if failed_spools:
		print(f"Spools failed: {failed_spools} -- see messages above")


if __name__ == "__main__":
	main()
