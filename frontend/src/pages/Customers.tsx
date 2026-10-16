import { useState } from "react";
import { api, type Invoice } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync, useDebounced } from "../hooks";
import { compact, date, money, rateLabel, title } from "../format";
import { Chip, Empty, ErrorBox, Field, Loading, Modal, PageHead, StatusChip, Tabs } from "../components/ui";

export default function Customers() {
  const { entity } = useApp();
  const [tab, setTab] = useState<"customers" | "invoices">("customers");
  const [q, setQ] = useState(""); const dq = useDebounced(q);
  const [sel, setSel] = useState<number | null>(null);
  const [inv, setInv] = useState("open");
  const cs = useAsync(() => api.get<any>(`/customers?entity_id=${entity.id}&q=${encodeURIComponent(dq)}&limit=200`), [entity.id, dq]);
  const is = useAsync(() => (tab === "invoices" ? api.get<any>(`/invoices?entity_id=${entity.id}&status=${inv}&q=${encodeURIComponent(dq)}&limit=200`) : Promise.resolve(null)), [entity.id, dq, tab, inv]);
  return (
    <>
      <PageHead title="Customers & invoices" sub="Master data, learned payer identities and open items imported from the ERP." />
      <Tabs value={tab} onChange={setTab} tabs={[["customers", "Customers"], ["invoices", "Invoices"]]} />
      <div className="toolbar"><input placeholder={tab === "customers" ? "Search name, code, GSTIN…" : "Search invoice number…"} value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
        {tab === "invoices" && <select value={inv} onChange={(e) => setInv(e.target.value)} aria-label="Invoice status"><option value="open">Open</option><option value="paid">Paid</option><option value="disputed">Disputed</option><option value="">All</option></select>}</div>
      {tab === "customers" ? (
        <div className="split">
          <div className="card flush"><div className="list">{cs.error && <ErrorBox msg={cs.error} />}{!cs.data ? <Loading /> : cs.data.items.map((c: any) => (
            <div key={c.id} className={`list-item ${sel === c.id ? "sel" : ""}`} onClick={() => setSel(c.id)}><div className="row between"><b>{c.name}</b><span className="num">{compact(c.open_minor)}</span></div><div className="faint" style={{ fontSize: 12 }}>{c.code} · {c.gstin ?? "no GSTIN"} · tier {c.tier}</div></div>
          ))}</div></div>
          {sel ? <CustomerDetail id={sel} onSaved={cs.reload} /> : <div className="card"><Empty>Select a customer</Empty></div>}
        </div>
      ) : (
        <div className="card flush">{!is.data ? <Loading /> : !is.data.items.length ? <Empty>No invoices.</Empty> : (
          <table><thead><tr><th>Invoice</th><th>Customer</th><th>Date</th><th>Due</th><th className="num">Total</th><th className="num">Open</th><th>Status</th></tr></thead>
            <tbody>{is.data.items.map((i: Invoice) => <tr key={i.id}><td>{i.number} {i.doc_type === "credit_note" && <Chip>credit note</Chip>}</td><td>{i.customer.name}</td><td>{date(i.invoice_date)}</td><td>{date(i.due_date)}</td><td className="num">{money(i.total_minor, i.currency)}</td><td className="num">{money(i.open_minor, i.currency)}</td><td><StatusChip s={i.status} /></td></tr>)}</tbody></table>
        )}</div>
      )}
    </>
  );
}

