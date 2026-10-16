import { useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { compact, date, money, rateLabel } from "../format";
import { Chip, Empty, ErrorBox, Loading, PageHead } from "../components/ui";

export default function Tds() {
  const { entity, can } = useApp();
  const [cert, setCert] = useState("pending");
  const r = useAsync(() => api.get<any>(`/tds?entity_id=${entity.id}&certificate=${cert}&limit=500`), [entity.id, cert]);
  const { run } = useAction();
  const byCust = new Map<string, number>();
  r.data?.items.forEach((t: any) => byCust.set(t.customer.name, (byCust.get(t.customer.name) ?? 0) + t.amount_minor));
  const top = [...byCust.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5);
  const set = (id: number, body: object) => run(() => api.post(`/tds/${id}/status`, body)).then(() => r.reload());
  return (
    <>
      <PageHead title="TDS tracker" sub="Tax your customers withheld. Each entry needs a Form 16A / TRACES certificate and must appear in Form 26AS before you can claim the credit." />
      <div className="toolbar">{[["pending", "Awaiting certificate"], ["received", "Certificate received"], ["", "All"]].map(([k, l]) => <button key={k} className={`btn ${cert === k ? "primary" : ""}`} onClick={() => setCert(k)}>{l}</button>)}
        {r.data && <Chip tone="brand">Total {money(r.data.total_amount_minor)}</Chip>}</div>
      {r.error && <ErrorBox msg={r.error} />}
      {top.length > 0 && <div className="card mb"><h3>Largest balances to chase</h3><div className="row wrap gap-lg">{top.map(([n, v]) => <span key={n}><b>{compact(v)}</b> <span className="muted">{n}</span></span>)}</div></div>}
      <div className="card flush">
        {!r.data ? <Loading /> : !r.data.items.length ? <Empty>No TDS entries here. They are created automatically when a match with a TDS short-pay is accepted.</Empty> : (
          <table><thead><tr><th>Date</th><th>Customer</th><th>Invoice</th><th>FY / Qtr</th><th>Section</th><th className="num">Rate</th><th className="num">TDS</th><th>16A received</th><th>In 26AS</th></tr></thead>
            <tbody>{r.data.items.map((t: any) => (
              <tr key={t.id}><td>{date(t.txn_date)}</td><td>{t.customer.name}</td><td>{t.invoice}</td><td>{t.fy} {t.quarter}</td><td>{t.section ?? "—"}</td><td className="num">{rateLabel(t.rate_bp)}</td><td className="num">{money(t.amount_minor)}</td>
                <td><input type="checkbox" style={{ width: "auto" }} checked={t.certificate_received} disabled={!can("ar_analyst", "ar_manager")} aria-label="Certificate received" onChange={(e) => set(t.id, { certificate_received: e.target.checked })} /></td>
                <td><input type="checkbox" style={{ width: "auto" }} checked={t.in_26as} disabled={!can("ar_analyst", "ar_manager")} aria-label="In 26AS" onChange={(e) => set(t.id, { in_26as: e.target.checked })} /></td></tr>
            ))}</tbody></table>
        )}
      </div>
    </>
  );
}
