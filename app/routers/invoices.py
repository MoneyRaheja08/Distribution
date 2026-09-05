import io
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pypdf import PdfReader

from ..auth import require_roles

router = APIRouter(prefix="/invoices", tags=["invoices"])
staff_only = require_roles("admin", "manager")


@router.post("/parse")
async def parse_invoice(file: UploadFile = File(...), _=Depends(staff_only)):
    """Extract bill no, date, amount and party name from a MARG-style invoice PDF."""
    data = await file.read()
    try:
        reader = PdfReader(io.BytesIO(data))
        txt = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        raise HTTPException(400, "Could not read that PDF")

    def find(pat):
        m = re.search(pat, txt)
        return m.group(1).strip() if m else None

    bill_no = find(r"Invoice No\.?\s*:\s*(\S+)")
    d = find(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})")
    party = find(r"Name\s*:\s*([A-Z0-9 &.\-]+)")
    totals = re.findall(r"GRAND TOTAL[^\d]*([\d,]+\.\d{2})", txt)
    total = totals[-1] if totals else None  # last page's grand total on multi-page invoices
    date_iso = None
    if d:
        dd, mm, yy = d.split("/")
        date_iso = f"{yy}-{mm}-{dd}"
    amount = float(total.replace(",", "")) if total else None
    if not (bill_no and amount):
        raise HTTPException(422, "Could not find invoice number/total — enter the bill manually")
    return {"bill_no": bill_no, "date": date_iso, "amount": amount, "party": (party or "").strip()}
