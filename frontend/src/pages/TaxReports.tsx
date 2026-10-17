import { useRef, useState } from "react";
import { api, download } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { compact, date, money, pct } from "../format";
import { Chip, Empty, ErrorBox, Field, Loading, PageHead, Tabs } from "../components/ui";

type Tab = "register" | "26as" | "gst" | "ageing";

export default function TaxReports() {
  const [tab, setTab] = useState<Tab>("ageing");
  return (
    <>
      <PageHead title="Tax & reports" sub="Receivables ageing and provision, TDS certificates, Form 26AS reconciliation and GST filing checks." />
      <Tabs value={tab} onChange={setTab} tabs={[["ageing", "Ageing & provision"], ["register", "TDS register"], ["26as", "26AS reconciliation"], ["gst", "GST filing checks"]]} />
      {tab === "ageing" && <Ageing />}
      {tab === "register" && <Register />}
      {tab === "26as" && <As26 />}
      {tab === "gst" && <Gst />}
    </>
  );
}

const BUCKETS = ["not_due", "1-30", "31-60", "61-90", "91-180", "181-365", "365+"];
const BL: Record<string, string> = { not_due: "Not due" };

function Ageing() {
  const { entity, toast } = useApp();
  const [asOf, setAsOf] = useState("");
  const r = useAsync(() => api.get<any>(`/reports/ageing?entity_id=${entity.id}${asOf ? `&as_of=${asOf}` : ""}`), [entity.id, asOf]);
  if (r.error) return <ErrorBox msg={r.error} />;
  if (!r.data) return <Loading />;
  const d = r.data, t = d.totals;
  return (
    <>
      <div className="toolbar">
        <label className="row" style={{ textTransform: "none" }}>As of <input type="date" value={asOf} max={new Date().toISOString().slice(0, 10)} onChange={(e) => setAsOf(e.target.value)} style={{ width: "auto" }} /></label>
        <Chip tone="brand">Net exposure {compact(t.net_minor)}</Chip><Chip tone={t.provision_minor ? "warn" : "good"}>Provision {compact(t.provision_minor)} ({d.coverage_pct}%)</Chip>
        <button className="btn" onClick={() => download(`/reports/ageing?entity_id=${entity.id}&format=csv${asOf ? `&as_of=${asOf}` : ""}`, "ageing.csv").catch((e) => toast(e.message, true))}>Export CSV</button>
      </div>
      {d.missing_rates.length > 0 && <div className="note warn mb">No exchange rate for {d.missing_rates.join(", ")} on this date: those invoices are left out of these totals.</div>}
      <div className="card flush">
        {!d.rows.length ? <Empty>No open receivables at this date.</Empty> :
          <table style={{ fontSize: 12.5 }}><thead><tr><th style={{ minWidth: 190 }}>Customer</th>{BUCKETS.map((b) => <th key={b} className="num">{BL[b] ?? b}</th>)}<th className="num">Credits</th><th className="num">Net</th><th className="num">Provision</th></tr></thead>
            <tbody>{d.rows.map((x: any) => (
              <tr key={x.customer_id}><td><b>{x.customer}</b>{x.disputed_minor > 0 && <> <Chip tone="warn">disputed {compact(x.disputed_minor)}</Chip></>}<div className="faint">{x.code} · oldest {x.oldest_days_overdue > 0 ? `${x.oldest_days_overdue}d overdue` : "not overdue"}</div></td>
                {BUCKETS.map((b) => <td key={b} className="num">{x[b] ? money(x[b]) : <span className="faint">–</span>}</td>)}
                <td className="num">{x.credits_minor + x.advances_minor ? `−${money(x.credits_minor + x.advances_minor)}` : "–"}</td><td className="num"><b>{money(x.net_minor)}</b></td><td className="num">{x.provision_minor ? money(x.provision_minor) : "–"}</td></tr>))}
              <tr><td><b>Total</b></td>{BUCKETS.map((b) => <td key={b} className="num"><b>{money(t[b])}</b></td>)}<td className="num">−{money(t.credits_minor + t.advances_minor)}</td><td className="num"><b>{money(t.net_minor)}</b></td><td className="num"><b>{money(t.provision_minor)}</b></td></tr></tbody></table>}
      </div>
      <div className="card mt"><h3>Provision matrix in use</h3>
        <div className="row wrap gap-lg">{d.matrix.map((m: any) => <span key={m.from_days}><b>{pct(m.pct_bp / 10000, m.pct_bp % 100 ? 1 : 0)}</b> <span className="muted">from {m.from_days} days overdue</span></span>)}<span><b>{pct(d.disputed_bp / 10000)}</b> <span className="muted">minimum on disputed invoices</span></span></div>
        <p className="faint" style={{ marginBottom: 0 }}>{d.note} Change the matrix in Settings.</p></div>
    </>
  );
}

