import { useEffect, useState } from "react";
import { api, type CustomerBrief, type ExceptionCase, type Invoice } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync, useDebounced } from "../hooks";
import { ago, date, label as lbl, money, title, toMinor } from "../format";
import { Chip, Empty, ErrorBox, Field, Loading, Modal, PageHead, StatusChip } from "../components/ui";

const TYPES = ["", "unidentified_receipt", "on_account", "short_pay", "overpayment", "duplicate_payment", "intercompany", "non_ar_credit", "short_pay_tds", "disputed_invoice"];

export default function Exceptions() {
  const { entity, can, user } = useApp();
  const [type, setType] = useState(""); const [scope, setScope] = useState<"open" | "mine" | "closed">("open");
  const status = scope === "closed" ? "resolved,written_off" : "open,in_progress,pending_approval";
  const r = useAsync(() => api.get<{ total: number; items: ExceptionCase[] }>(`/exceptions?entity_id=${entity.id}&status=${status}&limit=200${type ? `&type=${type}` : ""}${scope === "mine" ? `&assignee_id=${user.id}` : ""}`), [entity.id, type, scope]);
  const [sel, setSel] = useState<number | null>(null);
  const cur = r.data?.items.find((x) => x.id === sel) ?? r.data?.items[0] ?? null;
  return (
    <>
      <PageHead title="Exceptions" sub="Unmatched, short, over and duplicate cash — prioritised by value, age and customer tier." />
      <div className="toolbar">
        {(["open", "mine", "closed"] as const).map((s) => <button key={s} className={`btn ${scope === s ? "primary" : ""}`} onClick={() => setScope(s)}>{title(s)}</button>)}
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Type">{TYPES.map((t) => <option key={t} value={t}>{t ? lbl(t) : "All types"}</option>)}</select>
        {r.data && <span className="muted">{r.data.total} cases</span>}
      </div>
      {r.error && <ErrorBox msg={r.error} />}
      {r.loading && !r.data ? <Loading rows={6} /> : !r.data?.items.length ? <div className="card"><Empty>No exceptions here.</Empty></div> : (
        <div className="split">
          <div className="card flush"><div className="list">
            {r.data.items.map((x) => {
              const late = x.due_at && new Date(x.due_at + "Z") < new Date() && !x.resolution;
              return (
                <div key={x.id} className={`list-item ${cur?.id === x.id ? "sel" : ""}`} onClick={() => setSel(x.id)}>
                  <div className="row between"><Chip tone={x.type === "duplicate_payment" || x.type === "unidentified_receipt" ? "bad" : "warn"}>{lbl(x.type)}</Chip><b className="num">{money(x.amount_minor, x.currency ?? "INR")}</b></div>
                  <div style={{ fontSize: 13, marginTop: 3 }}>{x.customer?.name ?? <i className="muted">no customer</i>}</div>
                  <div className="faint" style={{ fontSize: 12 }}>{ago(x.created_at)} · priority {x.priority.toFixed(0)} {late && <span style={{ color: "var(--bad)" }}>· past SLA</span>} <StatusChip s={x.status} /></div>
                </div>
              );
            })}
          </div></div>
          {cur && <Case key={cur.id} id={cur.id} canAct={can("ar_analyst", "ar_manager")} />}
        </div>
      )}
    </>
  );
}

type Act = null | { action: string; label: string; needsReason?: boolean; pickCustomer?: boolean; amount?: boolean };

