"""Published fictional documents. No real buyer or supplier data is used."""

import io

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

EXAMPLES = [
    {
        "id": "price_increase",
        "title": "Price increase · complete evidence",
        "description": "A fictional supplier requests 12%. What does the declared cost basket explain?",
    },
    {
        "id": "missing_evidence",
        "title": "Missing energy index",
        "description": "The current energy index is unknown. The service must withhold the scenario.",
    },
    {
        "id": "scope_mismatch",
        "title": "Specification changed",
        "description": "The proposed quote is for revision B. The baseline is revision A.",
    },
]


# Unstructured fictional correspondence. The labelled-template parser ignores these pages
# (no ``key = value`` lines); a reviewer may cite them to justify an override.
SUPPORTING_PAGES = {
    "scope_mismatch": """SENONI / FICTIONAL SUPPLIER CORRESPONDENCE / CLARIFICATION
Synthetic demonstration only. Not a real message.
From: Fictional Northbridge Components, sales desk
To: Fictional buyer, category team
Date: 2026-11-05
Subject: PANEL-100 price proposal dated 2026-11-01
The price proposal applies to PANEL-100 revision A, the same drawing as the baseline quotation.
The letter B printed on the proposal is a typing error. No drawing change has been issued.
The proposed unit price and validity dates are unchanged by this clarification.""",
    "missing_evidence": """SENONI / FICTIONAL INDEX BULLETIN / SUPPORTING NOTE
Synthetic demonstration only. Not a market observation.
Issued: 2026-11-06
Series: fictional energy index E-1, same series as the declared baseline index
Energy index E-1 for period 2026-10-01: 90.0
The October value was published after the buyer scenario sheet was prepared.
Materials, labour and freight values are unchanged by this bulletin.""",
}


def document_text(case: str, supporting: bool = False) -> str:
    """Return the labelled fixture; ``supporting`` appends the unstructured review-pack page."""
    if case not in {x["id"] for x in EXAMPLES}:
        raise KeyError(case)
    baseline = """SENONI / FICTIONAL SUPPLIER DOCUMENT / BASELINE
Synthetic demonstration only. Not a real quotation.
baseline.supplier = Fictional Northbridge Components
baseline.sku = PANEL-100
baseline.revision = A
baseline.currency = GBP
baseline.unit = piece
baseline.quantity = 1000
baseline.unit_price = 100.00
baseline.quote_date = 2026-01-01
baseline.valid_until = 2026-02-01
baseline.freight = included"""
    proposal = """SENONI / FICTIONAL SUPPLIER DOCUMENT / PRICE PROPOSAL
Synthetic demonstration only. Not a real quotation.
proposal.supplier = Fictional Northbridge Components
proposal.sku = PANEL-100
proposal.revision = A
proposal.currency = GBP
proposal.unit = piece
proposal.quantity = 1000
proposal.unit_price = 112.00
proposal.quote_date = 2026-11-01
proposal.valid_until = 2026-12-31
proposal.freight = included"""
    drivers = """SENONI / FICTIONAL BUYER SCENARIO / DECLARED ASSUMPTIONS
All shares and index values below are synthetic, not market observations.
as_of = 2026-11-09
annual_volume = 10000
driver.materials = share:0.50; baseline_index:100; current_index:106; baseline_period:2026-01-01; current_period:2026-10-01
driver.energy = share:0.20; baseline_index:100; current_index:90; baseline_period:2026-01-01; current_period:2026-10-01
driver.labour = share:0.20; baseline_index:100; current_index:104; baseline_period:2026-01-01; current_period:2026-10-01
driver.freight = share:0.10; baseline_index:100; current_index:105; baseline_period:2026-01-01; current_period:2026-10-01
assumption = Baseline price moves proportionally with the declared index basket.
assumption = Cost shares are assumed; margin, FX, hedging and productivity are held constant.
assumption = Annual volume is an illustration, not committed demand or realised savings."""
    if case == "missing_evidence":
        drivers = drivers.replace("current_index:90;", "current_index:UNKNOWN;")
    if case == "scope_mismatch":
        proposal = proposal.replace("proposal.revision = A", "proposal.revision = B")
    pages = [baseline, proposal, drivers]
    if supporting and case in SUPPORTING_PAGES:
        pages.append(SUPPORTING_PAGES[case])
    return "\f".join(pages)


def document_pdf(case: str, supporting: bool = False) -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=landscape(A4), invariant=1)
    pdf.setTitle("Fictional sourcing evidence - " + case)
    width, height = landscape(A4)
    for page_number, text in enumerate(document_text(case, supporting).split("\f"), 1):
        pdf.setFillColorRGB(0.06, 0.13, 0.2)
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(40, height - 50, "SENONI | Sourcing evidence")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, height - 72, "FICTIONAL DATA / independent engineering demonstration")
        pdf.setFont("Courier", 8)
        y = height - 110
        for line in text.splitlines():
            pdf.drawString(40, y, line)
            y -= 22
        pdf.setFont("Helvetica", 9)
        footer = (
            "Labelled-document format v1 | No industrial validation"
            if page_number <= 3
            else "Unstructured supporting page | Not read by the labelled-template parser"
        )
        pdf.drawString(40, 30, f"Page {page_number} | {footer}")
        pdf.showPage()
    pdf.save()
    return output.getvalue()
