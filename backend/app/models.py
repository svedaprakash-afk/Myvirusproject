"""Relational model. All money is stored as integer minor units (paise/cents).

Sign convention: invoices are positive, credit notes negative. `open_minor` is the
balance not yet applied (applied at match-acceptance time, before ERP posting).
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- enums
class Role(str, enum.Enum):
    admin = "admin"                  # IT / configuration, no financial authority
    ar_analyst = "ar_analyst"
    ar_manager = "ar_manager"
    treasury = "treasury"
    finance_head = "finance_head"    # CFO / Controller
    auditor = "auditor"              # read-only


class TxnStatus(str, enum.Enum):
    new = "new"
    review = "review"                # proposal waiting for a human
    matched = "matched"              # proposal accepted / auto-approved, awaiting posting
    partially_matched = "partially_matched"
    exception = "exception"
    posted = "posted"
    ignored = "ignored"              # debits, non-AR


class ProposalStatus(str, enum.Enum):
    pending_review = "pending_review"
    auto_approved = "auto_approved"
    accepted = "accepted"
    rejected = "rejected"
    superseded = "superseded"
    reversed = "reversed"
    posted = "posted"


class ExceptionType(str, enum.Enum):
    unidentified_receipt = "unidentified_receipt"
    short_pay_tds = "short_pay_tds"
    short_pay = "short_pay"
    overpayment = "overpayment"
    on_account = "on_account"
    duplicate_payment = "duplicate_payment"
    intercompany = "intercompany"
    non_ar_credit = "non_ar_credit"
    disputed_invoice = "disputed_invoice"
    deduction = "deduction"


class ExceptionStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    pending_approval = "pending_approval"
    resolved = "resolved"
    written_off = "written_off"


class ApprovalKind(str, enum.Enum):
    write_off = "write_off"
    adjustment = "adjustment"
    manual_match = "manual_match"
    batch_release = "batch_release"
    refund = "refund"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class BatchStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    exported = "exported"
    acknowledged = "acknowledged"
    failed = "failed"


# ---------------------------------------------------------------- identity
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(Enum(Role))
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    entity_ids: Mapped[list] = mapped_column(JSON, default=list)  # empty => all entities
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- org structure
class LegalEntity(Base):
    __tablename__ = "legal_entities"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    gstin: Mapped[str | None] = mapped_column(String(15))
    pan: Mapped[str | None] = mapped_column(String(10))
    base_currency: Mapped[str] = mapped_column(String(3), default="INR")
    # Matching / control configuration (JSON so it can evolve without migrations)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # Ledger codes used when building canonical journals
    ledgers: Mapped[dict] = mapped_column(JSON, default=dict)
    bank_accounts: Mapped[list["BankAccount"]] = relationship(back_populates="entity")


DEFAULT_ENTITY_CONFIG = {
    "auto_approve_threshold": 0.90,     # model confidence needed for straight-through
    "review_threshold": 0.45,           # below this -> exception rather than suggestion
    "auto_approve_max_minor": 50_00_00_000,  # ₹50 lakh: above this a human must always look
    "auto_approve_enabled": True,        # False = suggest-only mode (recommended for the first weeks of a rollout)
    "auto_approve_amount_only": False,   # allow auto-approval on exact amount without any invoice reference
    "auto_approve_cross_currency": True,   # auto-approve cross-currency ONLY when the payer stated the foreign amount (estimates never auto)
    "fx_spread_bp": 150,                   # max deviation between bank-credited INR and invoice value at the rate table (1.5%)
    "foreign_bank_charge_max_base_minor": 500_000,  # intermediary bank charges tolerated on a foreign receipt, in BASE currency (₹5,000 ≈ $60)
    "straight_through_release": False,  # auto-release posting batches without a human
    "sla_hours": 48,
    "max_subset_size": 8,
    # per-customer graduation to straight-through (off by default: existing entities behave as before)
    "trust_graduation": {"enabled": False, "graduate_after": 5, "demote_on_correction": True},
    # Section 194Q (buyer deducts on purchases of goods above a yearly threshold). Statutory values change with the Finance Act: confirm with your tax team.
    "tds_194q_threshold_minor": 50_00_000_00,   # ₹50 lakh of purchases from the seller in the financial year
    "tds_194q_rate_bp": 10,
    "e_invoice_required": False,      # e-invoicing applies above the aggregate-turnover threshold in force: set per entity
    "gstr1_frequency": "monthly",     # monthly | quarterly (QRMP)
    "period_lock_policy": "redate",   # redate: late items in a closed period post on the first open day | block: refuse the batch
}
DEFAULT_LEDGERS = {
    "ar_control": "1200-AR",
    "tds_receivable": "1350-TDS-RECV",
    "bank_charges": "5400-BANK-CHG",
    "gst_input_on_fees": "1360-GST-ITC",
    "gst_tds_receivable": "1355-GST-TDS-RECV",
    "write_off": "5900-WRITEOFF",
    "unapplied_cash": "2150-UNAPPLIED",
    "intercompany": "1500-INTERCO",
    "fx_gain_loss": "4950-FX-GAINLOSS",
}


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"))
    bank_name: Mapped[str] = mapped_column(String(100))
    account_no: Mapped[str] = mapped_column(String(34))
    ifsc: Mapped[str | None] = mapped_column(String(11))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    ledger_code: Mapped[str] = mapped_column(String(50), default="1100-BANK")
    van_prefix: Mapped[str | None] = mapped_column(String(20))  # virtual account prefix
    is_gateway: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bank-agnostic onboarding: how to read this bank's files (profile) and how to fetch them (feed). Secrets are never stored,
    # only the NAMES of environment variables that hold them.
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    feed: Mapped[dict] = mapped_column(JSON, default=dict)
    entity: Mapped[LegalEntity] = relationship(back_populates="bank_accounts")
    __table_args__ = (UniqueConstraint("entity_id", "account_no"),)


# ---------------------------------------------------------------- AR master data
class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(250))
    gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    pan: Mapped[str | None] = mapped_column(String(10), index=True)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    tier: Mapped[str] = mapped_column(String(10), default="B")  # A/B/C — drives exception priority
    tds_rate_bp: Mapped[int | None] = mapped_column(Integer)   # usual TDS rate in basis points
    tds_section: Mapped[str | None] = mapped_column(String(20))
    tds_base: Mapped[str] = mapped_column(String(10), default="taxable")  # taxable | gross
    virtual_account_no: Mapped[str | None] = mapped_column(String(34), index=True)
    interco_entity_id: Mapped[int | None] = mapped_column(ForeignKey("legal_entities.id"))
    ar_ledger: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(200))
    tan: Mapped[str | None] = mapped_column(String(10), index=True)      # the customer's TAN as a TDS deductor: how Form 26AS names them
    __table_args__ = (UniqueConstraint("entity_id", "code"),)


class CustomerIdentifier(Base):
    """Learned or declared payer identifiers: names, accounts, VPAs."""
    __tablename__ = "customer_identifiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # name | account | vpa
    value: Mapped[str] = mapped_column(String(200), index=True)  # normalised
    hits: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(20), default="learned")
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("entity_id", "customer_id", "kind", "value"),)


class CustomerProfile(Base):
    """Behavioural statistics learnt from accepted matches."""
    __tablename__ = "customer_profiles"
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    payments: Mapped[int] = mapped_column(Integer, default=0)
    multi_invoice_payments: Mapped[int] = mapped_column(Integer, default=0)
    tds_payments: Mapped[int] = mapped_column(Integer, default=0)
    ref_in_narration: Mapped[int] = mapped_column(Integer, default=0)
    avg_days_to_pay: Mapped[float] = mapped_column(Float, default=0.0)
    avg_days_late: Mapped[float] = mapped_column(Float, default=0.0)
    last_paid: Mapped[date | None] = mapped_column(Date)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    number: Mapped[str] = mapped_column(String(60))
    doc_type: Mapped[str] = mapped_column(String(12), default="invoice")  # invoice|credit_note|debit_note
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    taxable_minor: Mapped[int] = mapped_column(BigInteger)
    gst_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    total_minor: Mapped[int] = mapped_column(BigInteger)
    open_minor: Mapped[int] = mapped_column(BigInteger)
    irn: Mapped[str | None] = mapped_column(String(64), index=True)       # e-invoice IRN
    ack_no: Mapped[str | None] = mapped_column(String(30))
    eway_bill_no: Mapped[str | None] = mapped_column(String(20))
    po_ref: Mapped[str | None] = mapped_column(String(60))
    disputed: Mapped[bool] = mapped_column(Boolean, default=False)
    erp_ref: Mapped[str | None] = mapped_column(String(80))
    booking_rate_micro: Mapped[int] = mapped_column(BigInteger, default=1_000_000)  # base currency per 1 unit of invoice currency, x1e6
    customer: Mapped[Customer] = relationship()
    __table_args__ = (UniqueConstraint("entity_id", "number"), Index("ix_inv_open", "entity_id", "customer_id", "open_minor"))

    @property
    def status(self) -> str:
        if self.total_minor == 0 or self.open_minor == 0:
            return "paid"
        if self.disputed:
            return "disputed"
        if abs(self.open_minor) < abs(self.total_minor):
            return "partial"
        return "open"


# ---------------------------------------------------------------- inbound cash
class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"))
    source: Mapped[str] = mapped_column(String(30), default="statement")
    source_batch: Mapped[str | None] = mapped_column(String(60))
    txn_date: Mapped[date] = mapped_column(Date, index=True)
    value_date: Mapped[date | None] = mapped_column(Date)
    direction: Mapped[str] = mapped_column(String(6), default="credit")
    amount_minor: Mapped[int] = mapped_column(BigInteger)       # credited to bank
    fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)  # gateway fee + GST on fee
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    channel: Mapped[str] = mapped_column(String(12), default="OTHER")
    utr: Mapped[str | None] = mapped_column(String(40), index=True)
    narration: Mapped[str] = mapped_column(Text, default="")
    payer_name: Mapped[str | None] = mapped_column(String(250))
    payer_account: Mapped[str | None] = mapped_column(String(34))
    payer_ifsc: Mapped[str | None] = mapped_column(String(11))
    payer_vpa: Mapped[str | None] = mapped_column(String(100))
    bank_ref: Mapped[str | None] = mapped_column(String(60))
    balance_minor: Mapped[int | None] = mapped_column(BigInteger)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    fx_ccy: Mapped[str | None] = mapped_column(String(3))            # foreign currency the payer actually sent (inward remittance)
    fx_minor: Mapped[int | None] = mapped_column(BigInteger)
    fx_rate_micro: Mapped[int | None] = mapped_column(BigInteger)    # base per 1 unit of txn.currency when the bank states it
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[TxnStatus] = mapped_column(Enum(TxnStatus), default=TxnStatus.new, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    customer_confidence: Mapped[float | None] = mapped_column(Float)
    remittance_id: Mapped[int | None] = mapped_column(ForeignKey("remittance_documents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime)

    bank_account: Mapped[BankAccount] = relationship()
    customer: Mapped[Customer | None] = relationship()

    @property
    def receivable_minor(self) -> int:
        """Cash the customer actually paid (bank credit plus gateway deductions)."""
        return self.amount_minor + self.fee_minor


class RemittanceDocument(Base):
    __tablename__ = "remittance_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    filename: Mapped[str | None] = mapped_column(String(250))
    raw_text: Mapped[str] = mapped_column(Text, default="")
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    extractor: Mapped[str] = mapped_column(String(30), default="rules")
    source_ref: Mapped[str | None] = mapped_column(String(200), index=True)   # e.g. email Message-ID, for idempotent polling
    sender: Mapped[str | None] = mapped_column(String(250))
    auth_result: Mapped[str | None] = mapped_column(String(60))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    linked_txn_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- matching
class MatchProposal(Base):
    __tablename__ = "match_proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("payment_transactions.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[ProposalStatus] = mapped_column(Enum(ProposalStatus), index=True)
    strategy: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(20))
    shadow_confidence: Mapped[float | None] = mapped_column(Float)      # what the shadow model would have said (evaluation only)
    shadow_model_version: Mapped[str | None] = mapped_column(String(20))
    explanation: Mapped[dict] = mapped_column(JSON, default=dict)
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    guardrails: Mapped[list] = mapped_column(JSON, default=list)  # reasons auto-approval was blocked
    applied_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    tds_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    writeoff_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    unapplied_minor: Mapped[int] = mapped_column(BigInteger, default=0)   # overpay / on-account
    short_minor: Mapped[int] = mapped_column(BigInteger, default=0)       # unexplained shortfall
    settle_currency: Mapped[str | None] = mapped_column(String(3))        # invoice currency when it differs from the receipt's
    settle_rate_micro: Mapped[int | None] = mapped_column(BigInteger)     # implied base per unit of settle_currency
    writeoff_kind: Mapped[str] = mapped_column(String(20), default="tolerance")  # tolerance | bank_charge
    created_by: Mapped[str] = mapped_column(String(60), default="engine")
    auto: Mapped[bool] = mapped_column(Boolean, default=False)   # applied straight-through, no human decision
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("posting_batches.id"))

    allocations: Mapped[list["MatchAllocation"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    txn: Mapped[PaymentTransaction] = relationship()
    customer: Mapped[Customer | None] = relationship()


class MatchAllocation(Base):
    __tablename__ = "match_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("match_proposals.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    applied_minor: Mapped[int] = mapped_column(BigInteger)       # cash applied
    tds_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    writeoff_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    tds_section: Mapped[str | None] = mapped_column(String(20))
    tds_rate_bp: Mapped[int | None] = mapped_column(Integer)
    reference_hit: Mapped[str | None] = mapped_column(String(20))  # exact|serial|remittance|irn|None
    proposal: Mapped[MatchProposal] = relationship(back_populates="allocations")
    invoice: Mapped[Invoice] = relationship()

    @property
    def cleared_minor(self) -> int:
        return self.applied_minor + self.tds_minor + self.writeoff_minor


class ScoringModel(Base):
    """Versioned logistic-regression weights; one active per entity (or global when entity_id NULL)."""
    __tablename__ = "scoring_models"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(20), unique=True)
    weights: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)   # scored alongside the active model on live traffic; never decides anything
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MatchFeedback(Base):
    __tablename__ = "match_feedback"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("match_proposals.id"))
    label: Mapped[int] = mapped_column(Integer)  # 1 = correct candidate, 0 = incorrect
    features: Mapped[dict] = mapped_column(JSON)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ToleranceRule(Base):
    __tablename__ = "tolerance_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    currency: Mapped[str | None] = mapped_column(String(3))
    abs_minor: Mapped[int] = mapped_column(BigInteger, default=100)  # ₹1
    pct_bp: Mapped[int] = mapped_column(Integer, default=0)          # 1bp = 0.01%


# ---------------------------------------------------------------- exceptions & approvals
class ExceptionCase(Base):
    __tablename__ = "exception_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    type: Mapped[ExceptionType] = mapped_column(Enum(ExceptionType), index=True)
    status: Mapped[ExceptionStatus] = mapped_column(Enum(ExceptionStatus), default=ExceptionStatus.open, index=True)
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("payment_transactions.id"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("match_proposals.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text, default="")
    suggestions: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[list["ExceptionNote"]] = relationship(cascade="all, delete-orphan", order_by="ExceptionNote.id")
    customer: Mapped[Customer | None] = relationship()
    txn: Mapped["PaymentTransaction | None"] = relationship(foreign_keys=[txn_id])

    @property
    def txn_currency(self) -> str | None:
        return self.txn.currency if self.txn else None


class ExceptionNote(Base):
    __tablename__ = "exception_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("exception_cases.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApprovalPolicy(Base):
    """Approval hierarchy: first row whose [min,max) contains the amount decides the approver role."""
    __tablename__ = "approval_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    kind: Mapped[ApprovalKind] = mapped_column(Enum(ApprovalKind))
    min_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    max_minor: Mapped[int | None] = mapped_column(BigInteger)
    approver_role: Mapped[Role] = mapped_column(Enum(Role))


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    kind: Mapped[ApprovalKind] = mapped_column(Enum(ApprovalKind))
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.pending, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    required_role: Mapped[Role] = mapped_column(Enum(Role))
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, default="")
    decision_note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ref_type: Mapped[str | None] = mapped_column(String(30))
    ref_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class Adjustment(Base):
    """A fully-specified two-legged journal that is not part of a cash application:
    write-offs, non-AR receipts (interest, bank-charge reversals), intercompany receipts."""
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))   # write_off | non_ar_receipt | interco_receipt
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("payment_transactions.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    exception_id: Mapped[int | None] = mapped_column(ForeignKey("exception_cases.id"))
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approval_requests.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    debit_account: Mapped[str] = mapped_column(String(60))
    credit_account: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str] = mapped_column(String(300), default="")
    posting_date: Mapped[date] = mapped_column(Date)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("posting_batches.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)


class ExchangeRate(Base):
    """Daily rates, base currency per 1 unit of `currency`, x1e6 (e.g. USD 83.4521 INR -> 83452100)."""
    __tablename__ = "exchange_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), index=True)
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    rate_micro: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    __table_args__ = (UniqueConstraint("currency", "rate_date"),)


class CustomerTrust(Base):
    """Per-customer straight-through state. A customer starts in `learning` (every match needs a human) and graduates to `auto`
    after N consecutive human-confirmed matches with no correction; any rejection, correction or reversed auto-match demotes."""
    __tablename__ = "customer_trust"
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    mode: Mapped[str] = mapped_column(String(14), default="learning")   # learning | auto | suggest_only (pinned by a human)
    clean_streak: Mapped[int] = mapped_column(Integer, default=0)
    confirmed: Mapped[int] = mapped_column(Integer, default=0)
    corrections: Mapped[int] = mapped_column(Integer, default=0)
    threshold_override: Mapped[float | None] = mapped_column(Float)      # can only RAISE the entity's auto-approve threshold
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)         # a human set the mode: automation never changes it
    note: Mapped[str] = mapped_column(String(300), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class OnAccountBalance(Base):
    """Cash received and posted to the unapplied-cash ledger that is still available to settle future invoices."""
    __tablename__ = "on_account_balances"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("payment_transactions.id"), index=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("match_proposals.id"), unique=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    original_minor: Mapped[int] = mapped_column(BigInteger)
    remaining_minor: Mapped[int] = mapped_column(BigInteger)
    reference: Mapped[str | None] = mapped_column(String(60))       # PO / proforma the payer quoted, used to target the right invoice
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | used | refunded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdvanceApplication(Base):
    """A proposal (and later the record) of settling one invoice from an on-account balance. Posts as Dr unapplied cash / Cr AR."""
    __tablename__ = "advance_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    balance_id: Mapped[int] = mapped_column(ForeignKey("on_account_balances.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    basis: Mapped[str] = mapped_column(String(20))                    # reference | oldest_first
    status: Mapped[str] = mapped_column(String(10), default="proposed")  # proposed | applied | rejected
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    adjustment_id: Mapped[int | None] = mapped_column(ForeignKey("adjustments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("balance_id", "invoice_id"),)


class JobRun(Base):
    """One row per (job, time-slot). The unique constraint is the lock: two workers cannot run the same slot."""
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), index=True)
    slot: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(12), default="running")   # running | ok | failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("name", "slot"),)


# ---------------------------------------------------------------- tax
class TdsEntry(Base):
    """TDS suffered on receipts: drives the 'awaiting Form 16A / 26AS' worklist."""
    __tablename__ = "tds_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    proposal_id: Mapped[int] = mapped_column(ForeignKey("match_proposals.id"))
    txn_date: Mapped[date] = mapped_column(Date)
    fy: Mapped[str] = mapped_column(String(9))         # 2025-26
    quarter: Mapped[str] = mapped_column(String(2))    # Q1..Q4
    section: Mapped[str | None] = mapped_column(String(20))
    rate_bp: Mapped[int | None] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(12), default="income_tax")   # income_tax (Form 16A / 26AS) | gst (GST-TDS s.51: credit via GSTR-7 / cash ledger)
    certificate_received: Mapped[bool] = mapped_column(Boolean, default=False)
    in_26as: Mapped[bool] = mapped_column(Boolean, default=False)
    reversed: Mapped[bool] = mapped_column(Boolean, default=False)
    customer: Mapped[Customer] = relationship()
    invoice: Mapped[Invoice] = relationship()


class Form26asEntry(Base):
    """One deduction line reported against our PAN in Form 26AS (or the AIS TDS section): what the deductor's TDS return says they paid in."""
    __tablename__ = "form26as_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    source: Mapped[str] = mapped_column(String(6), default="26AS")      # 26AS | AIS
    fy: Mapped[str] = mapped_column(String(9), index=True)
    quarter: Mapped[str] = mapped_column(String(2))
    deductor_tan: Mapped[str] = mapped_column(String(10), index=True)
    deductor_name: Mapped[str] = mapped_column(String(250))
    section: Mapped[str | None] = mapped_column(String(20))
    txn_date: Mapped[date] = mapped_column(Date)                         # date of payment / credit as reported by the deductor
    booking_date: Mapped[date | None] = mapped_column(Date)
    booking_status: Mapped[str | None] = mapped_column(String(2))        # F final | P provisional | U unmatched | O overbooked (26AS legend)
    amount_paid_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    tax_deducted_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    tds_deposited_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("entity_id", "source", "dedupe_key"),)


