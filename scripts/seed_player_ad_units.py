#!/usr/bin/env python3
"""
seed_player_ad_units.py — create `player` ad units as siblings of existing `pre` ad units.

For each provided `pre` ad unit ID, the script:
  1. Fetches its parentId + sizes via GET /gam/ad-units/batch
  2. Creates a sibling ad unit (same parentId, same sizes) via POST /gam/ad-units

Intended use: under each platform (mob, ctv, web, tab) and content type
(vod, tv, live), there is an existing `pre` ad unit. Pass all of those pre IDs
in one go and the script creates the corresponding `player` units.

Usage:
  PROXY_URL=https://your-proxy.example.com PROXY_SECRET=xxx \
    python scripts/seed_player_ad_units.py \
      --pre-ids 1111,2222,3333,4444 \
      [--name player] [--code player] [--apply]

Default is dry-run; pass --apply to actually create units.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def http_request(url, headers, data=None, method="GET"):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read() or b"{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read() or b"{}"
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body.decode("utf-8", errors="replace")}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pre-ids", required=True, help="Comma-separated GAM IDs of existing pre ad units")
    parser.add_argument("--name", default="player", help="Name for the new ad units (default: player)")
    parser.add_argument("--code", default="player", help="adUnitCode for the new ad units (default: player)")
    parser.add_argument("--apply", action="store_true", help="Actually create units (default: dry-run)")
    args = parser.parse_args()

    base_url = os.environ.get("PROXY_URL", "http://localhost:5000").rstrip("/")
    secret = os.environ.get("PROXY_SECRET", "")
    base_headers = {"X-Proxy-Secret": secret} if secret else {}

    pre_ids = [s.strip() for s in args.pre_ids.split(",") if s.strip()]
    if not pre_ids:
        sys.exit("No pre IDs provided")

    ids_param = urllib.parse.quote(",".join(pre_ids))
    status, resp = http_request(
        f"{base_url}/gam/ad-units/batch?ids={ids_param}",
        base_headers,
    )
    if status != 200:
        sys.exit(f"Batch fetch failed (HTTP {status}): {resp}")

    units = {u["id"]: u for u in resp.get("adUnits", [])}
    missing = [pid for pid in pre_ids if pid not in units]
    if missing:
        sys.exit(f"Pre ad units not found in GAM: {missing}")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {base_url} — creating {len(pre_ids)} `{args.code}` ad units as siblings of `pre` units")
    print()

    post_headers = {**base_headers, "Content-Type": "application/json"}
    created = []
    failed = []

    for pre_id in pre_ids:
        unit = units[pre_id]
        parent_id = unit.get("parentId")
        if not parent_id:
            print(f"  pre={pre_id}: SKIP — no parentId in response")
            failed.append((pre_id, "no parentId"))
            continue

        body = {
            "name": args.name,
            "parentId": int(parent_id),
            "adUnitCode": args.code,
            "adUnitSizes": [
                {
                    "size": {"width": s["width"], "height": s["height"]},
                    "environmentType": s.get("environmentType", "BROWSER"),
                }
                for s in unit.get("sizes", [])
            ],
        }

        size_summary = ",".join(f"{s['width']}x{s['height']}" for s in unit.get("sizes", [])) or "(none)"
        print(f"  pre={pre_id} parent={parent_id} sizes={size_summary}")

        if not args.apply:
            continue

        status, resp = http_request(
            f"{base_url}/gam/ad-units",
            post_headers,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
        )
        if status == 201:
            new_id = resp.get("id")
            print(f"    created id={new_id}")
            created.append((pre_id, new_id))
        elif status == 409:
            print(f"    already exists ({resp.get('message')})")
            failed.append((pre_id, "already exists"))
        else:
            print(f"    failed (HTTP {status}): {resp.get('message') or resp}")
            failed.append((pre_id, f"HTTP {status}"))

    print()
    print(f"Summary: created={len(created)} failed={len(failed)}")
    if failed and args.apply:
        sys.exit(1)


if __name__ == "__main__":
    main()
