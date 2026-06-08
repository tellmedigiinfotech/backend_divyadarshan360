"""Block a spam phone number AND wipe its existing orders.

Adds the phone to blocked_phones/ (future create_order / create_cod_order
calls will return 403). Then lists every order whose customer.phone matches
and offers to delete each one along with any linked transactions/refunds.

Run from backend_dd360/:
    venv\\Scripts\\python.exe scripts\\block_and_purge.py +917799095000 "spam orders"

Optional --extra-order-ids flag deletes specific order IDs even if the
phone field doesn't match (useful when the spammer changed details):
    venv\\Scripts\\python.exe scripts\\block_and_purge.py +917799095000 "spam" \\
        --extra-order-ids T6jvJsJiauz76RLusdy5 R436Y5or88FeygjpQFg7
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(phone: str, reason: str, extra_ids: list[str]) -> None:
    from google.cloud.firestore_v1.base_query import FieldFilter

    from app.blocklist import block_phone
    from app.firebase import db, init_firebase

    init_firebase()
    firestore = db()

    print(f"=== Blocking {phone} ===")
    normalized = block_phone(phone, reason=reason, blocked_by="manual script")
    print(f"  added blocked_phones/{normalized}  (reason: {reason})")
    print()

    print(f"=== Orders where customer.phone == {normalized} ===")
    by_phone = list(
        firestore.collection("orders")
        .where(filter=FieldFilter("customer.phone", "==", normalized))
        .stream()
    )
    print(f"  found: {len(by_phone)}")

    extra_snaps = []
    for oid in extra_ids:
        snap = firestore.collection("orders").document(oid).get()
        if snap.exists:
            extra_snaps.append(snap)
            print(f"  + extra ID: {oid} (found)")
        else:
            print(f"  - extra ID: {oid} (not found, skipping)")
    print()

    all_snaps = {s.id: s for s in (by_phone + extra_snaps)}
    if not all_snaps:
        print("Nothing to delete. Block was applied; exiting.")
        return

    print(f"{'order_id':40s}  {'status':14s}  {'method':14s}  {'name':24s}  amount")
    print("-" * 110)
    for oid, snap in all_snaps.items():
        d = snap.to_dict() or {}
        cust = d.get("customer") or {}
        status = str(d.get("status") or "?")[:14]
        method = str(d.get("payment_method") or "razorpay")[:14]
        name = (cust.get("full_name") or "?")[:24]
        amount = int(d.get("amount", 0)) / 100
        print(f"{oid[:40]:40s}  {status:14s}  {method:14s}  {name:24s}  Rs.{amount:.2f}")
    print()
    print(f"Total to delete: {len(all_snaps)}")
    print('Type "WIPE" to confirm:')
    confirm = input("> ").strip()
    if confirm != "WIPE":
        print("Aborted. Block remains in place; orders kept.")
        return

    deleted_orders = 0
    deleted_related = 0
    for oid, snap in all_snaps.items():
        # Also clean transactions / refunds linked by razorpay_order_id
        rp_oid = (snap.to_dict() or {}).get("razorpay_order_id") or oid
        for coll in ("transactions", "refunds"):
            linked = list(
                firestore.collection(coll)
                .where(filter=FieldFilter("razorpay_order_id", "==", rp_oid))
                .stream()
            )
            for x in linked:
                x.reference.delete()
                deleted_related += 1
        snap.reference.delete()
        deleted_orders += 1
        print(f"  deleted: {oid}")

    print()
    print(f"Done. Orders deleted: {deleted_orders}, related docs deleted: {deleted_related}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("phone", help="E.164 phone, e.g. +917799095000")
    p.add_argument("reason", help="Why this phone is being blocked")
    p.add_argument(
        "--extra-order-ids",
        nargs="*",
        default=[],
        help="Explicit order IDs to delete even if their phone field differs",
    )
    args = p.parse_args()
    main(args.phone.strip(), args.reason.strip(), [s.strip() for s in args.extra_order_ids])
