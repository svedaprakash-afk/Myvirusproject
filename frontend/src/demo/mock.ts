/**
 * Backend-less demo transport. Serves a frozen snapshot of the *synthetic* Meridian tenant (see backend/app/seed/export_snapshot.py)
 * through the same paths the real API exposes, so the production UI runs unchanged. Reviewer/approval actions mutate an in-memory copy
 * only; everything else is refused with a clear message. Nothing is persisted and nothing leaves the page.
 */
import { ApiError } from "../api";
import snapshot from "./snapshot.json";

type Row = Record<string, any>;
const S: any = JSON.parse(JSON.stringify(snapshot));

const USERS: Record<string, { id: number; name: string; role: string }> = {
  "admin@meridian.example": { id: 1, name: "Isha Admin", role: "admin" },
  "analyst@meridian.example": { id: 2, name: "Arjun Analyst", role: "ar_analyst" },
  "analyst2@meridian.example": { id: 3, name: "Meera Analyst", role: "ar_analyst" },
  "manager@meridian.example": { id: 4, name: "Rohan AR Manager", role: "ar_manager" },
  "treasury@meridian.example": { id: 5, name: "Tara Treasury", role: "treasury" },
  "cfo@meridian.example": { id: 6, name: "Vikram CFO", role: "finance_head" },
  "auditor@meridian.example": { id: 7, name: "Anita Auditor", role: "auditor" },
};
const RANK: Record<string, number> = { auditor: 0, ar_analyst: 1, ar_manager: 2, treasury: 2, finance_head: 3, admin: 0 };
const APPROVERS = ["ar_manager", "treasury", "finance_head"];

let current: string | null = null;
const me = () => (current ? { email: current, ...USERS[current] } : null);
const pe = (eid: number): Row => S.per_entity[String(eid)] ?? S.per_entity["1"];
const ci = (s: unknown) => String(s ?? "").toLowerCase();
const num = (v: string | null, d: number) => (v === null || v === "" ? d : Number(v));
const list = (v: string | null) => (v ? v.split(",").filter(Boolean) : []);
const readOnly = (what: string) => new ApiError(409, `Read-only demo: "${what}" is switched off. This page runs on a frozen snapshot of synthetic data with no backend.`);
const notFound = () => new ApiError(404, "Not found in the demo snapshot");
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));
const page = <T,>(rows: T[], q: URLSearchParams, dflt = 100) => rows.slice(num(q.get("offset"), 0), num(q.get("offset"), 0) + Math.min(num(q.get("limit"), dflt), 500));
const now = () => new Date().toISOString();

function audit(eid: number, action: string, type: string, id: number | string, detail: Row) {
  const a = pe(eid).audit as Row[];
  const seq = (a[0]?.seq ?? 0) + 1;
  a.unshift({ seq, ts: now(), actor: me()?.name ?? "demo", action, object_type: type, object_id: String(id), entity_id: eid, detail, hash: "demo-" + seq, prev_hash: a[0]?.hash ?? "" });
}

function recount(eid: number) {
  const p = pe(eid), s = p.summary;
  s.queues.review = (p.proposals as Row[]).filter((x) => x.status === "pending_review").length;
  s.queues.exceptions = (p.exceptions as Row[]).filter((x) => ["open", "in_progress", "pending_approval"].includes(x.status)).length;
  s.queues.approvals_pending = (p.approvals as Row[]).filter((x) => x.status === "pending").length;
}

function setTxn(eid: number, tid: number, status: string) {
  const p = pe(eid);
  for (const t of [...(p.transactions as Row[]), p.txn_detail[tid]].filter(Boolean)) if (t.id === tid) t.status = status;
}

function canDecide(a: Row) {
  const u = me();
  return !!u && APPROVERS.includes(u.role) && a.requested_by !== u.id && RANK[u.role] >= RANK[a.required_role];
}

