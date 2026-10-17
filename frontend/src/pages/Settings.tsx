import { useEffect, useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { date, money, pct, toMinor } from "../format";
import { Chip, ErrorBox, Field, Loading, PageHead, Tabs } from "../components/ui";

const DEFAULT_PROV = [{ from_days: 0, pct_bp: 50 }, { from_days: 31, pct_bp: 200 }, { from_days: 61, pct_bp: 500 }, { from_days: 91, pct_bp: 1500 }, { from_days: 181, pct_bp: 4000 }, { from_days: 366, pct_bp: 10000 }];

export default function Settings() {
  const [tab, setTab] = useState<"controls" | "ai">("controls");
  return (<><PageHead title="Settings & AI" sub="Matching thresholds, control switches and the scoring model lifecycle." /><Tabs value={tab} onChange={setTab} tabs={[["controls", "Controls"], ["ai", "AI model lifecycle"]]} />{tab === "controls" ? <Controls /> : <Models />}</>);
}

function provValid(rows: { d: string; p: string }[]): boolean {
  const r = rows.map((x) => ({ d: parseInt(x.d, 10), p: parseFloat(x.p) }));
  if (r.some((x) => Number.isNaN(x.d) || Number.isNaN(x.p) || x.d < 0 || x.p < 0 || x.p > 100)) return false;
  const s = [...r].sort((a, b) => a.d - b.d);
  return s[0].d === 0 && new Set(s.map((x) => x.d)).size === s.length && s.every((x, i) => i === 0 || x.p >= s[i - 1].p);
}

function Controls() {
  const { entity, can } = useApp();
  const cfg = useAsync(() => api.get<any>(`/entities/${entity.id}/config`), [entity.id]);
  const [f, setF] = useState<any>(null); const { run, busy } = useAction();
  const cKey = JSON.stringify(cfg.data);
  useEffect(() => { if (cfg.data) setF({ ...cfg.data.config, trust: { enabled: false, graduate_after: 5, ...(cfg.data.config.trust_graduation ?? {}) }, prov: (cfg.data.config.provision_matrix ?? DEFAULT_PROV).map((r: any) => ({ d: String(r.from_days), p: String(r.pct_bp / 100) })), dispBp: String(cfg.data.config.provision_disputed_bp ?? 5000), thr194: String((cfg.data.config.tds_194q_threshold_minor ?? 500000000) / 100), einv: !!cfg.data.config.e_invoice_required, gstFreq: cfg.data.config.gstr1_frequency ?? "monthly", tolerance: String((cfg.data.tolerances.find((t: any) => !t.customer_id && !t.currency)?.abs_minor ?? 100) / 100), max: String(cfg.data.config.auto_approve_max_minor / 100) }); }, [cKey]); // eslint-disable-line
  if (cfg.error) return <ErrorBox msg={cfg.error} />;
  if (!f) return <Loading />;
  const editable = can("admin", "finance_head");
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  const tol = toMinor(f.tolerance), mx = toMinor(f.max);
  const invalid = Number.isNaN(tol) || Number.isNaN(mx);
  return (
    <div className="grid g-main">
      <div className="card">
        {!editable && <div className="note warn">Only a finance head or administrator can change controls. Changes are audited.</div>}
        <Field label={`Auto-approve threshold: ${pct(f.auto_approve_threshold)}`} hint="Confidence required before the engine may apply a match without a human. Guardrails apply on top, whatever this is set to.">
          <input type="range" min={0.8} max={0.995} step={0.005} value={f.auto_approve_threshold} disabled={!editable} onChange={(e) => set("auto_approve_threshold", parseFloat(e.target.value))} /></Field>
        <Field label={`Minimum confidence to even suggest: ${pct(f.review_threshold)}`} hint="Below this the receipt becomes an exception with hints instead of a proposal."><input type="range" min={0.2} max={0.7} step={0.05} value={f.review_threshold} disabled={!editable} onChange={(e) => set("review_threshold", parseFloat(e.target.value))} /></Field>
        <div className="grid g2"><Field label="Rounding tolerance (₹)" hint="Differences up to this are auto-written-off"><input value={f.tolerance} disabled={!editable} onChange={(e) => set("tolerance", e.target.value)} inputMode="decimal" /></Field>
          <Field label="Auto-approval ceiling per receipt (₹)" hint="Above this a human always looks"><input value={f.max} disabled={!editable} onChange={(e) => set("max", e.target.value)} inputMode="decimal" /></Field></div>
        <div className="grid g2"><Field label="Exception SLA (hours)"><input type="number" min={1} value={f.sla_hours} disabled={!editable} onChange={(e) => set("sla_hours", Number(e.target.value))} /></Field>
          <Field label="Max invoices per combination"><input type="number" min={2} max={12} value={f.max_subset_size} disabled={!editable} onChange={(e) => set("max_subset_size", Number(e.target.value))} /></Field></div>
        {([["auto_approve_enabled", "Straight-through processing enabled", "Off = suggest-only mode. Recommended for the first weeks of a rollout, while you measure precision."],
          ["auto_approve_cross_currency", "Auto-approve cross-currency settlements when the payer stated the foreign amount", "ON by default. Estimates from the rate table never auto-approve, whatever this is set to. Realised FX is booked automatically."],
          ["auto_approve_amount_only", "Allow auto-approval on exact amount without an invoice reference", "Higher automation, higher risk. Enable only after reviewing precision on your own data."],
          ["straight_through_release", "Release posting batches without a second person", "Removes a segregation-of-duties control. Not recommended."]] as const).map(([k, l, h]) => (
          <div key={k} className="field"><label className="row" style={{ textTransform: "none", fontSize: 13.5, color: "var(--text)" }}><input type="checkbox" style={{ width: "auto" }} checked={!!f[k]} disabled={!editable} onChange={(e) => set(k, e.target.checked)} />{l}</label><div className="faint" style={{ fontSize: 12, marginLeft: 22 }}>{h}</div></div>))}
        <div className="field"><label className="row" style={{ textTransform: "none", fontSize: 13.5, color: "var(--text)" }}><input type="checkbox" style={{ width: "auto" }} checked={!!f.trust.enabled} disabled={!editable} onChange={(e) => set("trust", { ...f.trust, enabled: e.target.checked })} />Each customer must earn straight-through</label>
          <div className="faint" style={{ fontSize: 12, marginLeft: 22 }}>Customers start in a learning phase where every match waits for a person. After this many confirmed matches in a row with no correction they graduate; a rejection, override or reversed auto-match resets them. Managers can pin a customer on their page.</div>
          {f.trust.enabled && <div style={{ marginLeft: 22, marginTop: 6 }}><Field label="Confirmed matches in a row before a customer graduates"><input type="number" min={1} max={50} value={f.trust.graduate_after} disabled={!editable} onChange={(e) => set("trust", { ...f.trust, graduate_after: Number(e.target.value) })} style={{ width: 90 }} /></Field></div>}</div>
        <button className="btn primary" disabled={!editable || busy || invalid || !(f.trust.graduate_after >= 1 && f.trust.graduate_after <= 50)} onClick={() => run(() => api.put(`/entities/${entity.id}/config`, { trust_graduation: { enabled: !!f.trust.enabled, graduate_after: f.trust.graduate_after }, auto_approve_threshold: f.auto_approve_threshold, review_threshold: f.review_threshold, auto_approve_enabled: f.auto_approve_enabled, auto_approve_amount_only: f.auto_approve_amount_only, auto_approve_cross_currency: f.auto_approve_cross_currency, straight_through_release: f.straight_through_release, sla_hours: f.sla_hours, max_subset_size: f.max_subset_size, default_tolerance_minor: tol, auto_approve_max_minor: mx }), "Saved and audited").then(cfg.reload)}>Save changes</button>
      </div>
      <div className="card"><h2>Tax &amp; finance policy</h2>
        <Field label="Expected-credit-loss provision matrix" hint="Share of each overdue bucket you expect not to collect. The defaults are illustrative only: set them from your own loss history and your auditor's view.">
          {f.prov.map((r: any, i: number) => (
            <div key={i} className="row" style={{ marginBottom: 4 }}>
              <input aria-label="Days overdue from" value={r.d} disabled={!editable || i === 0} onChange={(e) => set("prov", f.prov.map((x: any, k: number) => (k === i ? { ...x, d: e.target.value } : x)))} inputMode="numeric" style={{ width: 80 }} /><span className="muted">days overdue →</span>
              <input aria-label="Provision percent" value={r.p} disabled={!editable} onChange={(e) => set("prov", f.prov.map((x: any, k: number) => (k === i ? { ...x, p: e.target.value } : x)))} inputMode="decimal" style={{ width: 80 }} /><span className="muted">%</span>
              {editable && i > 0 && <button className="btn sm" onClick={() => set("prov", f.prov.filter((_: any, k: number) => k !== i))}>Remove</button>}</div>))}
          {editable && f.prov.length < 12 && <button className="btn sm" onClick={() => set("prov", [...f.prov, { d: "", p: "" }])}>Add band</button>}
        </Field>
        <Field label="Minimum provision on disputed invoices (%)"><input value={String(Number(f.dispBp) / 100)} disabled={!editable} onChange={(e) => set("dispBp", String(Math.round(parseFloat(e.target.value || "0") * 100)))} inputMode="decimal" style={{ width: 90 }} /></Field>
        <Field label="Section 194Q threshold (₹)" hint="Purchases from you in a financial year above which a buyer deducts 0.1%. Statutory value: confirm with your tax team."><input value={f.thr194} disabled={!editable} onChange={(e) => set("thr194", e.target.value)} inputMode="decimal" /></Field>
        <div className="field"><label className="row" style={{ textTransform: "none", fontSize: 13.5, color: "var(--text)" }}><input type="checkbox" style={{ width: "auto" }} checked={f.einv} disabled={!editable} onChange={(e) => set("einv", e.target.checked)} />E-invoicing applies to this entity</label><div className="faint" style={{ fontSize: 12, marginLeft: 22 }}>Lets the GST checks flag invoices to registered customers that carry no IRN.</div></div>
        <Field label="GSTR-1 filing"><select value={f.gstFreq} disabled={!editable} onChange={(e) => set("gstFreq", e.target.value)} style={{ width: "auto" }}><option value="monthly">Monthly</option><option value="quarterly">Quarterly (QRMP)</option></select></Field>
        <button className="btn primary" disabled={!editable || busy || !provValid(f.prov) || Number.isNaN(toMinor(f.thr194))} onClick={() => run(() => api.put(`/entities/${entity.id}/config`, { provision_matrix: f.prov.map((r: any) => ({ from_days: parseInt(r.d, 10), pct_bp: Math.round(parseFloat(r.p) * 100) })), provision_disputed_bp: Number(f.dispBp), tds_194q_threshold_minor: toMinor(f.thr194), e_invoice_required: f.einv, gstr1_frequency: f.gstFreq }), "Saved and audited").then(cfg.reload)}>Save policy</button>
      </div>
      <div className="card"><h2>Guardrails (always on)</h2><p className="muted">These are plain code, independent of any model, and cannot be tuned away:</p>
        <ul style={{ paddingLeft: 18, margin: 0 }}>{["Payer must be identified with high certainty and unambiguously", "Payer name must resemble the customer (third-party payers need a human)", "Only one plausible invoice combination", "No short-pay, overpayment or unapplied residual", "No disputed invoice; credit notes only when referenced", "TDS must be at the customer's usual rate and base", "Invoice not already proposed for another receipt", "Currency must match; possible duplicates are stopped"].map((g) => <li key={g}>{g}</li>)}</ul></div>
    </div>
  );
}

function Health() {
  const { entity } = useApp();
  const h = useAsync(() => api.get<any>(`/models/health?entity_id=${entity.id}`), [entity.id]);
  if (!h.data) return null;
  const d = h.data, tone = d.status === "alert" ? "bad" : d.status === "watch" ? "warn" : d.status === "ok" ? "good" : "";
  return (
    <div className="card mb">
      <div className="row between wrap"><h2 style={{ margin: 0 }}>Model health <Chip tone={tone}>{String(d.status).replace("_", " ")}</Chip></h2><span className="faint">last {d.window_days} days vs the {d.baseline_days} before · {d.n_recent} / {d.n_baseline} proposals</span></div>
      {d.alerts.length === 0 ? <p className="muted">No drift detected.</p> : d.alerts.map((a: any, i: number) => <div key={i} className={`note ${a.level === "high" ? "bad" : a.level === "medium" ? "warn" : ""} mt`}>{a.message}</div>)}
      <div className="grid g4 mt" style={{ fontSize: 13 }}>
        <div><div className="faint">Confidence shift (PSI)</div><b className="num">{d.confidence_psi ?? "n/a"}</b></div>
        <div><div className="faint">Straight-through rate</div><b className="num">{d.rates.auto === null ? "n/a" : pct(d.rates.auto)}</b>{d.rates.auto_baseline !== null && <span className="faint"> (was {pct(d.rates.auto_baseline)})</span>}</div>
        <div><div className="faint">Reviewer overturn rate</div><b className="num">{d.overturn.recent.rate === null ? "n/a" : pct(d.overturn.recent.rate)}</b><span className="faint"> of {d.overturn.recent.judged}</span></div>
        <div><div className="faint">Calibration error</div><b className="num">{d.calibration.ece === null ? "n/a" : pct(d.calibration.ece)}</b><span className="faint"> on {d.calibration.n_labelled} labelled</span></div>
      </div>
      {d.feature_shifts.length > 0 && <p className="faint mt" style={{ fontSize: 12.5, marginBottom: 0 }}>Largest feature shifts: {d.feature_shifts.map((x: any) => `${x.feature.replace(/_/g, " ")} ${x.baseline} → ${x.recent}`).join(" · ")}</p>}
      <p className="faint" style={{ fontSize: 12, marginBottom: 0 }}>{d.caveats.join(" ")}</p>
    </div>
  );
}

function ShadowReport() {
  const { entity } = useApp();
  const r = useAsync(() => api.get<any>(`/models/shadow?entity_id=${entity.id}`), [entity.id]);
  if (!r.data || !r.data.shadow) return null;
  const d = r.data, rec = d.recommendation;
  return (
    <div className="card mb">
      <div className="row between wrap"><h2 style={{ margin: 0 }}>Shadow test: {d.shadow}</h2><Chip tone={rec === "promote_candidate" ? "good" : rec === "keep_active" ? "warn" : ""}>{String(rec ?? "").replace(/_/g, " ")}</Chip></div>
      <p className="muted">{d.message} <span className="faint">({d.scored} scored, {d.labelled} labelled by people)</span></p>
      {d.active && <table><thead><tr><th /><th className="num">Log-loss</th><th className="num">Brier</th><th className="num">Would auto-approve</th><th className="num">Precision there</th></tr></thead><tbody>
        {([["Active model", d.active], ["Candidate", d.candidate]] as const).map(([n, x]) => <tr key={n}><td><b>{n}</b></td><td className="num">{x.logloss}</td><td className="num">{x.brier}</td><td className="num">{x.would_auto}</td><td className="num">{x.precision === null ? "n/a" : pct(x.precision)}</td></tr>)}</tbody></table>}
      {d.disagreements && <p className="faint mt" style={{ marginBottom: 0 }}>Candidate would auto-approve {d.disagreements.candidate_would_auto_active_would_not} the active model would not; the reverse for {d.disagreements.active_would_auto_candidate_would_not}. {d.caveats.join(" ")}</p>}
    </div>
  );
}

function Models() {
  const { can, entity } = useApp();
  const m = useAsync(() => api.get<any>(`/models`), []);
  const { run, busy } = useAction();
  const allowed = can("admin", "finance_head");
  if (!m.data) return <Loading />;
  return (
    <>
      <Health />
      <ShadowReport />
      <div className="card mb"><div className="row between wrap"><div><h2 style={{ margin: 0 }}>Analyst feedback collected</h2><div className="muted">{m.data.feedback.total} decisions · {m.data.feedback.positive} accepted · {m.data.feedback.negative} rejected or reversed</div></div>
        <button className="btn primary" disabled={!allowed || busy} onClick={() => run(() => api.post<any>("/models/retrain"), (r) => `Candidate ${r.version} trained (holdout loss ${r.metrics.holdout_logloss_candidate} vs ${r.metrics.holdout_logloss_active} active)`).then(m.reload)}>Train candidate model</button></div>
        <p className="faint" style={{ marginBottom: 0, fontSize: 12.5 }}>A candidate never goes live by itself: you compare held-out loss, then promote it. Any previous version can be re-activated instantly (rollback). Guardrails apply to every model.</p></div>
      <div className="card flush"><table><thead><tr><th>Version</th><th>Status</th><th>Trained</th><th>Holdout log-loss</th><th>Notes</th><th /></tr></thead>
        <tbody>{m.data.items.map((x: any) => (<tr key={x.id}><td><b>{x.version}</b></td><td>{x.active ? <Chip tone="good">active</Chip> : x.shadow ? <Chip tone="info">shadow test</Chip> : <Chip>inactive</Chip>}</td><td>{date(x.created_at)}</td><td>{x.metrics.holdout_logloss_candidate ?? "—"}{x.metrics.holdout_logloss_active !== undefined && <span className="faint"> (was {x.metrics.holdout_logloss_active})</span>}</td><td className="muted">{x.note}</td>
          <td style={{ textAlign: "right" }}>{!x.active && allowed && <span className="row" style={{ justifyContent: "flex-end" }}><button className="btn sm" disabled={busy} onClick={() => run(() => api.post(`/models/${x.id}/shadow`, { enabled: !x.shadow }), x.shadow ? "Shadow test stopped" : "Shadow test started: new proposals are also scored by this model").then(m.reload)}>{x.shadow ? "Stop shadow" : "Run as shadow"}</button><button className="btn sm" disabled={busy} onClick={() => confirm(`Activate ${x.version}?`) && run(() => api.post(`/models/${x.id}/activate`), "Activated").then(m.reload)}>Activate</button></span>}</td></tr>))}</tbody></table></div>
      <div className="card mt"><h2>Active weights</h2>{(() => { const a = m.data.items.find((x: any) => x.active); return a ? <div className="grid g3">{Object.entries(a.weights).map(([k, v]: any) => <div key={k} className="row between"><span className="muted">{k.replace(/_/g, " ")}</span><b className="num" style={{ color: v < 0 ? "var(--bad)" : "inherit" }}>{v.toFixed(2)}</b></div>)}</div> : null; })()}</div>
    </>
  );
}
