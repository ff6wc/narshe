#!/usr/bin/env python3
"""
Backfill Preset Downloads Migration Script

Aggregates historical seed generations from the 'seedlist' collection in Firestore
by 'seed_type' and updates existing preset records in the 'presets' collection.

Note:
    - Only Discord-bot seed rolls carry the 'preset_' prefix (e.g. 'preset_ultros_league').
      Seeds generated from the web app historically stored seed_type = 'ff6wc', so
      web app preset usage is not recoverable from seedlist.
    - This script only updates existing presets in Firestore. It never creates new documents.

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
from api_utils.collections import SEEDLIST, PRESETS

PRESET_PREFIX = 'preset_'


def get_db():
    return firestore.Client()


def seed_type_to_preset_key(seed_type: str):
    """
    The Discord bot writes seed_type as f"preset_{preset_name.replace(' ', '_')}".
    Everything else ('ff6wc', 'ruin', 'ruin_hard', ...) is a seed category, not a preset.
    Returns the lowercased preset name, or None if this seed_type is not a preset roll.
    """
    cleaned = (seed_type or '').strip()
    if not cleaned.lower().startswith(PRESET_PREFIX):
        return None
    raw_name = cleaned[len(PRESET_PREFIX):].replace('_', ' ').strip()
    return raw_name.lower() or None


def parse_ts(value):
    """Best-effort parse of the mixed timestamp formats found in Firestore."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace('Z', '+00:00').replace(' ', 'T', 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    skipped_non_preset = 0

    # Stream seedlist documents
    for doc in seedlist_ref.stream():
        total_scanned += 1
        data = doc.to_dict() or {}
        seed_type = data.get('seed_type')
        key = seed_type_to_preset_key(seed_type)
        if not key:
            skipped_non_preset += 1
            continue

        counts[key] += 1
        if key not in display_names:
            display_names[key] = key

        parsed_ts = parse_ts(data.get('timestamp'))
        if parsed_ts:
            current_latest = latest_timestamps.get(key)
            if not current_latest or parsed_ts > current_latest:
                latest_timestamps[key] = parsed_ts

        if total_scanned % 1000 == 0:
            print(f"    ...scanned {total_scanned} seedlist records ({skipped_non_preset} non-preset skipped)")

    print(f"[+] Scan complete: {total_scanned} total seeds scanned.")
    print(f"    - Preset rolls matched: {sum(counts.values())} across {len(counts)} distinct presets.")
    print(f"    - Non-preset rolls skipped: {skipped_non_preset}")

    if not counts:
        print("[!] No preset seedlist entries found. Exiting.")
        return

    # Print summary table
    print("\n" + "=" * 65)
    print(f"{'Preset Name':<35} | {'Downloads':<10} | {'Latest Download'}")
    print("-" * 65)
    sorted_presets = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    for key, count in sorted_presets:
        latest_dt = latest_timestamps.get(key)
        latest_str = to_iso(latest_dt) if latest_dt else "N/A"
        name = display_names.get(key, key)
        print(f"{name:<35} | {count:<10} | {latest_str}")
    print("=" * 65 + "\n")

    # Load existing presets into lookup map and detect collisions
    print("[*] Loading existing presets from Firestore...")
    existing_preset_docs = {}
    ambiguous_keys = set()

    for pdoc in presets_ref.stream():
        pdata = pdoc.to_dict() or {}
        pname = pdata.get('preset_name_lower') or pdata.get('name') or pdata.get('preset_name') or ''
        if pname:
            pkey = str(pname).strip().lower()
            if pkey in existing_preset_docs:
                ambiguous_keys.add(pkey)
            else:
                existing_preset_docs[pkey] = (pdoc.reference, pdata)

    print(f"[+] Loaded {len(existing_preset_docs)} existing presets ({len(ambiguous_keys)} ambiguous names detected).")

    # Classify presets into matched, unmatched, and ambiguous
    matched_presets = []
    unmatched = []
    skipped_ambiguous = []

    for key, count in sorted_presets:
        if key in ambiguous_keys:
            skipped_ambiguous.append((key, count))
        elif key not in existing_preset_docs:
            unmatched.append((key, count))
        else:
            matched_presets.append((key, count))

    print(f"\n[+] Preset Match Classification:")
    print(f"    - Matched presets (will update): {len(matched_presets)}")
    print(f"    - Unmatched presets (not in Firestore): {len(unmatched)}")
    print(f"    - Ambiguous presets (name collisions): {len(skipped_ambiguous)}")

    if unmatched:
        print(f"\n[?] {len(unmatched)} preset names in seedlist were not found in the presets collection:")
        for u_name, u_count in unmatched:
            print(f"    - '{u_name}': {u_count} rolls")

    if skipped_ambiguous:
        print(f"\n[!] {len(skipped_ambiguous)} preset names were skipped due to name collisions in presets collection:")
        for a_name, a_count in skipped_ambiguous:
            print(f"    - '{a_name}': {a_count} rolls")

    if not apply_changes:
        print("\n[*] DRY RUN finished. Run with '--apply' to persist counts into existing preset documents.")
        return

    print("\n[*] Applying aggregated counts to 'presets' collection in Firestore...")

    batch = db.batch()
    operations_in_batch = 0
    total_updated = 0

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for key, count in matched_presets:
        doc_ref, pdata = existing_preset_docs[key]
        existing_dl = pdata.get('downloads') or pdata.get('download_count') or 0
        final_count = max(existing_dl, count)

        existing_ts = parse_ts(pdata.get('download_timestamp'))
        latest_dt = latest_timestamps.get(key)
        dt_candidates = [d for d in (existing_ts, latest_dt) if d is not None]
        final_ts = to_iso(max(dt_candidates)) if dt_candidates else now_iso

        batch.set(doc_ref, {
            'downloads': final_count,
            'download_count': final_count,
            'download_timestamp': final_ts,
        }, merge=True)
        total_updated += 1
        operations_in_batch += 1

        if operations_in_batch >= batch_size:
            try:
                batch.commit()
                print(f"    ...committed batch of {operations_in_batch} preset records")
            except Exception as e:
                print(f"[!] Failed to commit batch: {e}")
                raise
            batch = db.batch()
            operations_in_batch = 0

    if operations_in_batch > 0:
        try:
            batch.commit()
            print(f"    ...committed final batch of {operations_in_batch} preset records")
        except Exception as e:
            print(f"[!] Failed to commit final batch: {e}")
            raise

    print(f"\n[+] Successfully backfilled preset downloads! Updated: {total_updated} presets.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Backfill preset download counts from seedlist collection.")
    parser.add_argument('--apply', action='store_true', help="Persist changes to Firestore (defaults to dry-run)")
    parser.add_argument('--batch-size', type=int, default=450, help="Firestore write batch size (max 500)")
    args = parser.parse_args()

    run_backfill(apply_changes=args.apply, batch_size=args.batch_size)
