"""Deterministic synthetic data for demos and for measuring the matching engine against ground truth.

IMPORTANT: this data was written by the same authors as the matcher. Accuracy measured on it shows the pipeline
is internally consistent and exercises every code path; it is NOT evidence of accuracy on a real customer's
bank statements. Real accuracy has to be measured on a design partner's historical data (see docs/EVALUATION.md).
"""
from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..indian import make_gstin
from ..money import pct_of

FIRST = ["Bharat", "Sundaram", "Kaveri", "Himalaya", "Orient", "Deccan", "Gujarat", "Malwa", "Konkan", "Vindhya", "Nilgiri", "Aravali",
         "Triveni", "Sahyadri", "Godavari", "Ganga", "Surya", "Chola", "Pragati", "Navkar", "Vishwa", "Maitri", "Udaan", "Tejas"]
SECOND = ["Forgings", "Auto Components", "Precision Tools", "Cement Works", "Pumps & Valves", "Fasteners", "Alloys", "Polymers",
          "Engineering", "Castings", "Hydraulics", "Bearings", "Electricals", "Packaging", "Textiles", "Machinery", "Steel Traders",
          "Industries", "Enterprises", "Infra Projects", "Logistics", "Chemicals"]
SUFFIX = ["Pvt Ltd", "Ltd", "Private Limited", "LLP", "Limited"]
STATES = ["27", "24", "29", "33", "07", "06", "09", "19", "36", "23"]
BANKS = [("HDFC", "HDFC0000123"), ("ICIC", "ICIC0000456"), ("SBIN", "SBIN0001234"), ("UTIB", "UTIB0000789"), ("KKBK", "KKBK0000321"),
         ("YESB", "YESB0000555"), ("PUNB", "PUNB0123400")]


@dataclass
class Cust:
    code: str
    name: str
    gstin: str
    pan: str
    terms: int
    tier: str
    tds_bp: int | None
    tds_base: str
    van: str | None
    bank: tuple[str, str]
    vpa: str
    channel_pref: str
    refs_in_narration: float
    name_style: str
    psu: bool = False


@dataclass
class Pay:
    d: date
    amount: int
    channel: str
    payer: str
    ref_text: str
    cust: Cust | None
    scenario: str
    expect_invoices: list[str]
    expect: str                   # auto | review | exception | any_match
    expect_exception: str | None = None
    account: str = "main"         # main | collect | gateway
    vpa: str | None = None
    van_text: str | None = None
    remittance: str | None = None
    utr: str = ""
    narration: str = ""
    dup_of: str | None = None


@dataclass
class Dataset:
    entity: dict
    group_entity: dict
    customers: list[Cust]
    invoice_rows: list[dict]
    pays: list[Pay]
    statements: dict[str, str] = field(default_factory=dict)       # account -> csv text
    gateway_csv: str = ""
    remittances: list[dict] = field(default_factory=list)          # {"utr":..,"text":..}
    truth: list[dict] = field(default_factory=list)


def _pan(rng: random.Random, kind="C") -> str:
    L = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(rng.choice(L) for _ in range(3)) + kind + rng.choice(L) + f"{rng.randint(1000, 9999)}" + rng.choice(L)


def _hex(rng, n=64):
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _name_variant(c: Cust, rng: random.Random, style: str | None = None) -> str:
    n = c.name.upper()
    style = style or c.name_style
    if style == "exact":
        return n
    if style == "nosuffix":
        for s in ("PRIVATE LIMITED", "PVT LTD", "LIMITED", "LTD", "LLP"):
            n = n.replace(s, "").strip()
        return n
    if style == "abbrev":
        return (n.replace("INDUSTRIES", "INDS").replace("ENGINEERING", "ENGG").replace("PRIVATE LIMITED", "PVT LTD")
                .replace("AUTO COMPONENTS", "AUTO COMP").replace("LIMITED", "LTD"))
    if style == "truncate":
        return n[:20].strip()
    if style == "typo":
        i = rng.randrange(3, max(len(n) - 3, 4))
        return n[:i] + n[i + 1:] if rng.random() < .5 else n[:i] + n[i] + n[i:]
    return n