function CustomerDetail({ id, onSaved }: { id: number; onSaved: () => void }) {
  const { can } = useApp();
  const d = useAsync(() => api.get<any>(`/customers/${id}`), [id]);
  const [edit, setEdit] = useState(false);
  if (!d.data) return <div className="card"><Loading /></div>;
  const c = d.data, p = c.profile;
  return (
    <div className="grid">
      <div className="card">
        <div className="row between"><h2 style={{ margin: 0 }}>{c.name}</h2>{can("admin", "finance_head", "ar_analyst", "ar_manager") && <button className="btn sm" onClick={() => setEdit(true)}>Edit matching hints</button>}</div>
        <div className="grid g3 mt" style={{ fontSize: 13 }}>
          <div><div className="faint">GSTIN</div><span className="mono">{c.gstin ?? "—"}</span></div><div><div className="faint">PAN</div><span className="mono">{c.pan ?? "—"}</span></div><div><div className="faint">Terms</div>{c.payment_terms_days} days</div>
          <div><div className="faint">Usual TDS</div>{c.tds_rate_bp ? `${rateLabel(c.tds_rate_bp)} on ${c.tds_base} value${c.tds_section ? ` (${c.tds_section})` : ""}` : "none recorded"}</div>
          <div><div className="faint">Virtual account</div><span className="mono">{c.virtual_account_no ?? "—"}</span></div><div><div className="faint">Tier</div>{c.tier}</div>
        </div>
      </div>
      <div className="grid g2">
        <div className="card"><h2>Learned behaviour</h2>{p ? <table><tbody>
          <tr><td className="muted">Payments learned from</td><td className="num">{p.payments}</td></tr><tr><td className="muted">Multi-invoice payments</td><td className="num">{p.multi_invoice_payments}</td></tr><tr><td className="muted">With TDS</td><td className="num">{p.tds_payments}</td></tr>
          <tr><td className="muted">Avg days to pay</td><td className="num">{p.avg_days_to_pay}</td></tr><tr><td className="muted">Avg days late</td><td className="num">{p.avg_days_late}</td></tr></tbody></table> : <span className="muted">No confirmed payments yet — the model starts learning from the first accepted match.</span>}</div>
        <div className="card"><h2>Known payer identities</h2>{c.identifiers.length ? c.identifiers.map((i: any, k: number) => <div key={k} className="row between"><span><Chip>{i.kind}</Chip> <span className="mono">{i.value}</span></span><span className="faint">{i.source} · ×{i.hits}</span></div>) : <span className="muted">None learned yet.</span>}</div>
      </div>
      <div className="card flush"><div style={{ padding: "12px 16px 0" }}><h2>Open items</h2></div>{c.open_invoices.length ? <table><thead><tr><th>Invoice</th><th>Due</th><th className="num">Open</th><th>Status</th></tr></thead><tbody>{c.open_invoices.map((i: Invoice) => <tr key={i.id}><td>{i.number}</td><td>{date(i.due_date)}</td><td className="num">{money(i.open_minor, i.currency)}</td><td><StatusChip s={i.status} /></td></tr>)}</tbody></table> : <Empty>Nothing open.</Empty>}</div>
      {edit && <EditModal c={c} onClose={() => setEdit(false)} onSaved={() => { setEdit(false); d.reload(); onSaved(); }} />}
    </div>
  );
}

function EditModal({ c, onClose, onSaved }: { c: any; onClose: () => void; onSaved: () => void }) {
  const [rate, setRate] = useState(c.tds_rate_bp ? String(c.tds_rate_bp / 100) : ""); const [base, setBase] = useState(c.tds_base); const [van, setVan] = useState(c.virtual_account_no ?? "");
  const [tier, setTier] = useState(c.tier); const { run, busy } = useAction();
  const bp = rate === "" ? 0 : Math.round(parseFloat(rate) * 100);
  return (
    <Modal open onClose={onClose} title={`Matching hints — ${c.name}`}>
      <Field label="Usual TDS rate (%)" hint="Used to recognise a TDS short-pay. Statutory rates change; confirm against your tax team."><input value={rate} onChange={(e) => setRate(e.target.value)} inputMode="decimal" /></Field>
      <Field label="TDS is computed on"><select value={base} onChange={(e) => setBase(e.target.value)}><option value="taxable">Taxable value (excl. GST) — typical</option><option value="gross">Invoice total (incl. GST)</option></select></Field>
      <Field label="Virtual account number"><input value={van} onChange={(e) => setVan(e.target.value)} /></Field>
      <Field label="Priority tier"><select value={tier} onChange={(e) => setTier(e.target.value)}><option>A</option><option>B</option><option>C</option></select></Field>
      <div className="row between"><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || Number.isNaN(bp) || bp < 0 || bp > 3000} onClick={async () => { if (await run(() => api.patch(`/customers/${c.id}`, { tds_rate_bp: bp, tds_base: base, virtual_account_no: van, tier }), "Saved")) onSaved(); }}>Save</button></div>
    </Modal>
  );
}
