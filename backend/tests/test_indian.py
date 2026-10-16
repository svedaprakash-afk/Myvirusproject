from datetime import date

from app.indian import (financial_year, is_valid_gstin, is_valid_pan, make_gstin, mask_account, normalise_name, pan_category, pan_from_gstin,
                        parse_narration)
from app.money import parse_amount, pct_of, to_minor


def test_gstin_checksum_roundtrip_and_rejects_tampering():
    g = make_gstin("27", "AABCM4421F")
    assert is_valid_gstin(g) and len(g) == 15
    assert pan_from_gstin(g) == "AABCM4421F"
    bad = g[:-1] + ("A" if g[-1] != "A" else "B")
    assert not is_valid_gstin(bad)
    assert not is_valid_gstin("27AABCM4421F1Z") and not is_valid_gstin(None)


def test_pan_validation_and_category():
    assert is_valid_pan("AABCM4421F") and not is_valid_pan("AABCM44211")
    assert pan_category("AABCM4421F") == "C" and pan_category("ABCPK1234D") == "P"


def test_financial_year_quarters():
    assert financial_year(date(2026, 3, 31)) == ("2025-26", "Q4")
    assert financial_year(date(2026, 4, 1)) == ("2026-27", "Q1")
    assert financial_year(date(2026, 10, 16)) == ("2026-27", "Q3")


def test_parse_amount_indian_formats():
    assert parse_amount("1,23,456.78") == 12345678
    assert parse_amount("₹ 5,000") == 500000
    assert parse_amount("1234.5 Cr") == 123450
    assert parse_amount("(100.00)") == -10000
    assert parse_amount("") is None and parse_amount("abc") is None and parse_amount("-") is None


def test_money_rounding_is_half_up_and_exact():
    assert to_minor("0.005") == 1 and to_minor("10.10") == 1010
    assert pct_of(11800000, 200) == 236000


def test_name_normalisation_collapses_legal_suffixes():
    assert normalise_name("M/S Acme Industries Pvt. Ltd.") == normalise_name("ACME INDUSTRIES PRIVATE LIMITED") == "ACME INDUSTRIES"
    assert normalise_name("Tata & Sons Ltd") == "TATA SONS"


def test_mask_account():
    assert mask_account("50200012345678") == "XXXXXXXXXX5678"


def test_narration_neft_rtgs_imps_upi_cheque():
    n = parse_narration("NEFT-HDFCN52024100112345-ACME INDUSTRIES PVT LTD-INV 1023-HDFC0000123")
    assert (n.channel, n.utr, n.payer_name, n.payer_ifsc) == ("NEFT", "HDFCN52024100112345", "ACME INDUSTRIES PVT LTD", "HDFC0000123")
    r = parse_narration("RTGS-SBINR52024100800123456789-BHARAT FORGINGS LTD-INV/24-25/0456")
    assert r.channel == "RTGS" and r.payer_name == "BHARAT FORGINGS LTD" and r.utr.startswith("SBINR")
    i = parse_narration("MMT/IMPS/427812345678/INV 1023/ACME TRADERS")
    assert i.channel == "IMPS" and i.utr == "427812345678" and i.payer_name == "ACME TRADERS"
    u = parse_narration("UPI-ACME SALES-acme@okhdfc-HDFC0001234-427812345678-pay inv 1023")
    assert u.channel == "UPI" and u.payer_vpa == "acme@okhdfc" and u.payer_name == "ACME SALES" and u.utr == "427812345678"
    c = parse_narration("CHQ DEP - 123456 - Kumar Steels")
    assert c.channel == "CHEQUE" and c.utr == "123456" and c.payer_name == "Kumar Steels"


def test_narration_name_and_reference_in_one_segment():
    n = parse_narration("CHQ DEP - 449193 - SURYA INDUSTRIES PVT LTD BILL 0320 0457")
    assert n.payer_name == "SURYA INDUSTRIES PVT LTD"


def test_narration_non_customer_credits_are_classified():
    assert parse_narration("INT.PD:01-07-2024 TO 30-09-2024").kind == "interest"
    assert parse_narration("GST ON BANK CHARGES REVERSAL").kind in ("reversal", "charges")
    g = parse_narration("RAZORPAY SETTLEMENT UTR ICIC24100500012345")
    assert g.kind == "gateway" and g.channel == "GATEWAY"


def test_narration_never_crashes_on_garbage():
    for s in ["", "   ", "-/-/-", "😀" * 50, "A" * 5000, "NEFT", "UPI//////"]:
        parse_narration(s)
