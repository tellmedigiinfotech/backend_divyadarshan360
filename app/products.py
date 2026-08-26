from pydantic import BaseModel


class Product(BaseModel):
    sku: str
    name: str
    description: str
    unit_price_paise: int
    mrp_paise: int
    currency: str = "INR"
    max_quantity: int = 10
    # Stable IDs exposed to Fastrr (Shiprocket Checkout) in the catalog APIs and
    # used as the variant_id when generating a checkout token. Must stay constant.
    fastrr_product_id: str = ""
    fastrr_variant_id: str = ""
    # Physical attributes Fastrr/Shiprocket need for shipping.
    weight_kg: float = 0.5


CATALOG: dict[str, Product] = {
    "mobile-vr-box": Product(
        sku="mobile-vr-box",
        name="Mobile VR Box",
        description="Cardboard-style universal mobile VR headset for Divya Darshan 360.",
        unit_price_paise=69900,
        mrp_paise=299900,
        currency="INR",
        max_quantity=10,
        fastrr_product_id="900001",
        fastrr_variant_id="900101",
        weight_kg=0.5,
    ),
}


def get_product(sku: str) -> Product | None:
    return CATALOG.get(sku)


def get_product_by_fastrr_variant(variant_id: str) -> Product | None:
    for p in CATALOG.values():
        if p.fastrr_variant_id == variant_id:
            return p
    return None
