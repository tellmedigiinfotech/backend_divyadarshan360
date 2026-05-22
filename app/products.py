from pydantic import BaseModel


class Product(BaseModel):
    sku: str
    name: str
    description: str
    unit_price_paise: int
    mrp_paise: int
    currency: str = "INR"
    max_quantity: int = 10


CATALOG: dict[str, Product] = {
    "mobile-vr-box": Product(
        sku="mobile-vr-box",
        name="Mobile VR Box",
        description="Cardboard-style universal mobile VR headset for Divya Darshan 360.",
        # TEMP: lowered to Rs.10 for a live Razorpay test. Revert to 59900
        # (Rs.599) once the live test is verified + refunded.
        unit_price_paise=1000,
        mrp_paise=1500,
        currency="INR",
        max_quantity=10,
    ),
}


def get_product(sku: str) -> Product | None:
    return CATALOG.get(sku)
