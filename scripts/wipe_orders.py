"""Wipe order-related Firestore collections.

Touches:   orders, transactions, refunds, unmatched_payments
Leaves:    users (mobile-app data), contact_messages, anything else

Run from backend_dd360/:
    venv\\Scripts\\python.exe scripts\\wipe_orders.py

Re-uses the backend's firebase admin init, which reads SERVICE_ACCOUNT_PATH
from your .env (same credentials the deployed function uses).
"""

from __future__ import annotations

import sys
from pathlib import Path


# Make the app package importable when run from backend_dd360/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


COLLECTIONS_TO_WIPE = [
    "orders",
    "transactions",
    "refunds",
    "unmatched_payments",
]

BATCH_SIZE = 400  # Firestore batch limit is 500


def main() -> None:
    from app.firebase import db, init_firebase

    init_firebase()
    firestore = db()

    print("Pre-flight: counting docs in each collection...")
    counts = {}
    for name in COLLECTIONS_TO_WIPE:
        n = sum(1 for _ in firestore.collection(name).list_documents())
        counts[name] = n
        print(f"  {name:25s} {n} doc(s)")

    total = sum(counts.values())
    if total == 0:
        print("\nNothing to delete. Exiting.")
        return

    print(f"\nTotal documents to delete: {total}")
    print("\nThis is IRREVERSIBLE. Type WIPE (in caps) to confirm:")
    confirm = input("> ").strip()
    if confirm != "WIPE":
        print("Aborted — no changes made.")
        return

    print("\nDeleting...")
    grand_total = 0
    for name, count in counts.items():
        if count == 0:
            continue
        coll = firestore.collection(name)
        deleted_here = 0
        # Iterate in chunks via list_documents() to avoid loading all refs at once
        refs = list(coll.list_documents())
        while refs:
            chunk = refs[:BATCH_SIZE]
            refs = refs[BATCH_SIZE:]
            batch = firestore.batch()
            for ref in chunk:
                batch.delete(ref)
            batch.commit()
            deleted_here += len(chunk)
        print(f"  {name:25s} {deleted_here} doc(s) deleted")
        grand_total += deleted_here

    print(f"\nDone. Total deleted: {grand_total}")


if __name__ == "__main__":
    main()