class GstDocument(Base):
    """A document as the GST system holds it: an outward invoice or credit note reported in a GSTR-1 filing, or an e-invoice on the IRP register."""
    __tablename__ = "gst_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    source: Mapped[str] = mapped_column(String(6))                  # GSTR1 | IRP
    doc_type: Mapped[str] = mapped_column(String(3), default="INV")  # INV | CRN | DBN
    doc_no: Mapped[str] = mapped_column(String(40))
    doc_no_norm: Mapped[str] = mapped_column(String(40), index=True)
    doc_date: Mapped[date] = mapped_column(Date)
    party_gstin: Mapped[str | None] = mapped_column(String(15))
    taxable_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    irn: Mapped[str | None] = mapped_column(String(64))
    ack_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(3), default="ACT")   # ACT | CNL (cancelled IRN)
    period: Mapped[str | None] = mapped_column(String(6))           # GSTR-1 filing period MMYYYY
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("entity_id", "source", "doc_type", "doc_no_norm"),)


class PeriodLock(Base):
    """Books are closed through `locked_through`. Journals dated on or before it are re-dated to the first open day (or refused, per policy)."""
    __tablename__ = "period_locks"
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), primary_key=True)
    locked_through: Mapped[date] = mapped_column(Date)
    locked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    locked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    note: Mapped[str] = mapped_column(String(300), default="")


