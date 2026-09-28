"""Create (or re-create) the Phase 14 sample document used by evaluate.py.

Uploads a small, fully synthetic insurance-policy PDF through the public
/POST /documents/upload endpoint and prints the resulting document id.

Usage:
    python scripts/create_phase14_sample_document.py [base_url]
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FILENAME = "phase14_sample_insurance_policy.pdf"

SAMPLE_TEXT = (
    "Acme Health Insurance Policy\n"
    "\n"
    "Section 1. Hospitalization deductible. The annual hospitalization "
    "deductible is five hundred dollars (500 USD) per insured person and "
    "one thousand dollars (1000 USD) per family per policy year.\n"
    "\n"
    "Section 2. Annual coverage limit. The maximum annual coverage limit "
    "under this policy is one million dollars (1,000,000 USD) per insured "
    "person for all covered hospitalization and outpatient claims combined.\n"
    "\n"
    "Section 3. Waiting period. A waiting period of thirty (30) days "
    "applies to all claims arising from pre-existing conditions. No claim "
    "for a pre-existing condition is payable during the first thirty days "
    "of the policy.\n"
    "\n"
    "Section 4. Cancellation policy. The policyholder may cancel this "
    "policy at any time by giving thirty (30) days written notice to Acme "
    "Health Insurance. A pro-rated refund of premiums is returned for the "
    "unused portion of the policy period. If Acme cancels the policy, "
    "sixty (60) days written notice is given.\n"
    "\n"
    "Section 5. Claim filing deadline. Every claim must be filed within "
    "ninety (90) days of the date of service. Claims filed after ninety "
    "days are denied unless the delay was caused by Acme.\n"
    "\n"
    "Section 6. Indemnification. Acme Health Insurance indemnifies the "
    "policyholder against covered claims subject to the annual coverage "
    "limit and the deductible stated in Section 1.\n"
)


def build_pdf(text: str) -> bytes:
    import fitz  # PyMuPDF

    doc = fitz.open()
    page = doc.new_page()
    rect = page.rect + (50, 50, -50, -50)
    page.insert_textbox(rect, text, fontsize=10.5, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    pdf_bytes = build_pdf(SAMPLE_TEXT)

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/documents/upload",
            files={"file": (FILENAME, io.BytesIO(pdf_bytes), "application/pdf")},
        )
        if resp.status_code >= 400:
            print(f"Upload failed: HTTP {resp.status_code} {resp.text[:300]}")
            return 1
        payload = resp.json()

    print(f"document_id={payload['id']}")
    print(f"filename={payload['filename']}")
    print(f"status={payload['status']} chunks={payload.get('chunk_count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
