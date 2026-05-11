import razorpay

from .config import settings


client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


def create_order(amount_paise: int, currency: str, receipt: str, notes: dict | None = None) -> dict:
    payload = {
        "amount": int(amount_paise),
        "currency": currency,
        "receipt": receipt,
        "payment_capture": 1,
    }
    if notes:
        payload["notes"] = notes
    return client.order.create(payload)


def verify_signature(razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> bool:
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            }
        )
        return True
    except razorpay.errors.SignatureVerificationError:
        return False


def fetch_payment(razorpay_payment_id: str) -> dict:
    return client.payment.fetch(razorpay_payment_id)
