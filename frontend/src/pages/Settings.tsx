import { useEffect, useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { date, money, pct, toMinor } from "../format";
import { Chip, ErrorBox, Field, Loading, PageHead, Tabs } from "../components/ui";

export default function Settings() {
  const [tab, setTab] = useState<"controls" | "ai">("controls");
  return (<><PageHead title="Settings & AI" sub="Matching thresholds, control switches and the scoring model lifecycle." /><Tabs value={tab} onChange={setTab} tabs={[["controls", "Controls"], ["ai", "AI model lifecycle"]]} />{tab === "controls" ? <Controls /> : <Models />}</>);
}

function Controls() {
  const { entity, can } = useApp();
  const cfg = useAsync(() => api.get<any>(`/entities/${entity.id}/config`), [entity.id]);
  const [f, setF] = useState<any>(null); const { run, busy } = useAction();
  const cKey = JSON.stringify(cfg.data);
  useEffect(() => { if (cfg.data) setF({ ...cfg.data.config, tolerance: String((cfg.data.tolerances.find((t: any) => !t.customer_id && !t.currency)?.abs_minor ?? 100) / 100), max: String(cfg.data.config.auto_approve_max_minor / 100) }); }, [cKey]); // eslint-disable-line
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
          ["auto_approve_amount_only", "Allow auto-approval on exact amount without an invoice reference", "Higher automation, higher risk. Enable only after reviewing precision on your own data."],
          ["straight_through_release", "Release posting batches without a second person", "Removes a segregation-of-duties control. Not recommended."]] as const).map(([k, l, h]) => (
          <div key={k} className="field"><label className="row" style={{ textTransform: "none", fontSize: 13.5, color: "var(--text)" }}><input type="checkbox" style={{ width: "auto" }} checked={!!f[k]} disabled={!editable} onChange={(e) => set(k, e.target.checked)} />{l}</label><div className="faint" style={{ fontSize: 12, marginLeft: 22 }}>{h}</div></div>))}
        <button className="btn primary" disabled={!editable || busy || invalid} onClick={() => run(() => api.put(`/entities/${entity.id}/config`, { auto_approve_threshold: f.auto_approve_threshold, review_threshold: f.review_threshold, auto_approve_enabled: f.auto_approve_enabled, auto_approve_amount_only: f.auto_approve_amount_only, straight_through_release: f.straight_through_release, sla_hours: f.sla_hours, max_subset_size: f.max_subset_size, default_tolerance_minor: tol, auto_approve_max_minor: mx }), "Saved and audited").then(cfg.reload)}>Save changes</button>
      </div>
      <div className="card"><h2>Guardrails (always on)</h2><p className="muted">These are plain code, independent of any model, and cannot be tuned away:</p>
        <ul style={{ paddingLeft: 18, margin: 0 }}>{["Payer must be identified with high certainty and unambiguously", "Payer name must resemble the customer (third-party payers need a human)", "Only one plausible invoice combination", "No short-pay, overpayment or unapplied residual", "No disputed invoice; credit notes only when referenced", "TDS must be at the customer's usual rate and base", "Invoice not already proposed for another receipt", "Currency must match; possible duplicates are stopped"].map((g) => <li key={g}>{g}</li>)}</ul></div>
    </div>
  );
}

function Models() {
  const { can } = useApp();
  const m = useAsync(() => api.get<any>(`/models`), []);
  const { run, busy } = useAction();
  const allowed = can("admin", "finance_head");
  if (!m.data) return <Loading />;
  return (
    <>
      <div className="card mb"><div className="row between wrap"><div><h2 style={{ margin: 0 }}>Analyst feedback collected</h2><div className="muted">{m.data.feedback.total} decisions · {m.data.feedback.positive} accepted · {m.data.feedback.negative} rejected or reversed</div></div>
        <button className="btn primary" disabled={!allowed || busy} onClick={() => run(() => api.post<any>("/models/retrain"), (r) => `Candidate ${r.version} trained (holdout loss ${r.metrics.holdout_logloss_candidate} vs ${r.metrics.holdout_logloss_active} active)`).then(m.reload)}>Train candidate model</button></div>
        <p className="faint" style={{ marginBottom: 0, fontSize: 12.5 }}>A candidate never goes live by itself: you compare held-out loss, then promote it. Any previous version can be re-activated instantly (rollback). Guardrails apply to every model.</p></div>
      <div className="card flush"><table><thead><tr><th>Version</th><th>Status</th><th>Trained</th><th>Holdout log-loss</th><th>Notes</th><th /></tr></thead>
        <tbody>{m.data.items.map((x: any) => (<tr key={x.id}><td><b>{x.version}</b></td><td>{x.active ? <Chip tone="good">active</Chip> : <Chip>inactive</Chip>}</td><td>{date(x.created_at)}</td><td>{x.metrics.holdout_logloss_candidate ?? "—"}{x.metrics.holdout_logloss_active !== undefined && <span className="faint"> (was {x.metrics.holdout_logloss_active})</span>}</td><td className="muted">{x.note}</td>
          <td style={{ textAlign: "right" }}>{!x.active && allowed && <button className="btn sm" disabled={busy} onClick={() => confirm(`Activate ${x.version}?`) && run(() => api.post(`/models/${x.id}/activate`), "Activated").then(m.reload)}>Activate</button>}</td></tr>))}</tbody></table></div>
      <div className="card mt"><h2>Active weights</h2>{(() => { const a = m.data.items.find((x: any) => x.active); return a ? <div className="grid g3">{Object.entries(a.weights).map(([k, v]: any) => <div key={k} className="row between"><span className="muted">{k.replace(/_/g, " ")}</span><b className="num" style={{ color: v < 0 ? "var(--bad)" : "inherit" }}>{v.toFixed(2)}</b></div>)}</div> : null; })()}</div>
    </>
  );
}
