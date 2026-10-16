import { useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import { useAsync, useDebounced } from "../hooks";
import { date, money, pct } from "../format";
import { Chip, Empty, ErrorBox, Loading, PageHead, StatusChip } from "../components/ui";

const STATUSES = ["", "new", "review", "exception", "matched", "partially_matched", "posted", "ignored"];

export default function Receipts() {
  const { entity, can } = useApp();
  const [status, setStatus] = useState(""); const [q, setQ] = useState(""); const dq = useDebounced(q);
  const [dir, setDir] = useState("credit"); const [open, setOpen] = useState<number | null>(null);
  const r = useAsync(() => api.get<any>(`/transactions?entity_id=${entity.id}&limit=200&direction=${dir}${status ? `&status=${status}` : ""}${dq ? `&q=${encodeURIComponent(dq)}` : ""}`), [entity.id, status, dq, dir]);
  return (
    <>
      <PageHead title="Bank receipts" sub="Every bank line ingested, with its journey from statement to ERP." />
      <div className="toolbar">
        <input placeholder="Search narration, UTR, payer…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">{STATUSES.map((s) => <option key={s} value={s}>{s ? s.replace("_", " ") : "All statuses"}</option>)}</select>
        <select value={dir} onChange={(e) => setDir(e.target.value)} aria-label="Direction"><option value="credit">Credits (receipts)</option><option value="debit">Debits (ignored)</option></select>
        {r.data && <span className="muted">{r.data.total} lines</span>}
        {r.data && Object.entries(r.data.status_counts as Record<string, number>).map(([k, v]) => <Chip key={k} tone={k === "exception" ? "bad" : k === "review" ? "warn" : ""}>{k.replace("_", " ")} {v}</Chip>)}
      </div>
      {r.error && <ErrorBox msg={r.error} />}
      <div className="card flush">
        {r.loading && !r.data ? <Loading rows={8} /> : !r.data?.items.length ? <Empty>No receipts match.</Empty> : (
          <table><thead><tr><th>Date</th><th>Narration</th><th>Payer → customer</th><th>Channel</th><th className="num">Amount</th><th>Status</th></tr></thead>
            <tbody>{r.data.items.map((t: any) => (
              <tr key={t.id} className={`click ${open === t.id ? "sel" : ""}`} onClick={() => setOpen(open === t.id ? null : t.id)}>
                <td style={{ whiteSpace: "nowrap" }}>{date(t.txn_date)}</td>
                <td style={{ maxWidth: 420 }}><div className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={t.narration}>{t.narration}</div></td>
                <td>{t.payer_name ?? <span className="faint">—</span>}{t.customer && <div className="muted" style={{ fontSize: 12 }}>→ {t.customer.name} {t.customer_confidence !== null && `(${pct(t.customer_confidence)})`}</div>}</td>
                <td><Chip tone="info">{t.channel}</Chip></td>
                <td className="num">{t.fee_minor ? <span title={`bank credit ${money(t.amount_minor, t.currency)} + fee ${money(t.fee_minor, t.currency)}`}>{money(t.receivable_minor, t.currency)}</span> : money(t.amount_minor, t.currency)}{t.fx_ccy && <div className="faint" style={{ fontSize: 11.5 }}>payer sent {money(t.fx_minor, t.fx_ccy)}</div>}</td>
                <td><StatusChip s={t.status} /></td></tr>
            ))}</tbody></table>
        )}
      </div>
      {open && <Detail id={open} onClose={() => setOpen(null)} canSee={can("auditor", "finance_head", "admin")} />}
    </>
  );
}

function Detail({ id, onClose, canSee }: { id: number; onClose: () => void; canSee: boolean }) {
  const d = useAsync(() => api.get<any>(`/transactions/${id}`), [id]);
  const trail = useAsync(() => (canSee ? api.get<any>(`/audit?object_type=PaymentTransaction&object_id=${id}&limit=50`) : Promise.resolve(null)), [id, canSee]);
  return (
    <div className="card mt">
      <div className="row between"><h2>Receipt #{id}</h2><button className="btn sm" onClick={onClose}>Close</button></div>
      {!d.data ? <Loading /> : <div className="grid g2">
        <div>
          <div className="pre">{d.data.narration}</div>
          <table className="mt"><tbody>
            {[["UTR / ref", d.data.utr], ["Payer", d.data.payer_name], ["UPI handle", d.data.payer_vpa], ["Source", d.data.source], ["Bank", d.data.bank], ["Kind", d.data.extra?.kind]].map(([k, v]) => <tr key={k as string}><td className="muted">{k}</td><td>{v ?? "—"}</td></tr>)}
          </tbody></table>
          {d.data.remittance && <><h3 className="mt">Remittance advice</h3><div className="pre">{d.data.remittance.text}</div></>}
        </div>
        <div>
          <h3>Proposals</h3>
          {d.data.proposals.length ? d.data.proposals.map((p: any) => (
            <div key={p.id} className="note"><div className="row between"><b>#{p.id} {p.strategy.replace(/_/g, " ")}</b><span><StatusChip s={p.status} /> {pct(p.confidence)}</span></div>
              <div>{p.explanation.summary}</div></div>
          )) : <span className="muted">None — see exception.</span>}
          {d.data.exceptions.map((x: any) => <div key={x.id} className="note warn"><b>{x.type.replace(/_/g, " ")}</b> · <StatusChip s={x.status} /><div>{x.reason}</div></div>)}
          {trail.data && <><h3 className="mt">Audit trail</h3>{trail.data.items.map((a: any) => <div key={a.seq} className="row between" style={{ fontSize: 12.5 }}><span>{a.action}</span><span className="faint">{a.actor} · {a.ts.slice(0, 16).replace("T", " ")}</span></div>)}</>}
        </div>
      </div>}
    </div>
  );
}