def build(seed: int = 42, today: date | None = None, n_customers: int = 28, n_invoices: int = 260) -> Dataset:
    rng = random.Random(seed)
    today = today or date.today()
    entity = {"name": "Meridian Industrial Components Pvt Ltd", "pan": "AABCM4421F", "state": "27"}
    entity["gstin"] = make_gstin("27", entity["pan"])
    group = {"name": "Meridian Exports Private Limited", "pan": "AABCM4422G", "gstin": make_gstin("27", "AABCM4422G")}

    # ---- customers -------------------------------------------------------------------------------
    names: set[str] = set()
    customers: list[Cust] = []
    # two deliberately confusable pairs to exercise ambiguity handling
    fixed = [("Shree Balaji Traders", "Pvt Ltd"), ("Shree Balaji Industries", "Pvt Ltd"), ("Bharat Rail Equipments", "Ltd")]
    pool = [f"{a} {b}" for a in FIRST for b in SECOND]
    rng.shuffle(pool)
    for i in range(n_customers):
        if i < len(fixed):
            base, suf = fixed[i]
        else:
            base, suf = pool[i], rng.choice(SUFFIX)
        name = f"{base} {suf}"
        if name in names:
            continue
        names.add(name)
        psu = base.startswith("Bharat Rail")
        tds_bp = 400 if psu else rng.choices([None, 200, 1000, 100, 10], [.35, .35, .12, .1, .08])[0]
        gst = make_gstin(rng.choice(STATES), _pan(rng))
        bank = rng.choice(BANKS)
        van = f"MERID{rng.randint(100000, 999999)}" if rng.random() < 0.3 else None
        slug = "".join(ch for ch in base.lower() if ch.isalpha())[:10]
        customers.append(Cust(
            code=f"C{1000 + i}", name=name, gstin=gst, pan=gst[2:12], terms=rng.choice([30, 45, 60, 30, 45]), tier=rng.choices("ABC", [.2, .5, .3])[0],
            tds_bp=tds_bp, tds_base="taxable", van=van, bank=bank, vpa=f"{slug}@{rng.choice(['okhdfcbank', 'oksbi', 'ybl', 'icici'])}",
            channel_pref=rng.choices(["NEFT", "RTGS", "IMPS", "UPI", "CHEQUE"], [.5, .25, .08, .1, .07])[0],
            refs_in_narration=rng.choice([.95, .8, .55, .3]), name_style=rng.choice(["exact", "abbrev", "nosuffix", "truncate", "exact"]), psu=psu))

    # ---- invoices ---------------------------------------------------------------------------------
    rows: list[dict] = []
    inv_by_cust: dict[str, list[dict]] = {c.code: [] for c in customers}
    d0 = today - timedelta(days=170)
    seq = 300
    for k in range(n_invoices):
        c = rng.choice(customers)
        taxable = int(10 ** rng.uniform(4.4, 6.4)) // 10 * 10 * 100 + rng.choice([0, 0, 0, 5000, 2500])
        rate = rng.choices([18, 12, 5, 28], [.8, .08, .07, .05])[0]
        gst = pct_of(taxable, rate * 100)
        dt = d0 + timedelta(days=rng.randint(0, 145))
        seq += 1
        number = f"MIC/26-27/{seq:04d}"
        row = {"invoice_no": number, "customer_code": c.code, "customer_name": c.name, "gstin": c.gstin, "pan": c.pan,
               "invoice_date": dt.isoformat(), "due_date": (dt + timedelta(days=c.terms)).isoformat(), "taxable": f"{taxable / 100:.2f}",
               "gst": f"{gst / 100:.2f}", "total": f"{(taxable + gst) / 100:.2f}", "open": f"{(taxable + gst) / 100:.2f}",
               "doc_type": "invoice", "irn": _hex(rng) if rng.random() < .75 else "", "ack_no": "", "eway": "", "po_ref": f"PO-{rng.randint(10000, 99999)}" if rng.random() < .5 else "",
               "currency": "INR", "payment_terms": str(c.terms), "_taxable": taxable, "_gst": gst, "_total": taxable + gst, "_date": dt, "_cust": c.code}
        rows.append(row)
        inv_by_cust[c.code].append(row)
    # credit notes (kept out of inv_by_cust so ordinary scenarios never pick them as invoices)
    cns: list[dict] = []
    for k in range(14):
        base = rng.choice(rows)
        cn_taxable = base["_taxable"] * rng.choice([5, 10, 15]) // 100 // 100 * 100
        cn_gst = pct_of(cn_taxable, 1800)
        cn_total = cn_taxable + cn_gst
        dt = base["_date"] + timedelta(days=rng.randint(5, 25))
        row = {**base, "invoice_no": f"CN/26-27/{k + 1:04d}", "invoice_date": dt.isoformat(), "due_date": dt.isoformat(), "taxable": f"{cn_taxable / 100:.2f}",
               "gst": f"{cn_gst / 100:.2f}", "total": f"{cn_total / 100:.2f}", "open": f"{cn_total / 100:.2f}", "doc_type": "credit_note", "irn": "", "po_ref": "",
               "_taxable": cn_taxable, "_gst": cn_gst, "_total": cn_total, "_date": dt}
        cns.append(row)
    rows += cns
    rows.sort(key=lambda r: (r["_date"], r["invoice_no"]))

    # ---- payments ---------------------------------------------------------------------------------
    cust_by_code = {c.code: c for c in customers}
    available = {code: list(lst) for code, lst in inv_by_cust.items()}
    pays: list[Pay] = []
    used: set[str] = set()

    def due_pay_date(inv, jitter=(-3, 18)):
        return inv["_date"] + timedelta(days=cust_by_code[inv["_cust"]].terms + rng.randint(*jitter))

    def pick(c: Cust | None = None, minimum=1, max_amt=None, min_amt=None, older_than=14):
        order = [c] if c else rng.sample(customers, len(customers))
        for cc in order:
            cand = [r for r in available[cc.code] if r["invoice_no"] not in used and r.get("doc_type") == "invoice" and (today - r["_date"]).days > older_than
                    and (max_amt is None or r["_total"] <= max_amt) and (min_amt is None or r["_total"] >= min_amt)]
            if len(cand) >= minimum:
                return cc, cand
        return None, []

    def refs(c: Cust, invs: list[dict], style: int | None = None) -> str:
        if rng.random() > c.refs_in_narration:
            return ""
        style = rng.randrange(4) if style is None else style
        nums = [r["invoice_no"] for r in invs]
        short = [n.split("/")[-1] for n in nums]
        if style == 0:
            return " ".join(f"INV {n}" for n in nums)
        if style == 1:
            return " ".join(nums)
        if style == 2:
            return "PAYMENT AGAINST " + ",".join(short)
        return "BILL " + " ".join(short)

    def channel_for(c: Cust, amt: int) -> str:
        ch = c.channel_pref
        if ch == "UPI" and amt > 100_000_00:
            ch = "NEFT"
        if ch == "IMPS" and amt > 500_000_00:
            ch = "RTGS"
        if ch == "NEFT" and amt >= 20_000_000_00 and rng.random() < .6:
            ch = "RTGS"
        if ch == "RTGS" and amt < 200_000_00:
            ch = "NEFT"
        return ch

    def add(c, invs, amount, scenario, expect, d=None, ref=None, payer_style=None, exc=None, remittance=None, channel=None,
            account=None, van=False, expect_invoices=None):
        d = d or min(due_pay_date(invs[0]) if invs else today - timedelta(days=rng.randint(3, 12)), today - timedelta(days=1))
        ch = channel or channel_for(c, amount)
        account = account or ("collect" if (van and c.van) else "main")
        p = Pay(d=d, amount=amount, channel=ch, payer=_name_variant(c, rng, payer_style), ref_text=refs(c, invs) if ref is None else ref, cust=c,
                scenario=scenario, expect_invoices=[r["invoice_no"] for r in invs] if expect_invoices is None else expect_invoices,
                expect=expect, expect_exception=exc, account=account, vpa=c.vpa if ch == "UPI" else None,
                van_text=c.van if (van and c.van) else None, remittance=remittance)
        pays.append(p)
        for r in invs:
            used.add(r["invoice_no"])
        return p

    def tds_for(inv, bp, base="taxable", round_rupee=None):
        t = pct_of(inv["_taxable"] if base == "taxable" else inv["_total"], bp)
        if round_rupee if round_rupee is not None else rng.random() < .5:
            t = (t + 50) // 100 * 100
        return t

    N = 190
    mix = [("clean_ref", 24), ("tds_usual", 13), ("bundle_ref", 9), ("bundle_amount", 5), ("single_noref", 7), ("name_variant_ref", 5),
           ("van_noref", 4), ("short_unexplained", 4), ("overpay", 2), ("duplicate_pay", 2), ("unknown_payer", 3), ("remittance_email", 7),
           ("rounding", 3), ("partial_ref", 2), ("tds_unusual", 2), ("gateway", 4), ("interco", 1), ("noise", 1),
           ("ref_typo", 3), ("twin_amount", 3), ("cross_customer_ref", 2), ("name_collision", 3), ("tds_gross_base", 2), ("cn_netting", 3)]
    scen = rng.choices([m[0] for m in mix], [m[1] for m in mix], k=N)

    for s in scen:
        if s in ("clean_ref", "name_variant_ref"):
            c, cand = pick()
            if not c:
                continue
            inv = rng.choice(cand)
            pr = add(c, [inv], inv["_total"], s, "auto", ref=refs(c, [inv]) or f"INV {inv['invoice_no']}",
                     payer_style="typo" if s == "name_variant_ref" else None)
            if not pr.ref_text:
                pr.ref_text = f"INV {inv['invoice_no']}"
        elif s == "tds_usual":
            cs = [c for c in customers if c.tds_bp]
            c, cand = pick(rng.choice(cs))
            if not c:
                continue
            inv = rng.choice(cand)
            t = tds_for(inv, c.tds_bp)
            add(c, [inv], inv["_total"] - t, s, "auto", ref=f"INV {inv['invoice_no']}")
        elif s == "bundle_ref":
            c, cand = pick(minimum=2)
            if not c:
                continue
            invs = rng.sample(cand, rng.randint(2, min(4, len(cand))))
            amt = sum(i["_total"] for i in invs)
            t = sum(tds_for(i, c.tds_bp) for i in invs) if c.tds_bp else 0
            add(c, invs, amt - t, s, "auto", ref=refs(c, invs, 2) or "BILL " + " ".join(i["invoice_no"].split("/")[-1] for i in invs))
        elif s == "bundle_amount":
            c, cand = pick(minimum=3)
            if not c:
                continue
            invs = rng.sample(cand, 2)
            add(c, invs, sum(i["_total"] for i in invs), s, "review", ref="", van=bool(c.van))
        elif s == "single_noref":
            c, cand = pick(minimum=2)
            if not c:
                continue
            inv = rng.choice(cand)
            add(c, [inv], inv["_total"], s, "any_match", ref="PAYMENT", payer_style="exact")
        elif s == "van_noref":
            cs = [c for c in customers if c.van]
            c, cand = pick(rng.choice(cs)) if cs else (None, [])
            if not c:
                continue
            inv = rng.choice(cand)
            add(c, [inv], inv["_total"], s, "any_match", ref="", van=True, payer_style="truncate")
        elif s == "short_unexplained":
            c, cand = pick()
            if not c:
                continue
            inv = rng.choice(cand)
            short = pct_of(inv["_total"], rng.choice([150, 370, 520]))
            add(c, [inv], inv["_total"] - short, s, "review", ref=f"INV {inv['invoice_no']}", exc="short_pay")
        elif s == "overpay":
            c, cand = pick()
            if not c:
                continue
            inv = rng.choice(cand)
            add(c, [inv], inv["_total"] + rng.choice([500000, 1250000, 3000000]), s, "review", ref=f"INV {inv['invoice_no']}", exc="overpayment")
        elif s == "duplicate_pay":
            c, cand = pick()
            if not c:
                continue
            inv = rng.choice(cand)
            first = add(c, [inv], inv["_total"], "duplicate_first", "auto", ref=f"INV {inv['invoice_no']}")
            second = Pay(**{**first.__dict__})
            second.scenario, second.d, second.expect, second.expect_exception, second.expect_invoices = "duplicate_pay", first.d + timedelta(days=rng.randint(2, 6)), "exception", "duplicate_payment", []
            second.dup_of = inv["invoice_no"]
            second.d = min(second.d, today - timedelta(days=1))
            pays.append(second)
        elif s == "unknown_payer":
            amt = rng.randint(25, 900) * 100_00 + rng.randint(0, 99)
            pays.append(Pay(d=today - timedelta(days=rng.randint(1, 25)), amount=amt, channel="NEFT",
                            payer=rng.choice(["GLOBAL TRADELINKS", "ANANYA ENTERPRISES", "R K ASSOCIATES", "PRIME VENTURES"]), ref_text="ADVANCE", cust=None,
                            scenario=s, expect_invoices=[], expect="exception", expect_exception="unidentified_receipt"))
        elif s == "remittance_email":
            c, cand = pick(minimum=2)
            if not c:
                continue
            invs = rng.sample(cand, rng.randint(1, min(3, len(cand))))
            lines, total = [], 0
            for i in invs:
                t = tds_for(i, c.tds_bp) if c.tds_bp else 0
                paid = i["_total"] - t
                total += paid
                lines.append(f"{i['invoice_no']}  {i['_date'].strftime('%d-%b-%Y')}  {i['_total'] / 100:,.2f}"
                             + (f"  TDS {t / 100:,.2f}" if t else "") + f"  Net {paid / 100:,.2f}")
            p = add(c, invs, total, s, "auto", ref="", payer_style="truncate")
            p.remittance = "REMIT"
            p.van_text = None
            p._lines = lines  # type: ignore[attr-defined]
        elif s == "rounding":
            c, cand = pick()
            if not c:
                continue
            inv = rng.choice(cand)
            add(c, [inv], inv["_total"] - rng.choice([50, 100, 25, -50]), s, "auto", ref=f"INV {inv['invoice_no']}")
        elif s == "partial_ref":
            c, cand = pick()
            if not c:
                continue
            inv = rng.choice(cand)
            part = (inv["_total"] * rng.choice([50, 60, 70]) // 100) // 100 * 100
            add(c, [inv], part, s, "review", ref=f"INV {inv['invoice_no']} PART PAYMENT", exc="short_pay")
        elif s == "tds_unusual":
            c, cand = pick()
            if not c or not c.tds_bp:
                cs = [x for x in customers if x.tds_bp]
                c, cand = pick(rng.choice(cs))
            if not c:
                continue
            inv = rng.choice(cand)
            bp = 1000 if c.tds_bp != 1000 else 200
            add(c, [inv], inv["_total"] - tds_for(inv, bp), s, "review", ref=f"INV {inv['invoice_no']}")
        elif s == "gateway":
            c, cand = pick(max_amt=2_00_000_00)
            if not c:
                continue
            inv = rng.choice(cand)
            add(c, [inv], inv["_total"], s, "any_match", ref=inv["invoice_no"], account="gateway", channel="GATEWAY")
        elif s == "interco":
            pays.append(Pay(d=today - timedelta(days=rng.randint(2, 20)), amount=rng.randint(5, 40) * 1_00_000_00, channel="RTGS", payer=group["name"].upper(),
                            ref_text="FUND TRANSFER", cust=None, scenario=s, expect_invoices=[], expect="exception", expect_exception="intercompany"))
        elif s == "ref_typo":
            c, cand = pick(minimum=2)
            if not c:
                continue
            inv, other = rng.sample(cand, 2)
            # customer types the neighbour's invoice number but pays the intended invoice's exact amount
            add(c, [inv], inv["_total"], s, "not_auto", ref=f"INV {other['invoice_no']}", expect_invoices=[inv["invoice_no"]])
        elif s == "twin_amount":
            c, cand = pick(minimum=2)
            if not c:
                continue
            a = rng.choice(cand)
            twin = {**a, "invoice_no": a["invoice_no"].replace("MIC/26-27/", "MIC/26-27/9"), "_date": a["_date"] + timedelta(days=1),
                    "invoice_date": (a["_date"] + timedelta(days=1)).isoformat(), "irn": "", "po_ref": ""}
            rows.append(twin)
            inv_by_cust[c.code].append(twin)
            available[c.code].append(twin)
            used.add(twin["invoice_no"])
            used.add(a["invoice_no"])
            add(c, [], a["_total"], s, "not_auto", ref="", payer_style="exact", expect_invoices=[a["invoice_no"]])
            pays[-1].expect_invoices = ["EITHER:" + a["invoice_no"] + "|" + twin["invoice_no"]]
        elif s == "cross_customer_ref":
            c, cand = pick()
            c2, cand2 = pick(rng.choice([x for x in customers if x is not c]))
            if not c or not c2:
                continue
            inv = rng.choice(cand2)
            add(c, [inv], inv["_total"], s, "not_auto", ref=f"INV {inv['invoice_no']}", expect_invoices=[inv["invoice_no"]])
        elif s == "name_collision":
            twins = [x for x in customers if x.name.startswith("Shree Balaji")]
            if len(twins) < 2:
                continue
            c, cand = pick(rng.choice(twins), minimum=1, older_than=10)
            if not c:
                continue
            inv = rng.choice(cand)
            add(c, [inv], inv["_total"], s, "not_auto", ref="PAYMENT", payer_style="exact", expect_invoices=[inv["invoice_no"]])
            pays[-1].payer = "SHREE BALAJI"
        elif s == "tds_gross_base":
            cs = [x for x in customers if x.tds_bp and x.tds_bp != 10]
            c, cand = pick(rng.choice(cs))
            if not c:
                continue
            inv = rng.choice(cand)
            add(c, [inv], inv["_total"] - tds_for(inv, c.tds_bp, base="gross"), s, "not_auto", ref=f"INV {inv['invoice_no']}")
        elif s == "cn_netting":
            usable = [(r, cust_by_code[r["_cust"]]) for r in cns if r["invoice_no"] not in used]
            done = False
            for cn, c in usable:
                cand = [r for r in available[c.code] if r["invoice_no"] not in used and r["_total"] > cn["_total"] * 2 and r["doc_type"] == "invoice"
                        and (today - r["_date"]).days > 14 and r["_date"] <= cn["_date"] + timedelta(days=60)]
                if cand:
                    inv = rng.choice(cand)
                    add(c, [inv, cn], inv["_total"] - cn["_total"], s, "auto", ref=f"INV {inv['invoice_no']} CN {cn['invoice_no']}")
                    used.add(cn["invoice_no"])
                    done = True
                    break
        elif s == "noise":
            pays.append(Pay(d=today - timedelta(days=rng.randint(2, 40)), amount=rng.randint(100, 9000) * 100, channel="OTHER", payer="", ref_text="",
                            cust=None, scenario="interest", expect_invoices=[], expect="exception", expect_exception="non_ar_credit",
                            narration="INT.PD:01-07-2026 TO 30-09-2026"))

    # ---- narrations & statements -------------------------------------------------------------------
    ds = Dataset(entity=entity, group_entity=group, customers=customers, invoice_rows=rows, pays=pays)
    _render(ds, rng, today)
    return ds


def _utr(rng, bank: str, ch: str, d: date) -> str:
    if ch == "RTGS":
        return f"{bank}R{d:%y%m%d}{rng.randint(10**7, 10**8 - 1)}"[:22].ljust(22, "0")
    return f"{bank}N{d:%y}{d.timetuple().tm_yday:03d}{rng.randint(10**7, 10**8 - 1)}"[:16] + ""


def _render(ds: Dataset, rng: random.Random, today: date) -> None:
    rows_by_acc: dict[str, list[dict]] = {"main": [], "collect": []}
    gw_rows: list[dict] = []
    used_utr: set[str] = set()
    for p in ds.pays:
        c = p.cust
        bank = c.bank[0] if c else rng.choice(BANKS)[0]
        ifsc = c.bank[1] if c else rng.choice(BANKS)[1]
        if p.narration:
            nar = p.narration
        elif p.channel == "GATEWAY":
            nar = ""
        else:
            utr = _utr(rng, bank, p.channel, p.d) if p.channel in ("NEFT", "RTGS") else str(rng.randint(10**11, 10**12 - 1))
            while utr in used_utr:
                utr = utr[:-1] + str(rng.randint(0, 9))
            used_utr.add(utr)
            p.utr = utr
            ref = p.ref_text
            van = f" VAN {p.van_text}" if p.van_text else ""
            if p.channel == "NEFT":
                nar = f"NEFT-{utr}-{p.payer}-{ref}-{ifsc}{van}"
            elif p.channel == "RTGS":
                nar = f"RTGS-{utr}-{p.payer}-{ref}-{ifsc}{van}"
            elif p.channel == "IMPS":
                nar = f"MMT/IMPS/{utr}/{ref}/{p.payer}/{bank}{van}"
            elif p.channel == "UPI":
                nar = f"UPI/{utr}/CR/{p.payer}/{bank}/{p.vpa}/{ref}{van}"
            elif p.channel == "CHEQUE":
                nar = f"CHQ DEP - {rng.randint(100000, 999999)} - {p.payer} {ref}".strip()
            else:
                nar = f"{p.channel}-{p.payer}-{ref}"
        p.narration = nar
        if p.channel == "GATEWAY":
            fee = pct_of(p.amount, 200)
            tax = pct_of(fee, 1800)
            p.narration = f"GATEWAY SETTLEMENT {p.ref_text}"
            gw_rows.append({"Payment ID": f"pay_{rng.randint(10**9, 10**10)}", "Order ID": p.ref_text, "Amount": f"{p.amount / 100:.2f}",
                            "Fee": f"{fee / 100:.2f}", "Tax": f"{tax / 100:.2f}", "Settled At": p.d.strftime("%d/%m/%Y"),
                            "Settlement UTR": f"RZP{p.d:%y%m%d}{rng.randint(1000, 9999)}", "_pay": p})
            continue
        rows_by_acc[p.account if p.account in rows_by_acc else "main"].append({"d": p.d, "narration": nar, "credit": p.amount, "debit": 0, "ref": p.utr})

    # a few non-customer debits: bank charges, vendor payments
    for _ in range(14):
        d = today - timedelta(days=rng.randint(1, 120))
        rows_by_acc["main"].append({"d": d, "narration": rng.choice(["CHARGES FOR NEFT OUTWARD", "GST ON BANK CHARGES", "NEFT-VENDOR PAYMENT-SUPPLIER", "SMS ALERT CHARGES"]),
                                    "credit": 0, "debit": rng.randint(50, 500000) * 100 // 100, "ref": ""})
    # a duplicated statement line (same UTR, appears twice with different running balance) — realistic when exports overlap
    for acc, lst in rows_by_acc.items():
        credits = [r for r in lst if r["credit"] and r["ref"]]
        if credits and acc == "main":
            lst.append(dict(rng.choice(credits)))

    for acc, lst in rows_by_acc.items():
        lst.sort(key=lambda r: r["d"])
        bal = 25_000_000_00
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
        for r in lst:
            bal += r["credit"] - r["debit"]
            w.writerow([r["d"].strftime("%d/%m/%y"), r["narration"], r["ref"], r["d"].strftime("%d/%m/%y"),
                        f"{r['debit'] / 100:.2f}" if r["debit"] else "", f"{r['credit'] / 100:,.2f}" if r["credit"] else "", f"{bal / 100:,.2f}"])
        ds.statements[acc] = out.getvalue()
    if gw_rows:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Payment ID", "Order ID", "Amount", "Fee", "Tax", "Settled At", "Settlement UTR"])
        for r in gw_rows:
            w.writerow([r[k] for k in ["Payment ID", "Order ID", "Amount", "Fee", "Tax", "Settled At", "Settlement UTR"]])
        ds.gateway_csv = out.getvalue()

    for p in ds.pays:
        if p.remittance == "REMIT" and getattr(p, "_lines", None):
            text = (f"From: {_name_variant(p.cust, rng, 'exact')}\nSubject: Payment advice\nDear Sir,\nWe have released payment via {p.channel}. UTR: {p.utr}\n"
                    f"Invoice  Date  Amount  TDS  Net\n" + "\n".join(p._lines) + f"\nTotal payment: {p.amount / 100:,.2f}\n")  # type: ignore[attr-defined]
            ds.remittances.append({"utr": p.utr, "text": text})
        ds.truth.append({"utr": p.utr, "scenario": p.scenario, "amount_minor": p.amount, "date": p.d.isoformat(), "customer": p.cust.code if p.cust else None,
                         "expect": p.expect, "expect_exception": p.expect_exception, "expect_invoices": p.expect_invoices, "channel": p.channel,
                         "narration": p.narration})