function Register() {
  const { entity, toast } = useApp();
  const [fy, setFy] = useState("");
  const r = useAsync(() => api.get<any>(`/reports/tds-register?entity_id=${entity.id}${fy ? `&fy=${fy}` : ""}`), [entity.id, fy]);
  if (r.error) return <ErrorBox msg={r.error} />;
  if (!r.data) return <Loading />;
  const d = r.data, fys = [...new Set<string>(d.rows.map((x: any) => x.fy))];
  return (
    <>
      <div className="toolbar">
        <select value={fy} onChange={(e) => setFy(e.target.value)} aria-label="Financial year" style={{ width: "auto" }}><option value="">All years</option>{(fys.length ? fys : fy ? [fy] : []).map((f) => <option key={f}>{f}</option>)}</select>
        <Chip tone="brand">TDS credit {compact(d.totals.amount_minor)}</Chip><Chip tone={d.overdue_minor ? "bad" : "good"}>{d.overdue_minor ? `${compact(d.overdue_minor)} overdue from ${d.overdue_customers} customer(s)` : "No certificates overdue"}</Chip>
        <button className="btn" onClick={() => download(`/reports/tds-register?entity_id=${entity.id}&format=csv${fy ? `&fy=${fy}` : ""}`, "tds_register.csv").catch((e) => toast(e.message, true))}>Export CSV</button>
      </div>
      <div className="card flush">
        {!d.rows.length ? <Empty>No TDS credit recorded yet.</Empty> :
          <table><thead><tr><th>FY / Qtr</th><th>Customer</th><th>Section</th><th className="num">TDS</th><th className="num">Certificate pending</th><th className="num">Not in 26AS</th><th>Form 16A due</th></tr></thead>
            <tbody>{d.rows.map((x: any, i: number) => (
              <tr key={i}><td>{x.fy} {x.quarter}</td><td><b>{x.customer}</b><div className="faint">{x.code}{x.pan ? ` · ${x.pan}` : ""}</div></td><td>{x.section || "—"}</td><td className="num">{money(x.amount_minor)}</td>
                <td className="num">{x.pending_minor ? <b>{money(x.pending_minor)}</b> : "–"}</td><td className="num">{x.not_in_26as_minor ? money(x.not_in_26as_minor) : "–"}</td>
                <td>{date(x.certificate_due)} {x.overdue && <Chip tone="bad">{x.days_overdue}d overdue</Chip>}</td></tr>))}</tbody></table>}
      </div>
      <p className="faint mt">{d.note}</p>
    </>
  );
}

const ST: Record<string, [string, "good" | "warn" | "bad" | "info" | ""]> = {
  matched: ["Matches", "good"], timing: ["Quarter timing", "info"], ours_higher: ["We claimed more", "bad"], not_reported: ["Not reported", "bad"], reported_higher: ["Statement is higher", "warn"],
};

