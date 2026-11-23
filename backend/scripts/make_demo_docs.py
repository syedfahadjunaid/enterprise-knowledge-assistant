"""Builds the two binary demo documents (DOCX and PDF) in demo-docs/.

The generated files are committed; rerun this only after changing the text below:

    .\\.venv\\Scripts\\python.exe scripts\\make_demo_docs.py

Needs the dev dependencies (fpdf2).
"""

from pathlib import Path

import docx
from fpdf import FPDF

OUT = Path(__file__).resolve().parents[2] / "demo-docs"

TRAVEL_POLICY = [
    ("title", "Travel and expense policy"),
    ("p", "This policy explains how to book business travel and claim expenses back."),
    ("h1", "Booking travel"),
    ("p", "Book flights, trains and hotels through the company travel portal. Fly economy class"
     " for flights under six hours and premium economy for longer flights. Take the train"
     " instead of flying when the journey is under four hours."),
    ("h1", "Hotels"),
    ("p", "The hotel limit is 180 EUR per night. In London, Paris and New York the limit is"
     " 250 EUR per night."),
    ("h1", "Meals and per diem"),
    ("p", "The daily meal allowance is 45 EUR for domestic trips and 60 EUR for international"
     " trips. Alcohol is only reimbursed when you entertain a customer."),
    ("h1", "Submitting expenses"),
    ("bullet", "Submit expenses in the expense tool within 30 days of the purchase."),
    ("bullet", "Attach a receipt for every item above 25 EUR."),
    ("bullet", "Private car use is reimbursed at 0.30 EUR per kilometre."),
    ("h1", "Approval limits"),
    ("p", "Expense claims are approved according to their total amount:"),
    ("table", [
        ("Amount", "Approved by"),
        ("Up to 500 EUR", "Your manager"),
        ("500 to 5,000 EUR", "Department head"),
        ("Above 5,000 EUR", "CFO"),
    ]),
    ("h1", "Not reimbursed"),
    ("p", "Traffic fines, parking tickets, minibar items, upgrades you chose yourself and"
     " travel insurance (the company insurance already covers you) are not reimbursed."),
]  # fmt: skip

RUNBOOK_PAGES = [
    "Platform incident runbook\n\n"
    "Database failover\n\n"
    "Use this when the primary database cluster pg-main is unhealthy. First check that the"
    " replica's replication lag is under 30 seconds. Then run the switchover command from the"
    " ops bastion host and confirm the new primary accepts writes. Finally, restart the"
    " connection poolers so applications reconnect to the new primary.",
    "Kubernetes node pressure\n\n"
    "If a node reports memory or disk pressure, cordon the node so no new pods are scheduled,"
    " then drain it. The cluster autoscaler replaces the node within about ten minutes. Never"
    " drain more than one node at a time in the production cluster.",
    "Rolling back a deployment\n\n"
    "If error rates rise after a deploy, roll back first and investigate later. Use the"
    " rollback button in the deployment dashboard, which restores the previous image within two"
    " minutes. Pause the deployment pipeline until the post-mortem is done.",
    "Customer communication\n\n"
    "For a SEV1 incident, update the public status page within 15 minutes of declaring the"
    " incident, then every 30 minutes until it is resolved. The incident commander approves"
    " every status page message.",
]


def make_docx(path: Path) -> None:
    document = docx.Document()
    document.core_properties.title = "Travel and expense policy"
    for kind, content in TRAVEL_POLICY:
        if kind == "title":
            document.add_heading(content, level=0)
        elif kind == "h1":
            document.add_heading(content, level=1)
        elif kind == "p":
            document.add_paragraph(content)
        elif kind == "bullet":
            document.add_paragraph(content, style="List Bullet")
        elif kind == "table":
            table = document.add_table(rows=len(content), cols=2)
            for r, row in enumerate(content):
                for c, value in enumerate(row):
                    table.cell(r, c).text = value
    document.save(str(path))


def make_pdf(path: Path) -> None:
    pdf = FPDF()
    pdf.set_title("Platform incident runbook")
    for page in RUNBOOK_PAGES:
        pdf.add_page()
        title, _, body = page.partition("\n\n")
        pdf.set_font("Helvetica", style="B", size=14)
        pdf.multi_cell(0, 8, title)
        pdf.ln(4)
        pdf.set_font("Helvetica", size=11)
        pdf.multi_cell(0, 6, body)
    pdf.output(str(path))


if __name__ == "__main__":
    make_docx(OUT / "travel-and-expense-policy.docx")
    make_pdf(OUT / "platform-incident-runbook.pdf")
    print("wrote", OUT / "travel-and-expense-policy.docx", OUT / "platform-incident-runbook.pdf")
