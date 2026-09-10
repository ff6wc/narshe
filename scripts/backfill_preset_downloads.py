#!/usr/bin/env python3
"""
Backfill Preset Downloads Migration Script

Aggregates historical seed generations from the 'seedlist' collection in Firestore
by 'seed_type' and updates (or initializes) the 'downloads' and 'download_count' fields
in the 'presets' collection.

Usage:
    # Dry run (shows aggregated statistics without modifying Firestore):
    python scripts/backfill_preset_downloads.py

    # Apply changes to Firestore:
    python scripts/backfill_preset_downloads.py --apply
"""

import sys
import os
import argparse
from datetime import datetime, timezone
from collections import defaultdict

# Ensure root directory is on Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from api_utils.collections import SEEDLIST, PRESETS


def get_db():
    return firestore.Client()


def run_backfill(apply_changes: bool = False, batch_size: int = 450):
    db = get_db()
    seedlist_ref = db.collection(SEEDLIST)
    presets_ref = db.collection(PRESETS)

    print(f"[*] Starting preset download backfill (apply={apply_changes})...")
    print("[*] Scanning 'seedlist' collection...")

    counts = defaultdict(int)
    latest_timestamps = {}
    display_names = {}

    total_scanned = 0
    # Stream seedlist documents
    for doc in seedlist_ref.stream():
        total_scanned += 1
        data = doc.to_dict() or {}
        seed_type = data.get('seed_type')
        if not seed_type or not isinstance(seed_type, str):
            continue

        seed_type_clean = seed_type.strip()
        if not seed_type_clean:
            continue

        key = seed_type_clean.lower()
        counts[key] += 1
        if key not in display_names:
            display_names[key] = seed_type_clean

        timestamp = data.get('timestamp')
        if timestamp:
            current_latest = latest_timestamps.get(key)
            if not current_latest or str(timestamp) > str(current_latest):
                latest_timestamps[key] = str(timestamp)

        if total_scanned % 1000 == 0:
            print(f"    ...scanned {total_scanned} seedlist records so far")

    print(f"[+] Scan complete: {total_scanned} total seeds scanned across {len(counts)} distinct preset types.")

    if not counts:
        print("[!] No seedlist entries found. Exiting.")
        return

    # Print summary table
    print("\n" + "=" * 60)
    print(f"{'Preset Name':<35} | {'Downloads':<10} | {'Latest Download'}")
    print("-" * 60)
    sorted_presets = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    for key, count in sorted_presets:
        latest = latest_timestamps.get(key, "N/A")
        name = display_names.get(key, key)
        print(f"{name:<35} | {count:<10} | {latest}")
    print("=" * 60 + "\n")

    if not apply_changes:
        print("[*] DRY RUN finished. Run with '--apply' to persist counts into the 'presets' collection.")
        return

    print("[*] Applying aggregated counts to 'presets' collection in Firestore...")

    # Load existing presets into lookup map
    existing_preset_docs = {}
    for pdoc in presets_ref.stream():
        pdata = pdoc.to_dict() or {}
        pname = pdata.get('preset_name_lower') or pdata.get('name') or pdata.get('preset_name') or ''
        if pname:
            existing_preset_docs[str(pname).strip().lower()] = (pdoc.reference, pdata)

    batch = db.batch()
    operations_in_batch = 0
    total_updated = 0
    total_created = 0

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    for key, count in sorted_presets:
        name = display_names.get(key, key)
        latest_ts = latest_timestamps.get(key, now_iso)

        if key in existing_preset_docs:
            doc_ref, pdata = existing_preset_docs[key]
            # Use max of existing downloads and backfilled seedlist count
            existing_dl = pdata.get('downloads') or pdata.get('download_count') or 0
            final_count = max(existing_dl, count)
            existing_ts = pdata.get('download_timestamp') or ''
            final_ts = max(existing_ts, latest_ts) if existing_ts else latest_ts

            batch.set(doc_ref, {
                'downloads': final_count,
                'download_count': final_count,
                'download_timestamp': final_ts
            }, merge=True)
            total_updated += 1
        else:
            doc_ref = presets_ref.document()
            new_preset = {
                'id': doc_ref.id,
                'name': name,
                'preset_name': name,
                'preset_name_lower': key,
                'description': '',
                'flags': '',
                'creator_id': 'community',
                'creator_name': 'Community',
                'tags': [],
                'created_at': created_at,
                'download_timestamp': latest_ts,
                'downloads': count,
                'download_count': count,
            }
            batch.set(doc_ref, new_preset)
            total_created += 1

        operations_in_batch += 1
        if operations_in_batch >= batch_size:
            batch.commit()
            print(f"    ...committed batch of {operations_in_batch} preset records")
            batch = db.batch()
            operations_in_batch = 0

    if operations_in_batch > 0:
        batch.commit()
        print(f"    ...committed final batch of {operations_in_batch} preset records")

    print(f"[+] Successfully backfilled preset downloads! Updated: {total_updated}, Created: {total_created}.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Backfill preset download counts from seedlist collection.")
    parser.add_argument('--apply', action='store_true', help="Persist changes to Firestore (defaults to dry-run)")
    parser.add_argument('--batch-size', type=int, default=450, help="Firestore write batch size (max 500)")
    args = parser.parse_args()

    run_backfill(apply_changes=args.apply, batch_size=args.batch_size)