function As26() {
  const { entity, can } = useApp();
  const fyNow = (() => { const t = new Date(); const y = t.getMonth() >= 3 ? t.getFullYear() : t.getFullYear() - 1; return `${y}-${String(y + 1).slice(2)}`; })();
  const [fy, setFy] = useState(fyNow); const [source, setSource] = useState("26AS");
  const file = useRef<HTMLInputElement>(null); const [res, setRes] = useState<any>(null);
  const r = useAsync(() => api.get<any>(`/tax/26as/reconciliation?entity_id=${entity.id}&fy=${fy}&source=${source}`), [entity.id, fy, source]);
  const custs = useAsync(() => api.get<any>(`/customers?entity_id=${entity.id}&limit=500`), [entity.id]);
  const { run, busy } = useAction();
  const [pick, setPick] = useState<Record<string, string>>({});
  const upload = async () => {
    const f = file.current?.files?.[0]; if (!f) return;
    const fd = new FormData(); fd.append("entity_id", String(entity.id)); fd.append("source", source); fd.append("file", f);
    const x = await run(() => api.upload<any>("/tax/26as/import", fd), (x: any) => `${x.inserted} new, ${x.updated} updated, ${x.unchanged} unchanged`);
    if (x) { setRes(x); r.reload(); }
  };
  const canAct = can("ar_analyst", "ar_manager");
  return (
    <>
      <div className="card mb">
        <div className="row wrap">
          <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source" style={{ width: "auto" }}><option value="26AS">Form 26AS</option><option value="AIS">AIS (CSV / XLSX)</option></select>
          <input value={fy} onChange={(e) => setFy(e.target.value)} aria-label="Financial year" style={{ width: 110 }} placeholder="2026-27" />
          {canAct && <><input ref={file} type="file" accept=".txt,.csv,.xlsx" style={{ width: "auto" }} aria-label="Statement file" /><button className="btn primary" disabled={busy} onClick={upload}>Import statement</button></>}
        </div>
        <p className="faint" style={{ marginBottom: 0, fontSize: 12.5 }}>Download Form 26AS (text) from TRACES, or export the TDS section of AIS as CSV/XLSX. The reader follows the published layout and has not been run on a real file yet: check the counts after your first import.</p>
        {res?.mapped_notes?.length > 0 && res.mapped_notes.map((n: string, i: number) => <div key={i} className="note warn">{n}</div>)}
      </div>
      {r.error && <ErrorBox msg={r.error} />}
      {!r.data ? <Loading /> : <>
        <div className="toolbar"><Chip tone="brand">We took credit for {compact(r.data.ours_minor)}</Chip><Chip tone="good">Statement confirms {compact(r.data.reported_minor)}</Chip>
          {r.data.at_risk_minor > 0 && <Chip tone="bad">At risk {compact(r.data.at_risk_minor)}</Chip>}{r.data.unclaimed_minor > 0 && <Chip tone="warn">Possibly unclaimed {compact(r.data.unclaimed_minor)}</Chip>}
          {canAct && r.data.rows.length > 0 && <button className="btn" disabled={busy} onClick={() => run(() => api.post<any>(`/tax/26as/reconciliation/apply?entity_id=${entity.id}&fy=${fy}&source=${source}`), (x: any) => `${x.flagged} entries marked as seen in 26AS`).then(r.reload)}>Mark confirmed entries</button>}</div>
        <div className="card flush">
          {!r.data.rows.length ? <Empty>Nothing to compare for {fy}. Import a statement, or record TDS on matched receipts first.</Empty> :
            <table><thead><tr><th>Customer</th><th>Qtr</th><th className="num">Ours</th><th className="num">Statement (final)</th><th className="num">Provisional</th><th className="num">Difference</th><th>Status</th></tr></thead>
              <tbody>{r.data.rows.map((x: any, i: number) => (
                <tr key={i}><td><b>{x.customer}</b><div className="faint mono">{x.tan ?? "TAN unknown"}</div></td><td>{x.quarter}</td><td className="num">{money(x.ours_minor)}</td><td className="num">{money(x.reported_minor)}</td><td className="num">{x.unconfirmed_minor ? money(x.unconfirmed_minor) : "–"}</td>
                  <td className="num">{x.diff_minor ? money(x.diff_minor, { sign: true }) : "–"}</td><td><Chip tone={ST[x.status][1]}>{ST[x.status][0]}</Chip>{x.action && <div className="faint" style={{ maxWidth: 360 }}>{x.action}</div>}</td></tr>))}</tbody></table>}
        </div>
        {r.data.unmapped.length > 0 && <div className="card mt"><h3>Deductors we could not match to a customer</h3>
          {r.data.unmapped.map((u: any) => (
            <div key={u.tan} className="row between wrap" style={{ padding: "4px 0" }}><span><b>{u.name}</b> <span className="mono faint">{u.tan}</span> · {money(u.amount_minor)}</span>
              {canAct && <span className="row"><select value={pick[u.tan] ?? ""} onChange={(e) => setPick({ ...pick, [u.tan]: e.target.value })} aria-label={`Customer for ${u.tan}`} style={{ width: 240 }}><option value="">Choose customer…</option>{(custs.data?.items ?? []).map((c: any) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
                <button className="btn sm" disabled={!pick[u.tan] || busy} onClick={() => run(() => api.post<any>("/tax/26as/map", { entity_id: entity.id, tan: u.tan, customer_id: Number(pick[u.tan]) }), (x: any) => `Linked ${x.entries_linked} lines`).then(r.reload)}>Link</button></span>}</div>))}</div>}
        <p className="faint mt">{r.data.note}</p>
      </>}
    </>
  );
}

const GF: Record<string, string> = { not_in_gstr1: "Not in GSTR-1", gstin_mismatch: "GSTIN differs", value_mismatch: "Value differs", irn_cancelled: "IRN cancelled", irn_missing: "No IRN", irn_mismatch: "IRN differs", not_on_irp: "Not on IRP register" };

function Gst() {
  const { entity, can } = useApp();
  const r = useAsync(() => api.get<any>(`/tax/gst/crosscheck?entity_id=${entity.id}`), [entity.id]);
  const file = useRef<HTMLInputElement>(null); const [source, setSource] = useState("GSTR1");
  const { run, busy } = useAction();
  const upload = async () => {
    const f = file.current?.files?.[0]; if (!f) return;
    const fd = new FormData(); fd.append("entity_id", String(entity.id)); fd.append("source", source); fd.append("file", f);
    if (await run(() => api.upload<any>("/tax/gst/import", fd), (x: any) => `${x.inserted} new, ${x.updated} updated, ${x.unchanged} unchanged`)) r.reload();
  };
  return (
    <>
      {can("ar_analyst", "ar_manager", "treasury") && <div className="card mb"><div className="row wrap">
        <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source" style={{ width: "auto" }}><option value="GSTR1">GSTR-1 JSON</option><option value="IRP">e-invoice register (CSV / XLSX)</option></select>
        <input ref={file} type="file" accept=".json,.csv,.xlsx" style={{ width: "auto" }} aria-label="File" /><button className="btn primary" disabled={busy} onClick={upload}>Import</button></div>
        <p className="faint" style={{ marginBottom: 0, fontSize: 12.5 }}>A customer often cannot claim input credit, and so will not pay, until your invoice is in their GSTR-2B. Import each filed GSTR-1 and your IRP register to see which open invoices are not reported or have a problem. Formats follow the published layouts and have not been run on real downloads.</p></div>}
      {r.error && <ErrorBox msg={r.error} />}
      {!r.data ? <Loading /> : <>
        <div className="toolbar"><Chip>{r.data.checked} open items checked</Chip><Chip tone={r.data.flagged ? "bad" : "good"}>{r.data.flagged} with issues ({compact(r.data.open_flagged_minor)} open)</Chip>
          <Chip>GSTR-1 imported: {r.data.gstr1_periods.length ? r.data.gstr1_periods.join(", ") : "none"}</Chip><Chip>IRP documents: {r.data.irp_documents}</Chip></div>
        <div className="card flush">
          {!r.data.rows.length ? <Empty>{r.data.gstr1_periods.length || r.data.irp_documents ? "No issues found against what has been imported." : "Import a GSTR-1 or the e-invoice register to start checking."}</Empty> :
            <table><thead><tr><th>Invoice</th><th>Customer</th><th className="num">Open</th><th>Issues</th></tr></thead>
              <tbody>{r.data.rows.map((x: any) => (
                <tr key={x.invoice_id}><td><b>{x.number}</b><div className="faint">{date(x.invoice_date)}</div></td><td>{x.customer}<div className="faint mono">{x.gstin}</div></td><td className="num">{money(x.open_minor)}</td>
                  <td>{x.flags.map((f: string, i: number) => <div key={f}><Chip tone={x.severity === "high" ? "bad" : "warn"}>{GF[f] ?? f}</Chip> <span className="faint">{x.issues[i]}</span></div>)}</td></tr>))}</tbody></table>}
        </div>
        <p className="faint mt">{r.data.note}</p>
      </>}
    </>
  );
}
