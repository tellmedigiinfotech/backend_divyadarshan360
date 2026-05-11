from fastapi import APIRouter

from ..firebase import SERVER_TIMESTAMP, db
from ..schemas.customer import ContactFormInput, ContactFormOutput


router = APIRouter(prefix="/customers", tags=["Customers"])


@router.post("/contact", response_model=ContactFormOutput)
def submit_contact_form(payload: ContactFormInput) -> ContactFormOutput:
    doc_ref = db().collection("contact_messages").document()
    doc_ref.set(
        {
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
            "subject": payload.subject,
            "message": payload.message,
            "created_at": SERVER_TIMESTAMP,
        }
    )
    return ContactFormOutput(id=doc_ref.id)
