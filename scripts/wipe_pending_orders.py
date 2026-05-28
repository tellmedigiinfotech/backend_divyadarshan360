"""Wipe only the pending / failed / expired orders, keep paid ones intact.

Deletes orders whose status is in:
    created, awaiting_payment, failed, expired

Leaves orders with status == "paid" untouched.

Also cleans related side tables (transactions/refunds/unmatched_payments)
for the deleted orders.

Run from backend_dd360/:
    venv\\Scripts\\python.exe scripts\\wipe_pending_orders.py
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


PENDING_STATUSES = {"created", "awaiting_payment", "failed", "expired"}
BATCH_SIZE = 400


def main() -> None:
    from app.firebase import db, init_firebase

    init_firebase()
    firestore = db()

    print("Scanning orders by status...")
    orders = list(firestore.collection("orders").stream())

    by_status: dict[str, list] = {}
    for snap in orders:
        data = snap.to_dict() or {}
        st = data.get("status") or "unknown"
        by_status.setdefault(st, []).append(snap)

    print("\nCurrent breakdown:")
    for st, docs in sorted(by_status.items()):
        marker = "-> will delete" if st in PENDING_STATUSES else "   (kept)"
        print(f"  {st:25s} {len(docs):4d} doc(s)   {marker}")

    to_delete = [
        snap for st, docs in by_status.items() if st in PENDING_STATUSES for snap in docs
    ]
    if not to_delete:
        print("\nNothing to delete. Exiting.")
        return

    deleted_order_ids = {snap.id for snap in to_delete}

    # Also find transactions / unmatched_payments tied to these orders to clean up.
    related_to_delete = []
    for name in ("transactions", "unmatched_payments", "refunds"):
        coll_refs = []
        for snap in firestore.collection(name).stream():
            d = snap.to_dict() or {}
            linked = (
                d.get("razorpay_order_id")
                or d.get("linked_order_id")
                or (d.get("razorpay_payment_body") or {}).get("order_id")
            )
            if linked in deleted_order_ids:
                coll_refs.append(snap)
        if coll_refs:
            related_to_delete.append((name, coll_refs))
            print(f"  {name:25s} {len(coll_refs):4d} doc(s) linked to pending orders")

    total = len(to_delete) + sum(len(refs) for _, refs in related_to_delete)
    print(f"\nTotal to delete: {total}")
    print("Paid orders + their transactions will be KEPT.\n")
    print("Type WIPE (in caps) to confirm:")
    confirm = input("> ").strip()
    if confirm != "WIPE":
        print("Aborted — no changes made.")
        return

    print("\nDeleting...")
    deleted_total = 0

    refs = [snap.reference for snap in to_delete]
    while refs:
        chunk = refs[:BATCH_SIZE]
        refs = refs[BATCH_SIZE:]
        batch = firestore.batch()
        for ref in chunk:
            batch.delete(ref)
        batch.commit()
        deleted_total += len(chunk)
    print(f"  orders                    {len(to_delete)} doc(s) deleted")

    for name, snaps in related_to_delete:
        refs = [s.reference for s in snaps]
        n = 0
        while refs:
            chunk = refs[:BATCH_SIZE]
            refs = refs[BATCH_SIZE:]
            batch = firestore.batch()
            for ref in chunk:
                batch.delete(ref)
            batch.commit()
            n += len(chunk)
        print(f"  {name:25s} {n} doc(s) deleted")
        deleted_total += n

    print(f"\nDone. Total deleted: {deleted_total}")


if __name__ == "__main__":
    main()
