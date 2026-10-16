import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { compact, date, label, money, pct, plural } from "../format";
import { Chip, Empty, ErrorBox, Loading, PageHead } from "../components/ui";
import { Columns, HBars, Legend } from "../components/charts";
import { Link } from "react-router-dom";

export default function Dashboard() {
  const { entity, can, toast } = useApp();
  const s = useAsync(() => api.get<any>(`/analytics/summary?entity_id=${entity.id}`), [entity.id]);
  const trend = useAsync(() => api.get<any[]>(`/analytics/trend?entity_id=${entity.id}&days=30`), [entity.id]);
  const cash = useAsync(() => api.get<any[]>(`/analytics/cash-position?entity_id=${entity.id}`), [entity.id]);
  const fc = useAsync(() => api.get<any>(`/analytics/forecast?entity_id=${entity.id}&weeks=8`), [entity.id]);
  const { run, busy } = useAction();

  if (s.error) return <ErrorBox msg={s.error} />;
  if (!s.data) return <Loading rows={8} />;
  const d = s.data;
  const mt = d.matching_30d;
  const empty = d.unapplied.count === 0 && d.receivables.open_minor === 0;

  return (
    <>
      <PageHead title="Dashboard" sub={`${entity.name} · as of ${date(d.as_of)}`}>
        {can("ar_analyst", "ar_manager", "treasury") && <button className="btn primary" disabled={busy} onClick={() => run(() => api.post<any>("/matching/run", { entity_id: entity.id }), (r) => `Matched ${r.processed} receipts: ${r.auto} auto, ${r.review} to review, ${r.exception} exceptions`)}>Run matching</button>}
        {can("admin") && <button className="btn" disabled={busy} onClick={() => { if (confirm("Replace ALL data with the synthetic demo tenant?")) run(() => api.post("/demo/seed"), "Demo data loaded — sign in again to refresh roles"); }}>Reset demo data</button>}
      </PageHead>
      {d.missing_rates?.length > 0 && <div className="note bad mb" role="alert"><b>Missing exchange rates for {d.missing_rates.join(", ")}.</b> Those amounts are excluded from every total on this page (never added 1:1). Add rates under Settings → FX rates.</div>}
      {d.stale_rates?.length > 0 && <div className="note warn mb" role="alert"><b>Exchange rates are out of date:</b> {d.stale_rates.map((x: any) => `${x.currency} (${x.latest ? `${x.age_days}d old` : "none"})`).join(", ")}. Realised-FX postings and conversions use the latest rate; <Link to="/connections">fetch or import current rates</Link>.</div>}
      {empty && <div className="card mb"><Empty>No data yet. Go to <Link to="/import">Import</Link> to load open invoices and a bank statement{can("admin") ? ", or use “Reset demo data”" : ""}.</Empty></div>}

      <div className="grid g4 mb">
        <div className="card kpi"><span className="l">Unapplied cash</span><span className="v">{compact(d.unapplied.amount_minor)}</span><span className="s">{d.unapplied.count} receipts waiting · <Link to="/receipts">view</Link></span></div>
        <div className="card kpi"><span className="l">Straight-through rate (30d)</span><span className="v">{pct(mt.stp_rate)}</span><span className="s">{mt.auto_applied} auto · {mt.human_accepted} human · {mt.auto_reversed} auto reversed</span></div>
        <div className="card kpi"><span className="l">Open receivables</span><span className="v">{compact(d.receivables.open_minor)}</span><span className="s">{compact(d.receivables.overdue_minor)} overdue · DSO {d.receivables.dso_days ?? "—"}d</span></div>
        <div className="card kpi"><span className="l">TDS awaiting certificate</span><span className="v">{compact(d.tds.awaiting_certificate_minor)}</span><span className="s"><Link to="/tds">Form 16A / 26AS tracker</Link></span></div>
      </div>
      <div className="grid g3 mb">
        <Link to="/review" className="card kpi" style={{ color: "inherit" }}><span className="l">Matches awaiting review</span><span className="v">{d.queues.review}</span><span className="s">Oldest-confidence first</span></Link>
        <Link to="/exceptions" className="card kpi" style={{ color: "inherit" }}><span className="l">Open exceptions</span><span className="v">{d.queues.exceptions}</span><span className="s">{d.queues.exceptions_overdue_sla ? <span style={{ color: "var(--bad)" }}>{d.queues.exceptions_overdue_sla} past SLA</span> : "All within SLA"}</span></Link>
        <Link to="/approvals" className="card kpi" style={{ color: "inherit" }}><span className="l">Pending approvals</span><span className="v">{d.queues.approvals_pending}</span><span className="s">Write-offs, adjustments, refunds</span></Link>
      </div>

      {d.foreign_exposure?.length > 0 && <div className="card mb"><div className="row between wrap"><h2 style={{ margin: 0 }}>Foreign-currency receivables</h2><span className="faint" style={{ fontSize: 12 }}>base-currency equivalents at the latest rate</span></div>
        <div className="row wrap gap-lg mt">{d.foreign_exposure.map((f: any) => <span key={f.currency}><b className="num">{money(f.open_minor, f.currency)}</b> <span className="muted">≈ {compact(f.base_equivalent_minor)}</span></span>)}</div></div>}
      <div className="grid g2 mb">
        <div className="card">
          <h2>Unapplied cash by age</h2>
          <Columns money data={d.unapplied.ageing.map((a: any) => ({ label: a.bucket, cash: a.amount_minor }))} series={[{ key: "cash", label: "Unapplied", color: "var(--c1)" }]} />
        </div>
        <div className="card">
          <h2>Receivables ageing (by days overdue)</h2>
          <Columns money data={d.receivables.ageing.map((a: any) => ({ label: a.bucket, ar: a.amount_minor }))} series={[{ key: "ar", label: "Open AR", color: "var(--c2)" }]} />
        </div>
      </div>

      <div className="grid g2 mb">
        <div className="card">
          <div className="row between"><h2>Matches per day (30d)</h2><Legend series={[{ key: "auto", label: "Straight-through", color: "var(--c1)" }, { key: "review", label: "Human review", color: "var(--c3)" }, { key: "manual", label: "Manual", color: "var(--c4)" }]} /></div>
          {trend.data && trend.data.length ? <Columns data={trend.data.slice(-14).map((t) => ({ ...t, label: t.date.slice(5) }))} series={[{ key: "auto", label: "Straight-through", color: "var(--c1)" }, { key: "review", label: "Human review", color: "var(--c3)" }, { key: "manual", label: "Manual", color: "var(--c4)" }]} /> : <Empty>No matches yet</Empty>}
        </div>
        <div className="card">
          <h2>Open exceptions by type</h2>
          {d.exceptions_by_type.length ? <HBars money rows={d.exceptions_by_type.map((e: any) => ({ label: label(e.type), value: e.amount_minor, sub: plural(e.count, "case") }))} /> : <Empty>Nothing open 🎉</Empty>}
        </div>
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>Cash position</h2>
          {cash.data ? (
            <table><thead><tr><th>Account</th><th className="num">Last balance</th><th className="num">Credits (30d)</th></tr></thead>
              <tbody>{cash.data.map((c) => <tr key={c.bank_account_id}><td>{c.name}<div className="faint" style={{ fontSize: 12 }}>{c.balance_as_of ? `as of ${date(c.balance_as_of)}` : "no balance in statements"}</div></td><td className="num">{c.balance_minor === null ? "—" : money(c.balance_minor)}</td><td className="num">{compact(c.credits_30d_minor)}</td></tr>)}</tbody></table>
          ) : <Loading />}
          <p className="faint" style={{ fontSize: 12, marginBottom: 0 }}>Balances are the last running balance on imported statements, not a live bank feed.</p>
        </div>
        <div className="card">
          <h2>Expected collections (next 8 weeks)</h2>
          {fc.data ? <>
            <Columns money data={fc.data.weeks.map((w: any, i: number) => ({ label: i === fc.data.weeks.length - 1 ? "8w+" : `W${i}`, due: w.amount_minor }))} series={[{ key: "due", label: "Expected", color: "var(--c2)" }]} />
            <p className="faint" style={{ fontSize: 12, marginBottom: 0 }}>Method: {fc.data.method}. A transparent heuristic, not a statistical forecast. W0 includes everything already due.</p>
          </> : <Loading />}
        </div>
      </div>
    </>
  );
}