function Case({ id, canAct }: { id: number; canAct: boolean }) {
  const { entity } = useApp();
  const d = useAsync(() => api.get<ExceptionCase>(`/exceptions/${id}`), [id]);
  const { run, busy } = useAction();
  const [act, setAct] = useState<Act>(null);
  const [note, setNote] = useState("");
  if (!d.data) return <div className="card"><Loading /></div>;
  const x = d.data;
  const closed = x.status === "resolved" || x.status === "written_off";
  const done = () => { setAct(null); d.reload(); };

  const ACTIONS: Record<string, Act> = {
    link_customer: { action: "link_customer", label: "Link to customer", pickCustomer: true },
    on_account: { action: "on_account", label: "Hold on account", pickCustomer: !x.customer },
    apply_fifo: { action: "apply_fifo", label: "Apply to oldest invoices" },
    write_off: { action: "write_off", label: "Write off (needs approval)", needsReason: true, amount: true },
    refund: { action: "refund", label: "Refund (needs approval)", needsReason: true },
    mark_duplicate: { action: "mark_duplicate", label: "Mark as duplicate statement line" },
    dispute: { action: "dispute", label: "Mark invoice disputed" },
    await_balance: { action: "await_balance", label: "Wait for balance" },
    request_remittance: { action: "request_remittance", label: "Request remittance advice" },
    classify_non_ar: { action: "classify_non_ar", label: "Classify as non-AR income (needs approval)", needsReason: true },
    classify_interco: { action: "classify_interco", label: "Classify as intercompany (needs approval)", needsReason: true },
  };
  const unique = Array.from(new Map(x.suggestions.map((s) => [s.action, s])).values());

  return (
    <div className="grid">
      <div className="card">
        <div className="row between wrap"><div><Chip tone="bad">{lbl(x.type)}</Chip> <StatusChip s={x.status} /></div><b style={{ fontSize: 22 }} className="num">{money(x.amount_minor, x.currency ?? "INR")}</b></div>
        <p>{x.reason}</p>
        {x.txn && <div className="pre">{x.txn.narration}</div>}
        <div className="muted mt" style={{ fontSize: 12.5 }}>Opened {ago(x.created_at)} · SLA {x.due_at ? date(x.due_at) : "—"} {x.resolution && <>· Resolution: <b>{title(x.resolution)}</b></>}</div>
      </div>
      {!closed && canAct && (
        <div className="card">
          <h2>Resolve</h2>
          {x.status === "pending_approval" ? <div className="note warn">Waiting on an approver — see Approvals.</div> : <>
            <div className="row wrap">
              {unique.map((s, i) => { const a = ACTIONS[s.action]; return a ? <button key={i} className="btn" disabled={busy} onClick={() => (a.needsReason || a.pickCustomer || a.amount ? setAct(s.customer_id ? { ...a, pickCustomer: false } : a) : run(() => api.post(`/exceptions/${id}/resolve`, { action: a.action, params: s.customer_id ? { customer_id: s.customer_id } : {} }), "Done").then(done))}>{s.label}</button> : null; })}
              {x.type !== "unidentified_receipt" && !unique.some((s) => s.action === "dispute") && x.invoice_id && <button className="btn" onClick={() => setAct(ACTIONS.dispute)}>Mark disputed</button>}
              <button className="btn" onClick={() => setAct(ACTIONS.link_customer)}>Link customer…</button>
              <button className="btn" onClick={() => setAct(ACTIONS.mark_duplicate)}>Duplicate line</button>
            </div>
            {unique.filter((s) => s.action === "link_customer").map((s, i) => <div key={i} className="faint mt" style={{ fontSize: 12 }}>Suggested payer match score: {Math.round((s.score ?? 0) * 100)}%</div>)}
          </>}
        </div>
      )}
      <div className="card">
        <h2>Notes</h2>
        {x.notes.length ? x.notes.map((n) => <div key={n.id} className="note"><span className="faint">{ago(n.created_at)}</span> {n.text}</div>) : <span className="muted">No notes.</span>}
        {canAct && <div className="row mt"><input placeholder="Add a note…" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Note" /><button className="btn" disabled={!note.trim() || busy} onClick={() => run(() => api.post(`/exceptions/${id}/notes`, { text: note }), "Note added").then(() => { setNote(""); d.reload(); })}>Add</button></div>}
      </div>
      <ActionModal act={act} x={x} entityId={entity.id} onClose={() => setAct(null)} onDone={done} />
    </div>
  );
}

function ActionModal({ act, x, entityId, onClose, onDone }: { act: Act; x: ExceptionCase; entityId: number; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState(""); const [cust, setCust] = useState<CustomerBrief | null>(null); const [q, setQ] = useState(""); const dq = useDebounced(q);
  const [res, setRes] = useState<CustomerBrief[]>([]); const [amt, setAmt] = useState(""); const [invId, setInvId] = useState<number | null>(null); const [invs, setInvs] = useState<Invoice[]>([]);
  const { run, busy } = useAction();
  useEffect(() => { setReason(""); setCust(null); setQ(""); setAmt(String(x.amount_minor / 100)); setInvId(x.invoice_id); }, [act, x.id, x.amount_minor, x.invoice_id]);
  useEffect(() => { if (act?.pickCustomer) api.get<any>(`/customers?entity_id=${entityId}&q=${encodeURIComponent(dq)}&limit=8`).then((r) => setRes(r.items)).catch(() => {}); }, [dq, act, entityId]);
  useEffect(() => { if (act?.amount && x.customer) api.get<any>(`/invoices?entity_id=${entityId}&customer_id=${x.customer.id}&status=open&limit=50`).then((r) => { setInvs(r.items); if (!x.invoice_id && r.items[0]) setInvId(r.items[0].id); }).catch(() => {}); }, [act, x.customer, x.invoice_id, entityId]);
  if (!act) return null;
  const needCust = act.pickCustomer && !cust;
  const m = toMinor(amt);
  const submit = async () => {
    const params: Record<string, unknown> = { note: reason };
    if (cust) params.customer_id = cust.id;
    if (act.amount) { params.amount_minor = m; if (invId) params.invoice_id = invId; }
    const r = await run(() => api.post<any>(`/exceptions/${x.id}/resolve`, { action: act.action, params }), (r: any) => r.result?.approval_id ? `Sent for approval (${String(r.result.required_role).replace("_", " ")})` : "Done");
    if (r) onDone();
  };
  return (
    <Modal open onClose={onClose} title={act.label}>
      {act.pickCustomer && <Field label="Customer"><input placeholder="Search name, code, GSTIN…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="card flush mt" style={{ maxHeight: 150, overflow: "auto" }}><table><tbody>{res.map((c) => <tr key={c.id} className={`click ${cust?.id === c.id ? "sel" : ""}`} onClick={() => setCust(c)}><td>{c.name}</td><td className="muted">{c.code}</td></tr>)}</tbody></table></div>
        {cust && <div className="chip good mt">Selected: {cust.name}</div>}</Field>}
      {act.amount && <>
        <Field label="Invoice"><select value={invId ?? ""} onChange={(e) => setInvId(Number(e.target.value))}>{invs.map((i) => <option key={i.id} value={i.id}>{i.number} — open {money(i.open_minor, i.currency)}</option>)}</select></Field>
        <Field label="Amount to write off (₹)"><input value={amt} onChange={(e) => setAmt(e.target.value)} inputMode="decimal" />{Number.isNaN(m) && <span style={{ color: "var(--bad)" }}>Enter a valid amount</span>}</Field></>}
      {act.needsReason && <Field label="Reason (required, audited)" hint="A different person with sufficient authority must approve this."><textarea value={reason} onChange={(e) => setReason(e.target.value)} /></Field>}
      {!act.needsReason && <Field label="Note (optional)"><textarea value={reason} onChange={(e) => setReason(e.target.value)} /></Field>}
      <div className="row between"><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || needCust || (act.needsReason && reason.trim().length < 5) || (act.amount && (Number.isNaN(m) || m <= 0))} onClick={submit}>Confirm</button></div>
    </Modal>
  );
}
