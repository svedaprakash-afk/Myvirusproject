import { useEffect, useMemo, useState } from "react";
import { api, type Invoice, type Txn } from "../api";
import { useAction, useDebounced } from "../hooks";
import { date, money, toMinor } from "../format";
import { Field, Modal } from "./ui";

interface Row { inv: Invoice; applied: string; tds: string; wo: string }

/** Analyst-built allocation. Cash must reconcile; the server re-validates everything (never trust the client). */
export default function ManualMatch({ txn, customerId, open, onClose, onDone }: { txn: Txn; customerId?: number | null; open: boolean; onClose: () => void; onDone: () => void }) {
  const [q, setQ] = useState(""); const dq = useDebounced(q);
  const [pool, setPool] = useState<Invoice[]>([]);
  const [rows, setRows] = useState<Row[]>([]);
  const [reason, setReason] = useState("");
  const [adv, setAdv] = useState(false);
  const { run, busy } = useAction();

  useEffect(() => {
    if (!open) return;
    const p = new URLSearchParams({ entity_id: String(txn.entity_id), status: "open", limit: "60" });
    if (customerId && !dq) p.set("customer_id", String(customerId));
    if (dq) p.set("q", dq);
    api.get<{ items: Invoice[] }>(`/invoices?${p}`).then((r) => setPool(r.items)).catch(() => setPool([]));
  }, [open, dq, customerId, txn.entity_id]);
  useEffect(() => { if (open) { setRows([]); setReason(""); setQ(""); } }, [open, txn.id]);

  const minor = (s: string) => (s.trim() === "" ? 0 : toMinor(s));
  const applied = useMemo(() => rows.reduce((a, r) => a + (minor(r.applied) || 0), 0), [rows]);
  const remaining = txn.receivable_minor - applied;
  const bad = rows.some((r) => [r.applied, r.tds, r.wo].some((x) => x.trim() !== "" && Number.isNaN(minor(x))));
  const add = (inv: Invoice) => inv.currency === txn.currency && setRows((rs) => rs.some((r) => r.inv.id === inv.id) ? rs : [...rs, { inv, applied: String(Math.max(Math.min(remaining, inv.open_minor), 0) / 100), tds: "", wo: "" }]);
  const set = (i: number, k: keyof Row, v: string) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, [k]: v } : r)));

  async function submit() {
    const res = await run(() => api.post<any>(`/transactions/${txn.id}/manual-match`, {
      reason, allocations: rows.map((r) => ({ invoice_id: r.inv.id, applied_minor: minor(r.applied) || 0, tds_minor: minor(r.tds) || 0, writeoff_minor: minor(r.wo) || 0 })),
    }), (r) => (r.status === "awaiting_approval" ? `Sent for approval (${r.required_role.replace("_", " ")})` : "Applied"));
    if (res) { onDone(); onClose(); }
  }

  return (
    <Modal open={open} onClose={onClose} title={`Allocate ${money(txn.receivable_minor, txn.currency)} manually`}>
      <div className="field"><input placeholder="Search invoice number…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search invoices" /></div>
      <div className="card flush" style={{ maxHeight: 150, overflow: "auto", marginBottom: 12 }}>
        <table><tbody>{pool.length ? pool.map((i) => (
          <tr key={i.id} className="click" onClick={() => add(i)}><td>{i.number}<div className="faint" style={{ fontSize: 12 }}>{i.customer.name} · due {date(i.due_date)}</div></td><td className="num">{money(i.open_minor, i.currency)}</td><td><button className="btn sm" type="button" disabled={i.currency !== txn.currency} title={i.currency !== txn.currency ? "Cross-currency allocation is not supported manually" : ""}>Add</button></td></tr>
        )) : <tr><td className="muted">No open invoices found</td></tr>}</tbody></table>
      </div>
      {rows.length > 0 && (
        <table style={{ marginBottom: 8 }}><thead><tr><th>Invoice</th><th className="num">Open</th><th>Apply ₹</th>{adv && <><th>TDS ₹</th><th>Write-off ₹</th></>}<th /></tr></thead>
          <tbody>{rows.map((r, i) => (
            <tr key={r.inv.id}><td>{r.inv.number}</td><td className="num">{money(r.inv.open_minor, r.inv.currency)}</td>
              <td><input aria-label={`Apply to ${r.inv.number}`} value={r.applied} onChange={(e) => set(i, "applied", e.target.value)} inputMode="decimal" style={{ width: 110 }} /></td>
              {adv && <><td><input aria-label="TDS" value={r.tds} onChange={(e) => set(i, "tds", e.target.value)} style={{ width: 90 }} inputMode="decimal" /></td><td><input aria-label="Write-off" value={r.wo} onChange={(e) => set(i, "wo", e.target.value)} style={{ width: 90 }} inputMode="decimal" /></td></>}
              <td><button className="btn sm" type="button" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove">✕</button></td></tr>
          ))}</tbody></table>
      )}
      <div className="row between mb"><label style={{ margin: 0 }}><input type="checkbox" checked={adv} onChange={(e) => setAdv(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />Show TDS / write-off</label>
        <span className={remaining < 0 ? "chip bad" : remaining ? "chip warn" : "chip good"}>{remaining < 0 ? `Over-allocated by ${money(-remaining, txn.currency)}` : remaining ? `${money(remaining, txn.currency)} will be held on account` : "Fully allocated"}</span></div>
      <Field label="Reason (audited)" hint="Write-offs beyond tolerance, high-value or multi-customer allocations need a second person's approval."><textarea value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
      <div className="row between"><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !rows.length || remaining < 0 || bad || reason.trim().length < 3} onClick={submit}>Apply allocation</button></div>
    </Modal>
  );
}