// ---------------------------------------------------------------------------------------------------------- GET
function get(path: string, q: URLSearchParams): unknown {
  let m: RegExpMatchArray | null;
  const eid = num(q.get("entity_id"), S.entities[0].id);
  const p = pe(eid);
  if (path === "/auth/me") { const u = me(); if (!u) throw new ApiError(401, "Not signed in"); return { id: u.id, name: u.name, email: u.email, role: u.role }; }
  if (path === "/entities") return S.entities;
  if (path === "/connectors") return q.get("entity_id") ? p.connectors : S.connectors;
  if (path === "/auth/sso/config") return S.sso_config;
  if (path === "/advances") return p.advances;
  if ((m = path.match(/^\/customers\/(\d+)\/trust$/))) { const t = p.trust[m[1]]; if (!t) throw notFound(); return t; }
  if (path === "/models/health") return p.model_health;
  if (path === "/models/shadow") return p.model_shadow;
  if (path === "/reports/ageing") return p.ageing;
  if (path === "/reports/tds-register") {
    const fy = q.get("fy"), d = clone(p.tds_register), rows = fy ? d.rows.filter((r: Row) => r.fy === fy) : d.rows;
    const sum = (k: string) => rows.reduce((n: number, r: Row) => n + r[k], 0);
    return { ...d, rows, totals: Object.fromEntries(["amount_minor", "certificate_minor", "pending_minor", "in_26as_minor", "not_in_26as_minor"].map((k) => [k, sum(k)])),
      overdue_minor: rows.filter((r: Row) => r.overdue).reduce((n: number, r: Row) => n + r.pending_minor, 0), overdue_customers: new Set(rows.filter((r: Row) => r.overdue).map((r: Row) => r.customer_id)).size };
  }
  if (path === "/tax/26as/reconciliation") {
    const d = clone(p.recon_26as);
    return q.get("fy") === d.fy && (q.get("source") ?? "26AS") === d.source ? d : { ...d, fy: q.get("fy"), source: q.get("source"), rows: [], unmapped: [], summary: Object.fromEntries(Object.keys(d.summary).map((k) => [k, 0])), ours_minor: 0, reported_minor: 0, at_risk_minor: 0, unclaimed_minor: 0 };
  }
  if (path === "/tax/gst/crosscheck") return p.gst_check;
  if (path === "/periods/lock") return p.period_lock;
  if ((m = path.match(/^\/entities\/(\d+)\/notifications$/))) return pe(+m[1]).notif_cfg;
  if (path === "/notifications") return p.notif_list;
  if (path === "/posting/unconfirmed") return p.unconfirmed;
  if (path === "/bank-templates") return S.bank_templates;
  if (path === "/models") return S.models;
  if (path === "/fx-rates/feed") return S.fx_feed;
  if (path === "/fx-rates") { const c = (q.get("currency") ?? "").toUpperCase(); return (S.fx_rates as Row[]).filter((r) => !c || r.currency === c).slice(0, num(q.get("limit"), 100)); }
  if (path === "/storage") return S.storage;
  if (path === "/audit/verify") return S.audit_verify;
  if (path === "/analytics/summary") { recount(eid); return p.summary; }
  if (path === "/analytics/trend") return p.trend;
  if (path === "/analytics/cash-position") return p.cash;
  if (path === "/analytics/forecast") return p.forecast;
  if ((m = path.match(/^\/entities\/(\d+)\/config$/))) return pe(+m[1]).config;
  if ((m = path.match(/^\/entities\/(\d+)\/integrations$/))) return pe(+m[1]).integrations;
  if ((m = path.match(/^\/entities\/(\d+)\/bank-accounts$/))) return pe(+m[1]).banks;
  if ((m = path.match(/^\/bank-accounts\/(\d+)\/setup$/))) { for (const e of Object.values<Row>(S.per_entity)) if (e.bank_setup[m[1]]) return e.bank_setup[m[1]]; throw notFound(); }
  if (path === "/remittances") return p.remittances;

  if (path === "/proposals") {
    const st = list(q.get("status") ?? "pending_review"), lo = num(q.get("min_conf"), 0), hi = num(q.get("max_conf"), 1);
    const rows = (p.proposals as Row[]).filter((x) => st.includes(x.status) && x.confidence >= lo && x.confidence <= hi).sort((a, b) => b.confidence - a.confidence || a.id - b.id);
    return { total: rows.length, items: page(rows, q) };
  }
  if ((m = path.match(/^\/proposals\/(\d+)$/))) { const x = (p.proposals as Row[]).find((r) => r.id === +m![1]); if (!x) throw notFound(); return x; }

  if (path === "/transactions") {
    const dir = q.get("direction") ?? "credit", st = list(q.get("status")), s = ci(q.get("q"));
    const all = p.transactions as Row[];
    const rows = all.filter((t) => (!dir || t.direction === dir) && (!st.length || st.includes(t.status)) &&
      (!s || ci(t.narration).includes(s) || ci(t.utr).includes(s) || ci(t.payer_name).includes(s)) &&
      (!q.get("date_from") || t.txn_date >= q.get("date_from")!) && (!q.get("date_to") || t.txn_date <= q.get("date_to")!))
      .sort((a, b) => (a.txn_date < b.txn_date ? 1 : a.txn_date > b.txn_date ? -1 : b.id - a.id));
    const counts: Record<string, number> = {};
    for (const t of all) if (t.direction === "credit") counts[t.status] = (counts[t.status] ?? 0) + 1;
    return { total: rows.length, items: page(rows, q), status_counts: counts };
  }
  if ((m = path.match(/^\/transactions\/(\d+)$/))) { const x = p.txn_detail[m[1]] ?? Object.values<Row>(S.per_entity).map((e) => e.txn_detail[m![1]]).find(Boolean); if (!x) throw notFound(); return x; }

  if (path === "/exceptions") {
    const st = list(q.get("status") ?? "open,in_progress,pending_approval"), ty = list(q.get("type")), as = q.get("assignee_id");
    const rows = (p.exceptions as Row[]).filter((x) => (!st.length || st.includes(x.status)) && (!ty.length || ty.includes(x.type)) && (!as || String(x.assignee_id) === as))
      .sort((a, b) => b.priority - a.priority || a.id - b.id);
    return { total: rows.length, items: page(rows, q) };
  }
  if ((m = path.match(/^\/exceptions\/(\d+)$/))) { const x = p.exception_detail[m[1]] ?? Object.values<Row>(S.per_entity).map((e) => e.exception_detail[m![1]]).find(Boolean); if (!x) throw notFound(); return x; }

  if (path === "/approvals") {
    const st = list(q.get("status") ?? "pending");
    return (p.approvals as Row[]).filter((a) => st.includes(a.status)).sort((a, b) => b.id - a.id).map((a) => ({ ...a, can_decide: a.status === "pending" && canDecide(a) }));
  }
  if (path === "/tds") {
    const c = q.get("certificate"), fy = q.get("fy");
    const rows = (p.tds as Row[]).filter((t) => !t.reversed && (c === "pending" ? !t.certificate_received : c === "received" ? t.certificate_received : true) && (!fy || t.fy === fy))
      .sort((a, b) => (a.txn_date < b.txn_date ? 1 : -1));
    return { items: page(rows, q, 200), total_amount_minor: rows.reduce((n, t) => n + t.amount_minor, 0) };
  }
  if (path === "/posting/batches") return p.batches;
  if ((m = path.match(/^\/posting\/batches\/(\d+)$/))) { const x = p.batch_detail[m[1]]; if (!x) throw notFound(); return x; }

  if (path === "/customers") {
    const s = ci(q.get("q"));
    const rows = (p.customers as Row[]).filter((c) => !s || ci(c.name).includes(s) || ci(c.code).includes(s) || ci(c.gstin).includes(s)).sort((a, b) => a.name.localeCompare(b.name));
    return { total: rows.length, items: page(rows, q, 50) };
  }
  if ((m = path.match(/^\/customers\/(\d+)$/))) { const x = p.customer_detail[m[1]]; if (!x) throw notFound(); return x; }
  if (path === "/invoices") {
    const st = q.get("status"), cid = q.get("customer_id"), s = ci(q.get("q"));
    const rows = (p.invoices as Row[]).filter((i) => (!cid || String(i.customer?.id ?? i.customer_id) === cid) && (!s || ci(i.number).includes(s)) &&
      (st === "open" ? i.open_minor !== 0 : st === "paid" ? i.open_minor === 0 : st === "disputed" ? !!i.disputed : true))
      .sort((a, b) => (a.due_date < b.due_date ? -1 : a.due_date > b.due_date ? 1 : a.id - b.id));
    return { total: rows.length, items: page(rows, q) };
  }
  if (path === "/audit") {
    const ot = q.get("object_type"), oid = q.get("object_id"), ac = q.get("action"), actor = ci(q.get("actor"));
    const rows = (p.audit as Row[]).filter((a) => (!ot || a.object_type === ot) && (!oid || a.object_id === oid) && (!ac || a.action.startsWith(ac)) && (!actor || ci(a.actor).includes(actor)));
    return { total: rows.length, items: page(rows, q) };
  }
  throw notFound();
}

