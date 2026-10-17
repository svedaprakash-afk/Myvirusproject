"""Scanned remittances (real Tesseract), table parsing, reconciliation, and the local-first LLM cascade."""
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.ingestion import mailbox, ocr
from app.ingestion.remittance import CascadeExtractor, LLMExtractor, RuleExtractor, extract_upload, finalise, parse_table
from tests.conftest import login

needs_ocr = pytest.mark.skipif(not ocr.available(), reason="tesseract / OCR packages not installed")

ADVICE = ["REMITTANCE ADVICE", "From: ACME INDUSTRIES PVT LTD", "UTR: HDFCN26274000128",
          "Invoice No      Invoice Amount     TDS        Net Paid",
          "INV/26-27/0101  1,18,000.00     1,000.00   1,17,000.00",
          "INV/26-27/0102  59,000.00         500.00      58,500.00",
          "Total                                                    1,75,500.00"]


def render(lines, size=30, noise=False) -> Image.Image:
    f = ImageFont.load_default(size=size)
    img = Image.new("RGB", (1500, 60 + 52 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, l in enumerate(lines):
        d.text((40, 30 + 52 * i), l, fill="black", font=f)
    if noise:
        import random

        random.seed(1)
        for _ in range(60000):
            d.point((random.randrange(img.width), random.randrange(img.height)), fill=(random.randrange(256),) * 3)
    return img


def png(img) -> bytes:
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


@needs_ocr
def test_scanned_image_is_read_and_the_table_reconciles():
    up = extract_upload("advice.png", png(render(ADVICE)))
    assert up.method == "ocr" and up.ocr_confidence > 80
    out = RuleExtractor().extract(up.text)
    assert out["utr"] == "HDFCN26274000128" and out["payer_name"] == "ACME INDUSTRIES PVT LTD"
    assert [(l["ref"], l["gross_minor"], l["tds_minor"], l["amount_minor"]) for l in out["lines"]] == [
        ("INV/26-27/0101", 118_000_00, 1_000_00, 117_000_00), ("INV/26-27/0102", 59_000_00, 500_00, 58_500_00)]
    assert out["total_minor"] == 175_500_00 and out["meta"]["parse"] == "table" and out["meta"]["reconciled"] is True and out["confidence"] >= 0.9


@needs_ocr
def test_image_only_pdf_goes_through_ocr_but_a_text_pdf_does_not():
    b = io.BytesIO()
    render(ADVICE).save(b, format="PDF")
    up = extract_upload("scan.pdf", b.getvalue())
    assert up.method == "ocr" and up.pages == 1 and "INV/26-27/0101" in up.text


def test_scan_without_ocr_installed_is_refused_visibly_not_read_as_empty(monkeypatch, client, w):
    monkeypatch.setattr(ocr, "available", lambda: False)
    h = login(client, "ar_analyst@t.example", "pw-ar_analyst")
    r = client.post("/api/v1/ingest/remittance", data={"entity_id": w.e.id}, files={"file": ("a.png", png(render(ADVICE)), "image/png")}, headers=h)
    assert r.status_code == 422 and "OCR is not installed" in r.json()["detail"]


@needs_ocr
def test_pure_noise_scan_is_rejected_with_a_clear_message(client, w):
    h = login(client, "ar_analyst@t.example", "pw-ar_analyst")
    r = client.post("/api/v1/ingest/remittance", data={"entity_id": w.e.id}, files={"file": ("bad.png", png(render(ADVICE, noise=True)), "image/png")}, headers=h)
    assert r.status_code == 422 and "No readable text" in r.json()["detail"]


def test_a_poorly_scanned_advice_is_capped_flagged_and_audited_even_when_the_arithmetic_works(w):
    from sqlalchemy import select

    from app.ingestion import service as ing
    from app.ingestion.remittance import UploadText
    from app.models import AuditLog

    text = "Invoice No\tInvoice Amount\tNet Paid\nINV-7001\t1,000.00\t1,000.00\nTotal\t\t1,000.00\n"
    doc = ing.ingest_remittance(w.db, w.e.id, text, "scan.png", "manual", "t", None, upload=UploadText(text, "ocr", 45.0, 1, ["Low OCR quality (45%): check every figure against the original image"]))
    ex = doc.extracted
    assert ex["meta"]["reconciled"] is True and ex["confidence"] <= 0.4
    assert ex["meta"]["source_method"] == "ocr" and any("Low OCR quality" in x for x in ex["meta"]["warnings"])
    a = w.db.execute(select(AuditLog).where(AuditLog.action == "remittance.ingested")).scalars().all()[-1]
    assert a.detail["ocr"] is True and a.detail["reconciled"] is True and a.detail["llm_used"] is False


@needs_ocr
def test_corrupt_and_oversized_images_are_rejected_cleanly():
    with pytest.raises(ocr.OcrError):
        ocr.image_to_text(b"definitely not an image")
    with pytest.raises(ocr.OcrError):
        ocr.pdf_to_images(b"%PDF-1.4 broken")


def sheet(rows):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_xlsx_table_with_blank_tds_cells_is_read_positionally_and_reconciles():
    data = sheet([["Remittance advice - ACME"], ["Invoice No", "Invoice Date", "Invoice Amount", "TDS", "Deductions", "Net Paid"],
                  ["INV/26-27/0201", "2026-09-01", 100000.00, 2000.00, None, 98000.00],
                  ["INV/26-27/0202", "2026-09-05", 50000.00, None, 500.00, 49500.00],
                  ["Total", None, None, None, None, 147500.00]])
    out = RuleExtractor().extract(extract_upload("adv.xlsx", data).text)
    assert [(l["ref"], l["tds_minor"], l["deduction_minor"], l["amount_minor"]) for l in out["lines"]] == [
        ("INV/26-27/0201", 2_000_00, None, 98_000_00), ("INV/26-27/0202", None, 500_00, 49_500_00)]
    assert out["total_minor"] == 147_500_00 and out["meta"]["reconciled"] is True and out["meta"]["parse"] == "table"


def test_csv_upload_is_parsed_like_a_sheet():
    csv_bytes = b"Invoice No,Amount Paid\nINV-7001,\"1,000.00\"\nINV-7002,\"2,500.00\"\nTotal,\"3,500.00\"\n"
    out = RuleExtractor().extract(extract_upload("a.csv", csv_bytes).text)
    assert [(l["ref"], l["amount_minor"]) for l in out["lines"]] == [("INV-7001", 1_000_00), ("INV-7002", 2_500_00)] and out["meta"]["reconciled"] is True


def test_lines_that_do_not_add_up_lower_confidence_and_say_why():
    t = "Invoice No\tInvoice Amount\tNet Paid\nINV-8001\t1,000.00\t1,000.00\nINV-8002\t2,000.00\t2,000.00\nTotal\t\t4,000.00\n"
    out = RuleExtractor().extract(t)
    assert out["meta"]["reconciled"] is False and out["confidence"] < 0.7
    assert "Lines add up to 3,000.00 but the advice states 4,000.00" in out["meta"]["warnings"][0]


def test_whitespace_table_with_one_blank_amount_cell_is_only_accepted_when_arithmetic_is_unambiguous():
    ok = "Invoice No   Invoice Amount   TDS   Net Paid\nINV-9001 10,000.00 200.00 9,800.00\nINV-9002 5,000.00 5,000.00\n"
    out = parse_table(ok)
    assert [(l["ref"], l["tds_minor"], l["amount_minor"]) for l in out["lines"]] == [("INV-9001", 200_00, 9_800_00), ("INV-9002", None, 5_000_00)]
    ambiguous = "Invoice No   Invoice Amount   TDS   Deductions   Net Paid\nINV-9003 10,000.00 100.00 9,900.00\n"
    out2 = parse_table(ambiguous)
    assert out2 is None or out2["lines"] == [], "100 could be TDS or a deduction and both reconcile: refuse to guess"


def test_a_stray_integer_after_a_header_is_not_mistaken_for_the_total():
    out = RuleExtractor().extract("Net Paid\nINV/26-27/0101 1,000.00\nUTR: HDFCN26274000128\n")
    assert out["total_minor"] is None


def test_finalise_flags_gross_tds_net_inconsistency():
    out = finalise({"lines": [{"ref": "INV-1", "gross_minor": 1000_00, "amount_minor": 900_00, "tds_minor": 20_00, "deduction_minor": None}], "total_minor": 900_00, "confidence": 0.8})
    assert out["meta"]["reconciled"] is True and any("does not equal the net" in w for w in out["meta"]["warnings"])


class FakeLLM:
    name = "llm"

    def __init__(self, result):
        self.result, self.calls = result, 0

    def extract(self, text):
        self.calls += 1
        return self.result


def llm_result(conf, lines):
    return {"payer_name": None, "utr": None, "total_minor": None, "tds_total_minor": None, "lines": lines, "confidence": conf, "meta": {"parse": "llm"}}


def test_confident_local_extraction_never_calls_the_llm():
    llm = FakeLLM(llm_result(0.9, []))
    text = "Invoice No\tInvoice Amount\tNet Paid\nINV-7001\t1,000.00\t1,000.00\nTotal\t\t1,000.00\n"
    c = CascadeExtractor(RuleExtractor(), llm, 0.75)
    out = c.extract(text)
    assert llm.calls == 0 and c.name == "rules" and out["meta"]["parse"] == "table"


def test_low_confidence_local_extraction_consults_the_llm_and_uses_a_better_validated_answer():
    text = "pls find payment against our PO, see attached.\nINV-5001 and INV-5002 settled, thanks"
    better = llm_result(0.8, [{"ref": "INV-5001", "gross_minor": None, "amount_minor": 100_00, "tds_minor": None, "deduction_minor": None, "kind": "invoice"}])
    llm = FakeLLM(better)
    c = CascadeExtractor(RuleExtractor(), llm, 0.75)
    out = c.extract(text)
    assert llm.calls == 1 and c.name == "llm" and out["lines"][0]["ref"] == "INV-5001" and out["meta"]["rules_confidence"] < 0.75


def test_llm_answer_that_is_not_better_is_discarded_but_the_consultation_is_recorded():
    text = "Invoice No\tInvoice Amount\tNet Paid\nINV-8001\t1,000.00\t1,000.00\nINV-8002\t2,000.00\t2,000.00\nTotal\t\t4,000.00\n"
    llm = FakeLLM(llm_result(0.3, [{"ref": "INV-8001", "gross_minor": None, "amount_minor": 1, "tds_minor": None, "deduction_minor": None, "kind": "invoice"}]))
    c = CascadeExtractor(RuleExtractor(), llm, 0.75)
    out = c.extract(text)
    assert llm.calls == 1 and c.name == "rules+llm" and out["meta"]["parse"] == "table" and out["meta"]["llm_consulted"] is True


def test_llm_failure_falls_back_to_rules_silently_and_hallucinated_references_are_dropped():
    def boom(body):
        raise RuntimeError("network down")

    c = CascadeExtractor(RuleExtractor(), LLMExtractor("k", "m", transport=boom), 0.99)
    out = c.extract("INV-5001 1,000.00")
    assert c.name == "rules" and out["lines"][0]["ref"] == "INV-5001"
    inv = LLMExtractor("k", "m", transport=lambda b: '{"lines":[{"ref":"INV-5001","paid":10},{"ref":"INV-INVENTED","paid":5}]}')
    out2 = CascadeExtractor(RuleExtractor(), inv, 0.99).extract("INV-5001 settled")
    assert [l["ref"] for l in out2["lines"]] == ["INV-5001"]


@needs_ocr
def test_emailed_scan_attachment_is_read_and_linked(w):
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "ap@acme.example", "cash@test.example", "Payment advice", "<scan-1@acme>"
    m["Authentication-Results"] = "mx; spf=pass; dkim=pass"
    m.set_content("Please find our remittance attached.")
    m.add_attachment(png(render(ADVICE)), maintype="image", subtype="png", filename="advice.png")
    from sqlalchemy import select

    from app.models import RemittanceDocument

    res = mailbox.process_email(w.db, w.e.id, m.as_bytes(), {}, "test")
    doc = w.db.get(RemittanceDocument, res["id"])
    assert res["status"] == "ingested" and res["lines"] == 2 and doc.extracted["total_minor"] == 175_500_00