# ---------------------------------------------------------------- posting
class PostingBatch(Base):
    __tablename__ = "posting_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("legal_entities.id"), index=True)
    status: Mapped[BatchStatus] = mapped_column(Enum(BatchStatus), default=BatchStatus.draft)
    connector: Mapped[str | None] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime)
    checksum: Mapped[str | None] = mapped_column(String(64))
    export_key: Mapped[str | None] = mapped_column(String(500))     # where the exported file is stored (object-storage key / relative path)
    error: Mapped[str | None] = mapped_column(Text)
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    lines: Mapped[list["PostingLine"]] = relationship(cascade="all, delete-orphan", order_by="PostingLine.id")


class PostingLine(Base):
    __tablename__ = "posting_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("posting_batches.id"), index=True)
    voucher_no: Mapped[str] = mapped_column(String(40), index=True)   # one voucher per bank txn
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("payment_transactions.id"))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("match_proposals.id"))
    adjustment_id: Mapped[int | None] = mapped_column(ForeignKey("adjustments.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    line_type: Mapped[str] = mapped_column(String(24))
    account: Mapped[str] = mapped_column(String(60))
    debit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    base_debit_minor: Mapped[int] = mapped_column(BigInteger, default=0)    # same line in the entity's base currency
    base_credit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    rate_micro: Mapped[int | None] = mapped_column(BigInteger)
    posting_date: Mapped[date] = mapped_column(Date)
    narration: Mapped[str] = mapped_column(String(300), default="")
    erp_doc_no: Mapped[str | None] = mapped_column(String(80))


# ---------------------------------------------------------------- audit
class AuditLog(Base):
    """Append-only, hash-chained. Each row commits to the previous row's hash."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(120))
    actor_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(60), index=True)
    object_type: Mapped[str] = mapped_column(String(40), index=True)
    object_id: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