// --------------------------------------------------------------------------------------------------------- POST
function findProposal(id: number): { eid: number; p: Row } {
  for (const [eid, e] of Object.entries<Row>(S.per_entity)) { const p = (e.proposals as Row[]).find((x) => x.id === id); if (p) return { eid: +eid, p }; }
  throw notFound();
}

function post(path: string, body: Row): unknown {
  let m: RegExpMatchArray | null;
  const u = me();
  if (path === "/auth/login") {
    const email = String(body.email ?? "").toLowerCase().trim();
    if (!USERS[email] || body.password !== "demo1234") throw new ApiError(401, "Invalid credentials (demo password is demo1234)");
    current = email;
    return { token: "demo-" + email, user: { id: USERS[email].id, name: USERS[email].name, email, role: USERS[email].role } };
  }
  if (!u) throw new ApiError(401, "Not signed in");
  const may = (...r: string[]) => { if (!r.includes(u.role)) throw new ApiError(403, `Your demo role (${u.role.replace("_", " ")}) cannot do this`); };

  if ((m = path.match(/^\/proposals\/(\d+)\/accept$/))) {
    may("ar_analyst", "ar_manager", "treasury");
    const { eid, p } = findProposal(+m[1]);
    if (p.status !== "pending_review") throw new ApiError(409, "This proposal was already decided");
    p.status = "accepted"; p.decided_by = u.name; setTxn(eid, p.txn_id, "matched"); recount(eid);
    audit(eid, "proposal.accepted", "MatchProposal", p.id, { txn: p.txn_id, confidence: p.confidence });
    return p;
  }
  if ((m = path.match(/^\/proposals\/(\d+)\/reject$/))) {
    may("ar_analyst", "ar_manager", "treasury");
    const { eid, p } = findProposal(+m[1]);
    if (p.status !== "pending_review") throw new ApiError(409, "This proposal was already decided");
    p.status = "rejected"; p.decided_by = u.name; setTxn(eid, p.txn_id, "exception"); recount(eid);
    audit(eid, "proposal.rejected", "MatchProposal", p.id, { txn: p.txn_id, reason: body.reason ?? "" });
    return p;
  }
  if ((m = path.match(/^\/exceptions\/(\d+)\/assign$/))) {
    may("ar_analyst", "ar_manager", "treasury");
    const x = findException(+m[1]); x.x.assignee_id = body.assignee_id ?? u.id; if (x.x.status === "open") x.x.status = "in_progress";
    syncException(x.eid, x.x); return x.x;
  }
  if ((m = path.match(/^\/exceptions\/(\d+)\/notes$/))) {
    may("ar_analyst", "ar_manager", "treasury");
    const x = findException(+m[1]); (x.x.notes ??= []).push({ id: x.x.notes.length + 1, user_id: u.id, text: String(body.note ?? body.text ?? ""), created_at: now() });
    syncException(x.eid, x.x); return x.x;
  }
  if ((m = path.match(/^\/exceptions\/(\d+)\/resolve$/))) {
    may("ar_analyst", "ar_manager", "treasury");
    const { eid, x } = findException(+m[1]);
    const needsApproval = ["write_off", "refund", "classify_non_ar", "classify_interco"].includes(body.action);
    if (needsApproval) {
      const note = String(body.params?.note ?? "").trim();
      if (note.length < 5) throw new ApiError(409, "A reason of at least 5 characters is required");
      const role = body.action === "classify_interco" || x.amount_minor >= 100000000 ? "finance_head" : "ar_manager";
      const ap = { id: Math.max(0, ...(pe(eid).approvals as Row[]).map((a) => a.id)) + 1, entity_id: eid, kind: body.action === "write_off" ? "write_off" : body.action === "refund" ? "refund" : "adjustment",
        status: "pending", amount_minor: x.amount_minor, required_role: role, requested_by: u.id, requested_by_name: u.name, decided_by: null, decided_by_name: null, reason: note,
        decision_note: null, payload: { exception_id: x.id }, ref_type: "ExceptionCase", ref_id: x.id, created_at: now(), decided_at: null, can_decide: false };
      pe(eid).approvals.unshift(ap); x.status = "pending_approval"; syncException(eid, x); recount(eid);
      audit(eid, "approval.requested", "ApprovalRequest", ap.id, { kind: ap.kind, amount_minor: ap.amount_minor });
      return { result: { approval_id: ap.id, required_role: role }, exception: x };
    }
    if (["mark_duplicate", "link_customer", "apply_fifo"].includes(body.action)) {
      x.status = "resolved"; x.resolved_at = now(); x.resolution = body.action === "mark_duplicate" ? "duplicate_statement_line" : body.action; syncException(eid, x); recount(eid);
      if (x.txn_id && body.action === "mark_duplicate") setTxn(eid, x.txn_id, "ignored");
      audit(eid, "exception.resolved", "ExceptionCase", x.id, { action: body.action });
      return { result: {}, exception: x };
    }
    throw readOnly(String(body.action));
  }
  if ((m = path.match(/^\/approvals\/(\d+)\/decide$/))) {
    may(...APPROVERS);
    let found: { eid: number; a: Row } | null = null;
    for (const [eid, e] of Object.entries<Row>(S.per_entity)) { const a = (e.approvals as Row[]).find((r) => r.id === +m![1]); if (a) found = { eid: +eid, a }; }
    if (!found) throw notFound();
    const { eid, a } = found;
    if (a.status !== "pending") throw new ApiError(409, "Already decided");
    if (a.requested_by === u.id) throw new ApiError(409, "You requested this, so someone else has to decide it (segregation of duties)");
    if (!canDecide(a)) throw new ApiError(403, `This needs a ${a.required_role.replace("_", " ")}`);
    if (!body.approve && !String(body.note ?? "").trim()) throw new ApiError(422, "Give a reason when rejecting");
    a.status = body.approve ? "approved" : "rejected"; a.decided_by = u.id; a.decided_by_name = u.name; a.decision_note = body.note ?? ""; a.decided_at = now();
    const ex = (pe(eid).exceptions as Row[]).find((r) => r.id === a.ref_id);
    if (ex) { ex.status = body.approve ? "resolved" : "open"; if (body.approve) ex.resolved_at = now(); syncException(eid, ex); if (body.approve && ex.txn_id) setTxn(eid, ex.txn_id, "matched"); }
    recount(eid); audit(eid, body.approve ? "approval.approved" : "approval.rejected", "ApprovalRequest", a.id, { note: body.note ?? "" });
    return a;
  }
  if ((m = path.match(/^\/tds\/(\d+)\/status$/))) {
    may("ar_analyst", "ar_manager", "treasury", "finance_head");
    for (const e of Object.values<Row>(S.per_entity)) { const t = (e.tds as Row[]).find((r) => r.id === +m![1]); if (t) { Object.assign(t, Object.fromEntries(Object.entries(body).filter(([, v]) => v !== null))); return t; } }
    throw notFound();
  }
  if ((m = path.match(/^\/advances\/(\d+)\/(accept|reject)$/))) {
    may("ar_analyst", "ar_manager");
    for (const [eid, e] of Object.entries<Row>(S.per_entity)) {
      const a = (e.advances.proposals as Row[]).find((r) => r.id === +m![1]);
      if (!a) continue;
      if (m[2] === "accept") {
        const bal = (e.advances.balances as Row[]).find((b) => b.id === a.balance_id), inv = (e.invoices as Row[]).find((i) => i.id === a.invoice.id);
        if (bal) { bal.remaining_minor -= a.amount_minor; if (bal.remaining_minor <= 0) e.advances.balances = (e.advances.balances as Row[]).filter((b) => b.id !== bal.id); }
        if (inv) inv.open_minor -= a.amount_minor;
        audit(+eid, "advance.applied", "AdvanceApplication", a.id, { invoice: a.invoice.number, amount_minor: a.amount_minor, basis: a.basis });
      }
      e.advances.proposals = (e.advances.proposals as Row[]).filter((r) => r.id !== a.id);
      e.advances.total_minor = (e.advances.balances as Row[]).reduce((n, b) => n + b.remaining_minor, 0);
      return { ...a, status: m[2] === "accept" ? "applied" : "rejected" };
    }
    throw notFound();
  }
  if (path === "/advances/propose") return { proposed: 0, skipped_foreign_balances: 0 };
  if ((m = path.match(/^\/customers\/(\d+)\/trust$/))) {
    may("ar_manager", "finance_head");
    for (const e of Object.values<Row>(S.per_entity)) {
      const t = e.trust[m[1]];
      if (!t) continue;
      if (body.mode === "auto" && !String(body.note ?? "").trim()) throw new ApiError(422, "Give a reason when pinning a customer to straight-through");
      if (body.mode) { t.mode = body.mode; t.pinned = body.mode !== "learning"; if (body.mode === "learning") t.clean_streak = 0; }
      if (body.clear_threshold) t.threshold_override = null; else if (body.threshold_override != null) t.threshold_override = body.threshold_override;
      t.note = body.note ?? "";
      const row = (e.customers as Row[]).find((c) => c.id === +m![1]); if (row && body.mode) row.trust_mode = body.mode;
      return t;
    }
    throw notFound();
  }
  if (path === "/periods/lock") {
    may("admin", "finance_head");
    const eid = Number(body.entity_id), e = pe(eid), cur = e.period_lock.locked_through as string | null, nw = (body.locked_through as string | null) ?? null;
    if (nw && nw > new Date().toISOString().slice(0, 10)) throw new ApiError(422, "A period cannot be closed in the future");
    if (cur && (nw === null || nw < cur) && String(body.note ?? "").trim().length < 5) throw new ApiError(422, "Give a reason (at least 5 characters) to re-open a closed period");
    e.period_lock = { locked_through: nw, note: body.note ?? "", locked_at: now(), locked_by: u.id };
    audit(eid, cur && (nw === null || nw < cur) ? "period.reopened" : "period.locked", "LegalEntity", eid, { before: cur, after: nw, note: body.note ?? "" });
    return { locked_through: nw };
  }
  const names: [RegExp, string][] = [[/^\/tax\//, "Importing tax statements"], [/^\/entities\/\d+\/(notifications|integrations|config)/, "Changing settings"], [/^\/posting\/batches\/\d+\/ack-file/, "ERP confirmation upload"],[/^\/matching\/run$/, "Run matching"], [/^\/posting\/batches/, "Posting batches"], [/^\/ingest|^\/entities\/\d+\/mailbox/, "Import / mailbox poll"],
    [/^\/bank-accounts\/\d+\/sync/, "Bank feed sync"], [/^\/fx-rates/, "FX rate changes"], [/^\/models/, "Model training"], [/^\/storage/, "Storage test"], [/^\/demo\/seed/, "Reset demo data"],
    [/^\/transactions\/\d+\/manual-match/, "Manual match"], [/^\/remittances/, "Release remittance"]];
  throw readOnly(names.find(([r]) => r.test(path))?.[1] ?? path);
}

function findException(id: number): { eid: number; x: Row } {
  for (const [eid, e] of Object.entries<Row>(S.per_entity)) { const x = (e.exceptions as Row[]).find((r) => r.id === id); if (x) return { eid: +eid, x }; }
  throw notFound();
}
function syncException(eid: number, x: Row) { const d = pe(eid).exception_detail[x.id]; if (d) Object.assign(d, x); }

// ------------------------------------------------------------------------------------------------------ entry point
const delay = () => new Promise((r) => setTimeout(r, 60 + Math.random() * 90));

export async function demoRequest<T>(method: string, fullPath: string, body?: unknown): Promise<T> {
  await delay();
  const [path, qs] = fullPath.split("?");
  const q = new URLSearchParams(qs ?? "");
  if (method === "GET") {
    if (path !== "/auth/me" && !me()) throw new ApiError(401, "Not signed in");
    return clone(get(path, q)) as T;
  }
  return clone(post(path, (body ?? {}) as Row)) as T;
}
export const demoSignedIn = () => !!current;
export const demoSignOut = () => { current = null; };
