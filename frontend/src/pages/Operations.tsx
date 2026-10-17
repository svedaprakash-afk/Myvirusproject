import { useEffect, useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { date } from "../format";
import { Chip, ErrorBox, Field, Loading } from "../components/ui";

/** Settings > Operations: period close, alerts, ERP connector options and how people sign in. */
export default function Operations() {
  const { can } = useApp();
  const editable = can("admin", "finance_head");
  return (
    <div className="grid g2">
      <PeriodLock editable={editable} />
      <Access />
      <Notifications editable={editable} />
      <ErpOptions editable={editable} />
    </div>
  );
}

function PeriodLock({ editable }: { editable: boolean }) {
  const { entity } = useApp();
  const r = useAsync(() => api.get<any>(`/periods/lock?entity_id=${entity.id}`), [entity.id]);
  const [d, setD] = useState(""); const [note, setNote] = useState(""); const { run, busy } = useAction();
  if (r.error) return <ErrorBox msg={r.error} />;
  if (!r.data) return <div className="card"><Loading rows={3} /></div>;
  const cur: string | null = r.data.locked_through;
  const reopening = !!cur && (d === "" || d < cur);
  const save = async (through: string | null) => { if (await run(() => api.post("/periods/lock", { entity_id: entity.id, locked_through: through, note }), through ? "Books closed" : "Lock removed")) { setNote(""); r.reload(); } };
  return (
    <div className="card">
      <h2>Period close <Chip tone={cur ? "warn" : ""}>{cur ? `closed through ${date(cur)}` : "open"}</Chip></h2>
      <p className="muted">Receipts and adjustments dated on or before the closing date are posted on the first open day instead, with the original date in the narration. Nothing is posted into a closed period.</p>
      <div className="row wrap">
        <input type="date" value={d} max={new Date().toISOString().slice(0, 10)} onChange={(e) => setD(e.target.value)} disabled={!editable} aria-label="Close books through" style={{ width: "auto" }} />
        <button className="btn primary" disabled={!editable || busy || !d || (reopening && note.trim().length < 5)} onClick={() => save(d)}>{reopening ? "Move the lock earlier" : "Close the books"}</button>
        {cur && <button className="btn" disabled={!editable || busy || note.trim().length < 5} onClick={() => save(null)}>Remove lock</button>}
      </div>
      {(reopening || cur) && <Field label="Reason" hint="Required to re-open a closed period. Recorded in the audit trail and alerted."><input value={note} onChange={(e) => setNote(e.target.value)} /></Field>}
      {!editable && <div className="note warn">Only a finance head or administrator can close or re-open a period.</div>}
    </div>
  );
}

function Access() {
  const sso = useAsync(() => api.get<any>("/auth/sso/config"), []);
  return (
    <div className="card">
      <h2>How people sign in</h2>
      <table><tbody>
        <tr><td className="muted">Single sign-on (OpenID Connect)</td><td>{sso.data?.enabled ? <Chip tone="good">on</Chip> : <Chip>not configured</Chip>}{sso.data?.enforce && <> <Chip tone="warn">passwords off except admin</Chip></>}</td></tr>
        <tr><td className="muted">SCIM provisioning</td><td><span className="mono">/scim/v2</span> <span className="faint">(on when the server has SCIM_TOKEN)</span></td></tr>
      </tbody></table>
      <p className="muted">Sign-in and provisioning are set by the operator through environment variables so that secrets never live in the database: <span className="mono">SSO_ISSUER</span>, <span className="mono">SSO_CLIENT_ID</span>, <span className="mono">SSO_CLIENT_SECRET</span>, <span className="mono">SSO_REDIRECT_URI</span>, <span className="mono">SSO_ROLE_MAP</span>, <span className="mono">SCIM_TOKEN</span>. SAML is not supported: most SAML-only providers can expose an OIDC application. Run one real sign-in with each provider before relying on it.</p>
      <p className="faint" style={{ marginBottom: 0 }}>SCIM can create people as an analyst, manager, treasury or auditor only. Finance-head and admin roles are never granted by an identity provider.</p>
    </div>
  );
}

const CHANNELS: [string, string][] = [["email", "Email"], ["slack", "Slack"], ["teams", "Microsoft Teams"]];

function Notifications({ editable }: { editable: boolean }) {
  const { entity } = useApp();
  const r = useAsync(() => api.get<any>(`/entities/${entity.id}/notifications`), [entity.id]);
  const out = useAsync(() => api.get<any[]>(`/notifications?entity_id=${entity.id}&limit=12`), [entity.id]);
  const [f, setF] = useState<any>(null); const { run, busy } = useAction();
  const key = JSON.stringify(r.data?.config);
  useEffect(() => { if (r.data) setF(JSON.parse(JSON.stringify(r.data.config))); }, [key]); // eslint-disable-line
  if (r.error) return <ErrorBox msg={r.error} />;
  if (!r.data || !f) return <div className="card"><Loading rows={4} /></div>;
  const events: Record<string, string> = r.data.events;
  const set = (ch: string, patch: object | null) => setF((p: any) => { const n = { ...p }; if (patch === null) delete n[ch]; else n[ch] = { ...(n[ch] ?? {}), ...patch }; return n; });
  const toggleEv = (ch: string, ev: string, on: boolean) => { const cur: string[] = f[ch].events ?? Object.keys(events); set(ch, { events: on ? [...new Set([...cur, ev])] : cur.filter((x) => x !== ev) }); };
  return (
    <div className="card" style={{ gridColumn: "1 / -1" }}>
      <h2>Alerts</h2>
      <p className="muted">Approvals waiting, exceptions past due, model problems, failed or unconfirmed posting, reversed straight-through matches and re-opened periods. Webhook addresses and mail passwords are read from environment variables you name here; they are never stored.</p>
      <div className="grid g3">
        {CHANNELS.map(([ch, label]) => (
          <div key={ch} className="card" style={{ boxShadow: "none" }}>
            <label className="row" style={{ textTransform: "none", fontSize: 14, color: "var(--text)" }}><input type="checkbox" style={{ width: "auto" }} checked={!!f[ch]} disabled={!editable} onChange={(e) => set(ch, e.target.checked ? (ch === "email" ? { port: 587, starttls: true, to: [] } : { webhook_env: "" }) : null)} /><b>{label}</b></label>
            {f[ch] && <>
              {ch === "email" ? <>
                <Field label="SMTP host"><input value={f[ch].host ?? ""} disabled={!editable} onChange={(e) => set(ch, { host: e.target.value })} /></Field>
                <Field label="From"><input value={f[ch].from ?? ""} disabled={!editable} onChange={(e) => set(ch, { from: e.target.value })} /></Field>
                <Field label="To (comma separated)"><input value={(f[ch].to ?? []).join(", ")} disabled={!editable} onChange={(e) => set(ch, { to: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} /></Field>
                <Field label="User / password variable names"><div className="row"><input placeholder="SMTP_USER" value={f[ch].username_env ?? ""} disabled={!editable} onChange={(e) => set(ch, { username_env: e.target.value })} /><input placeholder="SMTP_PASS" value={f[ch].password_env ?? ""} disabled={!editable} onChange={(e) => set(ch, { password_env: e.target.value })} /></div></Field>
              </> : <Field label="Variable that holds the webhook URL"><input placeholder={ch === "slack" ? "SLACK_WEBHOOK_URL" : "TEAMS_WEBHOOK_URL"} value={f[ch].webhook_env ?? ""} disabled={!editable} onChange={(e) => set(ch, { webhook_env: e.target.value })} /></Field>}
              <div className="field"><label>Send</label>{Object.entries(events).map(([ev, desc]) => (
                <label key={ev} className="row" style={{ textTransform: "none", fontSize: 12.5, color: "var(--text)" }}><input type="checkbox" style={{ width: "auto" }} disabled={!editable} checked={(f[ch].events ?? Object.keys(events)).includes(ev)} onChange={(e) => toggleEv(ch, ev, e.target.checked)} />{desc}</label>))}</div>
              <button className="btn sm" disabled={!editable || busy || JSON.stringify(f) !== key} title={JSON.stringify(f) !== key ? "Save first" : ""} onClick={() => run(() => api.post<any>(`/entities/${entity.id}/notifications/test?channel=${ch}`), (x: any) => x.status === "sent" ? "Test alert delivered" : `Not delivered: ${x.error}`).then(out.reload)}>Send a test</button>
            </>}
          </div>))}
      </div>
      <div className="row mt"><button className="btn primary" disabled={!editable || busy || JSON.stringify(f) === key} onClick={() => run(() => api.put(`/entities/${entity.id}/notifications`, f), "Saved and audited").then(r.reload)}>Save alert settings</button></div>
      {out.data && out.data.length > 0 && <><h3 className="mt">Recent alerts</h3>
        <table><tbody>{out.data.map((n: any) => <tr key={n.id}><td className="mono">{n.created_at.slice(5, 16).replace("T", " ")}</td><td><Chip>{n.channel}</Chip></td><td>{n.subject}</td><td><Chip tone={n.status === "sent" ? "good" : n.status === "failed" ? "bad" : "warn"}>{n.status}</Chip>{n.error && <span className="faint"> {n.error}</span>}</td></tr>)}</tbody></table></>}
    </div>
  );
}

const ERP_FIELDS: Record<string, { label: string; fields: [string, string, string?][] }> = {
  tally_prime: { label: "Tally Prime", fields: [["company", "Company name in Tally"], ["party_by", "Customer ledger is named by", "name"]] },
  sap_s4: { label: "SAP", fields: [["company_code", "Company code"], ["doc_type", "Document type", "DZ"], ["username", "Posting user", "CASHAPP"]] },
  d365_bc: { label: "Business Central", fields: [["journal", "Journal name"], ["company_id", "Company id (optional)"]] },
  netsuite: { label: "NetSuite", fields: [["subsidiary", "Subsidiary"]] },
};

function ErpOptions({ editable }: { editable: boolean }) {
  const { entity } = useApp();
  const r = useAsync(() => api.get<any>(`/entities/${entity.id}/integrations`), [entity.id]);
  const [f, setF] = useState<any>(null); const { run, busy } = useAction();
  const key = JSON.stringify(r.data?.erp_options);
  useEffect(() => { if (r.data) setF(JSON.parse(JSON.stringify(r.data.erp_options ?? {}))); }, [key]); // eslint-disable-line
  if (!r.data || !f) return <div className="card"><Loading rows={3} /></div>;
  const set = (c: string, k: string, v: string) => setF((p: any) => ({ ...p, [c]: { ...(p[c] ?? {}), [k]: v } }));
  return (
    <div className="card">
      <h2>ERP connector settings</h2>
      <p className="muted">Renderers for these ERPs were written from each vendor's published import format and have <b>not been imported into a real ERP</b>. Run the first export against a test company. Fill in only the ERPs you use.</p>
      {Object.entries(ERP_FIELDS).map(([c, d]) => (
        <div key={c} className="mb"><h3>{d.label} {(r.data.connectors ?? []).find((x: any) => x.name === c)?.status === "available" ? <Chip tone="good">ready</Chip> : <Chip>needs settings</Chip>}</h3>
          <div className="grid g2">{d.fields.map(([k, l, ph]) => <Field key={k} label={l}><input value={f[c]?.[k] ?? ""} placeholder={ph} disabled={!editable} onChange={(e) => set(c, k, e.target.value)} /></Field>)}</div></div>))}
      <button className="btn primary" disabled={!editable || busy || JSON.stringify(f) === key} onClick={() => run(() => api.put(`/entities/${entity.id}/integrations`, { erp_options: Object.fromEntries(Object.entries(f).map(([c, o]: any) => [c, Object.fromEntries(Object.entries(o).filter(([, v]) => String(v).trim() !== ""))]).filter(([, o]: any) => Object.keys(o).length)) }), "Saved and audited").then(r.reload)}>Save connector settings</button>
    </div>
  );
}
