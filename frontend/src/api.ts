export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let token: string | null = (() => { try { return sessionStorage.getItem("token"); } catch { return null; } })();
let onUnauthorized: () => void = () => {};
export const setUnauthorizedHandler = (fn: () => void) => (onUnauthorized = fn);
export function setToken(t: string | null) {
  token = t;
  try { t ? sessionStorage.setItem("token", t) : sessionStorage.removeItem("token"); } catch { /* storage unavailable */ }
}
export const hasToken = () => !!token;

async function request<T>(method: string, path: string, body?: unknown, form?: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/v1${path}`, { method, headers, body: form ?? (body !== undefined ? JSON.stringify(body) : undefined) });
  if (res.status === 401 && path !== "/auth/login") { setToken(null); onUnauthorized(); }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: any) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ") : msg;
    } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, msg);
  }
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("application/json") ? res.json() : res.blob()) as Promise<T>;
}

export const api = {
  get: <T,>(p: string) => request<T>("GET", p),
  post: <T,>(p: string, b?: unknown) => request<T>("POST", p, b ?? {}),
  put: <T,>(p: string, b: unknown) => request<T>("PUT", p, b),
  patch: <T,>(p: string, b: unknown) => request<T>("PATCH", p, b),
  upload: <T,>(p: string, f: FormData) => request<T>("POST", p, undefined, f),
};

export async function download(path: string, filename: string, body?: unknown) {
  const res = await fetch(`/api/v1${path}`, { method: body ? "POST" : "GET", headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch {} throw new ApiError(res.status, m); }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

// ---------- types (mirrors backend serializers) ----------
export type Role = "admin" | "ar_analyst" | "ar_manager" | "treasury" | "finance_head" | "auditor";
export interface User { id: number; name: string; email: string; role: Role }
export interface Entity { id: number; name: string; gstin: string | null; pan: string | null }
export interface CustomerBrief { id: number; code: string; name: string; tier: string }
export interface Txn {
  id: number; entity_id: number; bank: string; txn_date: string; amount_minor: number; fee_minor: number; receivable_minor: number; currency: string;
  channel: string; fx_ccy?: string | null; fx_minor?: number | null; utr: string | null; narration: string; payer_name: string | null; payer_vpa: string | null; status: string; kind: string | null;
  customer: CustomerBrief | null; customer_confidence: number | null; remittance_id: number | null; source: string;
}
export interface Allocation {
  invoice_id: number; number: string; applied_minor: number; tds_minor: number; writeoff_minor: number; tds_section: string | null; tds_rate_bp: number | null;
  reference_hit: string | null; invoice_open_minor: number; invoice_total_minor: number; due_date: string; disputed: boolean; currency: string;
}
export interface Contribution { feature: string; label: string; value: number; weight: number; contribution: number }
export interface Proposal {
  id: number; txn_id: number; customer: CustomerBrief | null; status: string; strategy: string; confidence: number; model_version: string; auto: boolean;
  explanation: { summary: string; evidence: string[]; warnings: string[]; contributions: Contribution[]; alternatives: { invoices: string[]; confidence: number; strategy: string }[] };
  guardrails: string[]; currency: string; settle_currency: string | null; settle_rate_micro: number | null; writeoff_kind: string; applied_minor: number; tds_minor: number; writeoff_minor: number; unapplied_minor: number; short_minor: number; created_at: string;
  allocations: Allocation[]; txn?: Txn; batch_id: number | null;
}
export interface Suggestion { action: string; label: string; customer_id?: number; score?: number }
export interface ExceptionCase {
  id: number; type: string; status: string; txn_id: number | null; invoice_id: number | null; customer: CustomerBrief | null; amount_minor: number; reason: string;
  suggestions: Suggestion[]; currency: string | null; priority: number; assignee_id: number | null; created_at: string; due_at: string | null; resolution: string | null;
  notes: { id: number; user_id: number | null; text: string; created_at: string }[]; txn?: Txn;
}
export interface Approval {
  id: number; kind: string; status: string; amount_minor: number; required_role: string; requested_by_name: string; decided_by_name: string | null; reason: string;
  decision_note: string | null; created_at: string; can_decide: boolean; payload: Record<string, unknown>;
}
export interface Invoice {
  id: number; number: string; doc_type: string; customer: CustomerBrief; invoice_date: string; due_date: string; total_minor: number; open_minor: number; taxable_minor: number; currency: string; booking_rate_micro: number;
  gst_minor: number; status: string; disputed: boolean;
}
export interface Batch {
  id: number; status: string; connector: string | null; created_at: string; total_minor: number; voucher_count: number; checksum: string | null; error: string | null;
  lines?: { id: number; voucher_no: string; line_type: string; account: string; debit_minor: number; credit_minor: number; currency: string; base_debit_minor: number; base_credit_minor: number; rate_micro: number | null; posting_date: string; narration: string; erp_doc_no: string | null }[];
}
