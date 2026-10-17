import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { date, money } from "../format";
import { Chip, Empty, ErrorBox, Loading, PageHead } from "../components/ui";

const BASIS: Record<string, string> = { reference: "PO or reference quoted by the payer", oldest_first: "Oldest due invoice first" };

export default function Advances() {
  const { entity, can } = useApp();
  const r = useAsync(() => api.get<any>(`/advances?entity_id=${entity.id}`), [entity.id]);
  const { run, busy } = useAction();
  const canAct = can("ar_analyst", "ar_manager");
  if (r.error) return <ErrorBox msg={r.error} />;
  if (!r.data) return <Loading />;
  const d = r.data;
  return (
    <>
      <PageHead title="On account & advances" sub="Cash already booked to unapplied cash that can settle invoices raised later. Applying it posts Dr unapplied cash / Cr receivables; there is no second bank entry.">
        {canAct && <button className="btn" disabled={busy} onClick={() => run(() => api.post<any>(`/advances/propose?entity_id=${entity.id}`), (x) => `${x.proposed} suggestion(s) created`).then(r.reload)}>Look for new suggestions</button>}
      </PageHead>
      <div className="grid g-main">
        <div className="card flush">
          <div style={{ padding: "12px 16px 0" }}><h2>Suggested applications</h2></div>
          {d.proposals.length === 0 ? <Empty>No suggestions. They appear when an invoice is raised for a customer who has cash on account.</Empty> :
            <table><thead><tr><th>Customer</th><th>Invoice</th><th>Why this invoice</th><th className="num">Apply</th><th /></tr></thead>
              <tbody>{d.proposals.map((a: any) => (
                <tr key={a.id}><td><b>{a.customer.name}</b></td>
                  <td>{a.invoice.number}<div className="faint">due {date(a.invoice.due_date)} · open {money(a.invoice.open_minor, a.invoice.currency)}</div></td>
                  <td><Chip tone={a.basis === "reference" ? "good" : ""}>{BASIS[a.basis] ?? a.basis}</Chip></td>
                  <td className="num"><b>{money(a.amount_minor, a.invoice.currency)}</b>{a.amount_minor < a.invoice.open_minor && <div className="faint">part-settles</div>}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{canAct && <>
                    <button className="btn sm primary" disabled={busy} onClick={() => run(() => api.post(`/advances/${a.id}/accept`), "Applied").then(r.reload)}>Apply</button>{" "}
                    <button className="btn sm" disabled={busy} onClick={() => run(() => api.post(`/advances/${a.id}/reject`, { reason: "" }), "Dismissed").then(r.reload)}>Dismiss</button></>}</td></tr>))}</tbody></table>}
        </div>
        <div className="card flush">
          <div style={{ padding: "12px 16px 0" }}><h2>Cash on account</h2></div>
          {d.balances.length === 0 ? <Empty>Nothing held on account.</Empty> :
            <table><thead><tr><th>Customer</th><th>Received</th><th className="num">Remaining</th></tr></thead>
              <tbody>{d.balances.map((b: any) => (
                <tr key={b.id}><td>{b.customer.name}<div className="faint mono">{b.utr ?? `receipt #${b.txn_id}`}</div></td><td>{date(b.txn_date)}</td>
                  <td className="num">{money(b.remaining_minor, b.currency)}{b.remaining_minor !== b.original_minor && <div className="faint">of {money(b.original_minor, b.currency)}</div>}{b.currency !== entity.base_currency && <div className="faint">foreign: apply by manual match</div>}</td></tr>))}</tbody></table>}
        </div>
      </div>
    </>
  );
}
