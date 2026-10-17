import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { date, money, pct } from "../format";
import { Chip, Empty, ErrorBox, Field, Loading, PageHead, StatusChip, Tabs } from "../components/ui";

type Tab = "banks" | "erp" | "inbox" | "fx" | "residency";

export default function Connections() {
  const [tab, setTab] = useState<Tab>("banks");
  return (
    <>
      <PageHead title="Connections" sub="Bank-agnostic and ERP-agnostic by configuration: describe a bank's file layout or an ERP's import format once, preview it, save it. No code change." />
      <Tabs value={tab} onChange={setTab} tabs={[["banks", "Banks"], ["erp", "ERP"], ["inbox", "Remittance inbox"], ["fx", "FX rates"], ["residency", "Data residency"]]} />
      {tab === "banks" && <Banks />}{tab === "erp" && <Erp />}{tab === "inbox" && <Inbox />}{tab === "fx" && <Fx />}{tab === "residency" && <Residency />}
    </>
  );
}

function Json({ label, value, onChange, hint, rows = 10 }: { label: string; value: string; onChange: (v: string) => void; hint?: string; rows?: number }) {
  let err = ""; try { value.trim() && JSON.parse(value); } catch (e) { err = (e as Error).message; }
  return <Field label={label} hint={hint}><textarea className="mono" style={{ minHeight: rows * 19, width: "100%" }} value={value} spellCheck={false} onChange={(e) => onChange(e.target.value)} aria-invalid={!!err} />{err && <div style={{ color: "var(--bad)", fontSize: 12 }}>Not valid JSON: {err}</div>}</Field>;
}
const pretty = (o: unknown) => JSON.stringify(o ?? {}, null, 2);
const parse = (s: string) => (s.trim() ? JSON.parse(s) : {});

const PROFILE_TEMPLATES: Record<string, object> = {
  "Auto-detect (default)": {},
  "Semicolon CSV, dd.mm.yyyy, 1 preamble row": { format: "csv", delimiter: ";", skip_rows: 1, date_format: "%d.%m.%Y", columns: { date: "Posting Dt", narration: "Particulars", debit: "Debit", credit: "Credit", balance: "Bal" } },
  "Single signed amount column": { format: "csv", columns: { date: "Date", narration: "Description", amount: "Amount", balance: "Balance" } },
  "USD account": { currency: "USD" },
  "Custom narration rule": { narration_rules: [{ name: "my-bank", pattern: "TXN(?P<utr>\\d+)\\*PAYER:(?P<payer>[^*]+)\\*REF:(?P<ref>\\S+)", channel: "NEFT", utr: "utr", payer: "payer" }] },
};
const FEED_TEMPLATES: Record<string, object> = {
  "None (manual upload)": {},
  "Folder drop (SFTP landing zone)": { type: "folder_drop", path: "hdfc", pattern: "*.csv", archive: true },
  "SFTP host-to-host (pinned host key)": { type: "sftp_native", host: "sftp.yourbank.example", port: 22, username: "corp-user", remote_dir: "/outbound", pattern: "*.sta", archive_dir: "/outbound/processed", host_key_sha256: "SHA256:<fingerprint from your bank>", auth: { password_env: "BANK_SFTP_PASSWORD" } },
  "Generic REST/JSON API": { type: "rest_json", url: "https://api.yourbank.example/v1/statements", auth: { token_env: "BANK_API_TOKEN" }, params: { from: "{since}" }, records_path: "data.transactions", date_format: "%Y-%m-%d", lookback_days: 7, credit_values: ["CR"], map: { date: "txnDate", amount: "amount", direction: "type", narration: "remarks", ref: "utr", balance: "balance" } },
};

function Banks() {
  const { entity, can, toast } = useApp();
  const banks = useAsync(() => api.get<any[]>(`/entities/${entity.id}/bank-accounts`), [entity.id]);
  const [id, setId] = useState<number | null>(null);
  const bid = id ?? banks.data?.[0]?.id;
  const setup = useAsync(() => (bid ? api.get<any>(`/bank-accounts/${bid}/setup`) : Promise.resolve(null)), [bid]);
  const [tplNote, setTplNote] = useState(""), [prof, setProf] = useState(""), [feed, setFeed] = useState(""), [pv, setPv] = useState<any>(null), [sync, setSync] = useState<any>(null);
  const file = useRef<HTMLInputElement>(null);
  const tpls = useAsync(() => api.get<any[]>("/bank-templates"), []);
  const { run, busy } = useAction();
  // Reset the editors only when the SAVED data actually changes (app-wide refreshes re-fetch identical data and must not wipe unsaved edits).
  const setupKey = JSON.stringify(setup.data);
  useEffect(() => { if (setup.data) { setProf(pretty(setup.data.profile)); setFeed(pretty(setup.data.feed)); setPv(null); setSync(null); } }, [setupKey, bid]); // eslint-disable-line
  const cfgOk = can("admin", "finance_head"), ingOk = can("ar_analyst", "ar_manager", "treasury");

  async function preview(useProfile: boolean) {
    const f = file.current?.files?.[0];
    if (!f) return toast("Choose a sample statement file first", true);
    const fd = new FormData(); fd.append("bank_account_id", String(bid)); fd.append("file", f);
    if (useProfile) fd.append("profile_json", prof || "{}");
    try { setPv(await api.upload<any>("/ingest/preview", fd)); } catch (e) { setPv(null); toast((e as Error).message, true); }
  }
  if (!banks.data) return <Loading />;
  return (
    <div className="grid g-main">
      <div className="grid">
        <div className="card">
          <Field label="Bank account"><select value={bid ?? ""} onChange={(e) => setId(Number(e.target.value))}>{banks.data.map((b) => <option key={b.id} value={b.id}>{b.bank_name} · {b.currency}</option>)}</select></Field>
          <h2>1 · Statement profile — how to read this bank's files</h2>
          <div className="row mb"><select aria-label="Profile template" onChange={(e) => { const v = e.target.value; if (!v) return; const t = tpls.data?.find((x) => x.name === v); setProf(pretty(t ? t.profile : PROFILE_TEMPLATES[v])); setTplNote(t?.note ?? ""); }} value=""><option value="">Insert a template…</option>
      <optgroup label="Banks (written from memory — verify with Preview)">{tpls.data?.map((t) => <option key={t.name}>{t.name}</option>)}</optgroup>
      <optgroup label="Patterns">{Object.keys(PROFILE_TEMPLATES).map((k) => <option key={k}>{k}</option>)}</optgroup></select></div>
          {tplNote && <div className="note warn">{tplNote} <b>Unverified against a real file — run Preview.</b></div>}
          <Json label="Profile (JSON)" value={prof} onChange={setProf} rows={11} hint="Keys: format, delimiter, encoding, skip_rows, columns{date,narration,debit,credit,amount,drcr,balance,ref,value_date}, date_format, currency, invert_direction, narration_rules[{pattern,utr,payer,ifsc,vpa,channel,kind}]" />
          <Field label="Sample statement file"><input ref={file} type="file" accept=".csv,.xlsx,.xml,.txt,.sta" /></Field>
          <div className="row wrap"><button className="btn" disabled={busy} onClick={() => preview(false)}>Detect columns</button><button className="btn" disabled={busy} onClick={() => preview(true)}>Preview with this profile</button>
            <button className="btn primary" disabled={busy || !(cfgOk || ingOk)} onClick={() => run(() => api.put(`/bank-accounts/${bid}/profile`, { profile: parse(prof) }), "Profile saved").then(setup.reload)}>Save profile</button></div>
          <p className="faint" style={{ fontSize: 12, marginBottom: 0 }}>Preview never writes anything. Saving is audited.</p>
        </div>
        <div className="card">
          <h2>2 · Feed — how statements arrive</h2>
          <div className="row mb"><select aria-label="Feed template" onChange={(e) => e.target.value && setFeed(pretty(FEED_TEMPLATES[e.target.value]))} value=""><option value="">Insert a template…</option>{Object.keys(FEED_TEMPLATES).map((k) => <option key={k}>{k}</option>)}</select></div>
          <Json label="Feed (JSON)" value={feed} onChange={setFeed} rows={9} hint="Credentials are never stored here: name an environment variable (token_env) on the server instead." />
          <div className="row wrap"><button className="btn primary" disabled={busy || !cfgOk} onClick={() => run(() => api.put(`/bank-accounts/${bid}/feed`, { feed: parse(feed) }), "Feed saved").then(setup.reload)}>Save feed</button>
            <button className="btn" disabled={busy || !ingOk || !setup.data?.feed?.type} onClick={async () => { const r = await run(() => api.post<any>(`/bank-accounts/${bid}/sync`), (r) => `Synced: ${r.inserted} new lines`); if (r) setSync(r); }}>Sync now</button></div>
          {!cfgOk && <div className="note warn mt">Only a finance head or administrator can change feeds.</div>}
          {sync && <div className="pre mt">{JSON.stringify(sync, null, 2)}</div>}
          <h3 className="mt">Feed types</h3>{setup.data?.feed_types.map((f: any) => <div key={f.type} className="row between"><span>{f.label}</span><Chip tone={f.status === "available" ? "good" : ""}>{f.status}</Chip></div>)}
        </div>
      </div>
      <div className="card">
        <h2>Preview</h2>
        {!pv ? <Empty>Upload a sample and press Preview to see exactly how it would be read.</Empty> : <>
          <div className="row wrap gap-lg mb"><span><b>{pv.rows_total}</b> rows ({pv.format})</span><span>{pv.credits} credits · {pv.debits} debits</span>
            <Chip tone={pv.utr_found_pct >= 80 ? "good" : "warn"}>UTR found {pv.utr_found_pct ?? "—"}%</Chip><Chip tone={pv.payer_found_pct >= 80 ? "good" : "warn"}>payer found {pv.payer_found_pct ?? "—"}%</Chip></div>
          <div style={{ overflowX: "auto" }}><table><thead><tr><th>Date</th><th>Dr/Cr</th><th className="num">Amount</th><th>Parsed</th></tr></thead>
            <tbody>{pv.sample.map((r: any, i: number) => <tr key={i}><td style={{ whiteSpace: "nowrap" }}>{r.date}</td><td>{r.direction}</td><td className="num">{money(r.amount_minor, r.currency)}</td>
              <td><div className="mono" style={{ fontSize: 11.5, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={r.narration}>{r.narration}</div>
                <div className="row wrap" style={{ gap: 4, marginTop: 3 }}><Chip tone="info">{r.parsed.channel}</Chip>{r.parsed.utr && <Chip>UTR {r.parsed.utr.slice(0, 14)}</Chip>}{r.parsed.payer && <Chip tone="good">{r.parsed.payer}</Chip>}{r.parsed.kind !== "customer" && <Chip tone="warn">{r.parsed.kind}</Chip>}{r.parsed.rule && <Chip tone="brand">rule: {r.parsed.rule}</Chip>}</div></td></tr>)}</tbody></table></div>
          {pv.suggested?.profile && <><h3 className="mt">Suggested profile</h3><div className="pre">{pretty(pv.suggested.profile)}</div><button className="btn mt" onClick={() => setProf(pretty(pv.suggested.profile))}>Use suggestion</button>{pv.suggested.unrecognised_headers?.length > 0 && <p className="faint">Unrecognised headers: {pv.suggested.unrecognised_headers.join(", ")}</p>}</>}
        </>}
      </div>
    </div>
  );
}

const EXPORT_TEMPLATES: Record<string, object> = {
  "Built-in: canonical files": {},
  "Column-mapped CSV (any ERP import)": { type: "mapped_csv", delimiter: ",", decimal: ".", date_format: "%d-%m-%Y", amounts: "document", columns: [{ header: "Voucher No", source: "voucher_no" }, { header: "Date", source: "date" }, { header: "Ledger", source: "account" }, { header: "Party", source: "party_code" }, { header: "Dr/Cr", source: "drcr" }, { header: "Amount", source: "amount" }, { header: "Currency", source: "currency" }, { header: "Rate", source: "exchange_rate" }, { header: "Narration", source: "narration" }, { header: "Company", const: "1000" }] },
  "Signed webhook / REST": { type: "webhook", url: "https://erp.yourcompany.in/api/cash-receipts", secret_env: "ERP_WEBHOOK_SECRET", timeout: 20 },
};
const LEDGERS = ["ar_control", "tds_receivable", "bank_charges", "gst_input_on_fees", "write_off", "unapplied_cash", "intercompany", "fx_gain_loss"];

function Erp() {
  const { entity, can } = useApp();
  const r = useAsync(() => api.get<any>(`/entities/${entity.id}/integrations`), [entity.id]);
  const [exp, setExp] = useState(""), [imp, setImp] = useState(""), [led, setLed] = useState<Record<string, string>>({});
  const { run, busy } = useAction();
  const rKey = JSON.stringify(r.data);
  useEffect(() => { if (r.data) { setExp(pretty(r.data.erp_export)); setImp(pretty(r.data.erp_import)); setLed(r.data.ledgers ?? {}); } }, [rKey]); // eslint-disable-line
  if (!r.data) return <Loading />;
  const ok = can("admin", "finance_head");
  return (
    <div className="grid g2">
      <div className="card">
        <h2>Outbound: how cash postings reach your ERP</h2>
        <div className="row mb"><select aria-label="Export template" onChange={(e) => e.target.value && setExp(pretty(EXPORT_TEMPLATES[e.target.value]))} value=""><option value="">Insert a template…</option>{Object.keys(EXPORT_TEMPLATES).map((k) => <option key={k}>{k}</option>)}</select></div>
        <Json label="Export configuration (JSON)" value={exp} onChange={setExp} rows={14} hint="mapped_csv: any column layout, delimiter, decimal and date format, account map. webhook: HMAC-SHA256 signed POST; a reply of {documents:{voucher:erpDocNo}} acknowledges automatically." />
        <h3>Connectors for this entity</h3>{r.data.connectors.map((c: any) => <div key={c.name} className="row between"><span>{c.label}</span><Chip tone={c.status === "available" ? "good" : ""}>{c.status}</Chip></div>)}
      </div>
      <div className="card">
        <h2>Ledger codes</h2><p className="muted" style={{ marginTop: 0 }}>Map each logical account to the code your ERP uses.</p>
        <div className="grid g2">{LEDGERS.map((k) => <Field key={k} label={k.replace(/_/g, " ")}><input value={led[k] ?? ""} onChange={(e) => setLed({ ...led, [k]: e.target.value })} disabled={!ok} /></Field>)}</div>
        <h2 className="mt">Inbound: reading your ERP's open-items export</h2>
        <Json label="Import mapping (JSON)" value={imp} onChange={setImp} rows={9} hint='{"delimiter":";","date_format":"%d.%m.%Y","columns":{"invoice_no":"Belegnr","customer_code":"Kunde","total":"Betrag","open":"Offen","currency":"Währung","booking_rate":"Kurs"}}' />
        {!ok && <div className="note warn">Only a finance head or administrator can change integrations.</div>}
        <button className="btn primary" disabled={!ok || busy} onClick={() => run(() => api.put(`/entities/${entity.id}/integrations`, { erp_export: parse(exp), erp_import: parse(imp), ledgers: led }), "Saved and audited").then(r.reload)}>Save</button>
      </div>
    </div>
  );
}

function Inbox() {
  const { entity, can } = useApp();
  const r = useAsync(() => api.get<any>(`/entities/${entity.id}/integrations`), [entity.id]);
  const q = useAsync(() => api.get<any[]>(`/remittances?entity_id=${entity.id}`), [entity.id]);
  const [cfg, setCfg] = useState(""); const { run, busy } = useAction(); const file = useRef<HTMLInputElement>(null);
  const mKey = JSON.stringify(r.data?.mailbox);
  useEffect(() => { if (r.data) setCfg(pretty(Object.keys(r.data.mailbox).length ? r.data.mailbox : { host: "imap.yourcompany.in", port: 993, username: "remittances@yourcompany.in", password_env: "MAILBOX_PASSWORD", folder: "INBOX", processed_folder: "Processed", allowed_domains: [], require_auth_pass: true })); }, [mKey]); // eslint-disable-line
  if (!r.data) return <Loading />;
  const ok = can("admin", "finance_head"), ing = can("ar_analyst", "ar_manager", "treasury");
  const quarantined = (q.data ?? []).filter((x) => x.extractor === "quarantined");
  return (
    <div className="grid g-main">
      <div className="card">
        <h2>Remittance inbox (IMAP)</h2>
        <Json label="Mailbox (JSON)" value={cfg} onChange={setCfg} rows={11} hint="The password is read from the server environment variable named in password_env. Leave allowed_domains empty to accept any sender; unauthenticated mail (no DKIM/DMARC pass from YOUR mail server) is quarantined." />
        <div className="row wrap"><button className="btn primary" disabled={!ok || busy} onClick={() => run(() => api.put(`/entities/${entity.id}/integrations`, { mailbox: parse(cfg) }), "Saved").then(r.reload)}>Save</button>
          <button className="btn" disabled={!ing || busy || !Object.keys(r.data.mailbox).length} onClick={() => run(() => api.post<any>(`/entities/${entity.id}/mailbox/poll`), (x) => `Polled: ${x.ingested} ingested, ${x.quarantined} quarantined, ${x.errors.length} errors`).then(q.reload)}>Poll now</button></div>
        <h3 className="mt">…or drop a single .eml</h3>
        <div className="row"><input type="file" accept=".eml" ref={file} /><button className="btn" disabled={!ing || busy} onClick={() => { const f = file.current?.files?.[0]; if (!f) return; const fd = new FormData(); fd.append("entity_id", String(entity.id)); fd.append("file", f); run(() => api.upload<any>("/ingest/email", fd), (x) => `Email ${x.status}`).then(q.reload); }}>Ingest</button></div>
      </div>
      <div className="card">
        <h2>Quarantine <Chip tone={quarantined.length ? "warn" : "good"}>{quarantined.length}</Chip></h2>
        <p className="muted" style={{ marginTop: 0 }}>Mail that failed sender authentication or the domain allow-list. Nothing from it influences matching until a person releases it.</p>
        {!quarantined.length ? <Empty>Nothing quarantined.</Empty> : quarantined.map((x) => (
          <div key={x.id} className="note warn"><div className="row between"><b>{x.sender}</b><button className="btn sm" disabled={!can("ar_analyst", "ar_manager") || busy} onClick={() => run(() => api.post(`/remittances/${x.id}/release`), "Released and extracted").then(q.reload)}>Release</button></div>
            <div>{x.subject}</div><div className="faint" style={{ fontSize: 12 }}>{x.reasons?.join("; ")} · {x.auth_result}</div></div>))}
        <h3 className="mt">Recent advices</h3>
        {(q.data ?? []).filter((x) => x.extractor !== "quarantined").slice(0, 8).map((x) => <div key={x.id} className="row between" style={{ fontSize: 13 }}><span>{x.subject || x.source}<span className="faint"> · {x.sender ?? "manual"}</span></span>{x.linked_txn_id ? <Chip tone="good">linked #{x.linked_txn_id}</Chip> : <Chip>unlinked</Chip>}</div>)}
      </div>
    </div>
  );
}

function Fx() {
  const { can, toast } = useApp();
  const r = useAsync(() => api.get<any[]>(`/fx-rates?limit=60`), []);
  const feed = useAsync(() => api.get<any>(`/fx-rates/feed`), []);
  const [text, setText] = useState(""); const { run, busy } = useAction(); const file = useRef<HTMLInputElement>(null); const [res, setRes] = useState<any>(null);
  const ok = can("treasury", "finance_head", "admin");
  const submit = () => {
    const rows = text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const [currency, rate_date, rate] = l.split(/[,\s]+/); return { currency, rate_date, rate }; });
    run(() => api.post<any>("/fx-rates", rows), (x) => `${x.upserted} rates saved`).then(() => { setText(""); r.reload(); });
  };
  const upload = async () => {
    const f = file.current?.files?.[0]; if (!f) return toast("Choose the file downloaded from FBIL first", true);
    const fd = new FormData(); fd.append("file", f);
    const x = await run(() => api.upload<any>("/fx-rates/import", fd), (x) => `${x.stored} rates stored, ${x.rejected.length} held for review`);
    if (x) { setRes(x); r.reload(); feed.reload(); }
  };
  const f = feed.data;
  return (
    <div className="grid g2">
      <div className="grid">
        <div className="card"><h2>Reference-rate feed</h2>
          <p className="muted" style={{ marginTop: 0 }}>The “RBI reference rate” is computed and published by <b>FBIL</b> (Financial Benchmarks India) on weekdays, around 1 pm IST. The worker fetches it daily after 2 pm IST and retries until it succeeds.</p>
          {!f ? <Loading rows={2} /> : <table><tbody>
            <tr><td className="muted">Source</td><td>{f.source}</td></tr>
            <tr><td className="muted">Feed URL host</td><td>{f.configured ? <>{f.host} {f.egress_allowed === false && <Chip tone="bad">blocked by residency allow-list</Chip>}</> : <Chip tone="warn">not configured (set FX_FEED_URL)</Chip>}</td></tr>
            <tr><td className="muted">Published by FBIL</td><td>{f.direct_currencies.map((c: string) => <Chip key={c} tone="good">{c}</Chip>)}</td></tr>
            {f.pegged_currencies?.length > 0 && <tr><td className="muted">Pegged to USD</td><td>{f.pegged_currencies.map((c: string) => <Chip key={c} tone="warn">{c}</Chip>)}<div className="faint" style={{ fontSize: 12, marginTop: 3 }}>= FBIL USD/INR ÷ the configured peg (FX_PEGS, AED 3.6725). Exact while the peg holds; not a market quote.</div></td></tr>}
            <tr><td className="muted">Derived (cross rates)</td><td>{f.derived_currencies.length ? f.derived_currencies.map((c: string) => <Chip key={c} tone="warn">{c}</Chip>) : "—"}
              <div className="faint" style={{ fontSize: 12, marginTop: 3 }}>{f.cross_configured ? <>= FBIL EUR/INR ÷ ECB EUR/currency from {f.cross_host}{f.cross_egress_allowed === false && <Chip tone="bad">blocked by residency allow-list</Chip>}. Indicative, <b>not</b> an official reference rate.</> : <>Not configured (set FX_CROSS_FEED_URL) — enter these by hand or import a file.</>}</div></td></tr>
            <tr><td className="muted">Last run</td><td>{f.last_run ? <><StatusChip s={f.last_run.status === "ok" ? "resolved" : "failed"} /> {f.last_run.slot} {f.last_run.detail?.error && <span style={{ color: "var(--bad)" }}>{f.last_run.detail.error}</span>}</> : <span className="muted">never</span>}</td></tr></tbody></table>}
          <div className="row mt"><button className="btn primary" disabled={!ok || busy || !f?.configured} onClick={() => run(() => api.post<any>("/fx-rates/fetch"), (x) => `${x.stored} stored, ${x.rejected.length} held for review`).then((x) => { x && setRes(x); r.reload(); feed.reload(); })}>Fetch now</button></div>
          <div className="note warn mt"><b>Unverified:</b> FBIL's exact URL and file layout could not be confirmed when this was built. After pointing FX_FEED_URL at the live publication, run “Fetch now” once and check the rates below against fbil.org.in. Anything moving more than 5% in a day is held for review, never stored silently.</div>
        </div>
        <div className="card"><h2>Or upload the file you downloaded</h2>
          <input type="file" ref={file} accept=".csv,.txt,.xlsx,.html,.htm" /><div className="row mt"><button className="btn" disabled={!ok || busy} onClick={upload}>Import file</button></div>
          {res && <div className="mt"><b>{res.stored}</b> stored of {res.parsed} parsed.{res.rejected?.map((x: string, i: number) => <div key={i} className="note bad">{x}</div>)}{res.skipped?.length > 0 && <div className="faint" style={{ fontSize: 12 }}>Skipped lines: {res.skipped.length}</div>}</div>}
        </div>
        <div className="card"><h2>Add rates manually</h2>
          <Field label="One per line: CURRENCY, YYYY-MM-DD, rate" hint="₹ per 1 unit (e.g. USD, 2026-10-15, 83.4521)."><textarea className="mono" value={text} onChange={(e) => setText(e.target.value)} placeholder={"USD, 2026-10-15, 83.4521"} /></Field>
          <button className="btn primary" disabled={!ok || busy || !text.trim()} onClick={submit}>Save rates</button>{!ok && <div className="note warn mt">Treasury, finance heads and admins maintain FX rates.</div>}</div>
      </div>
      <div className="card flush">{!r.data ? <Loading /> : <table><thead><tr><th>Date</th><th>Currency</th><th className="num">₹ per unit</th><th>Source</th></tr></thead><tbody>{r.data.map((x) => <tr key={x.id}><td>{date(x.rate_date)}</td><td>{x.currency}</td><td className="num">{x.rate.toFixed(4)}</td><td className="muted">{x.source}</td></tr>)}</tbody></table>}</div>
    </div>
  );
}

function StorageCard() {
  const { can, toast } = useApp();
  const st = useAsync(() => api.get<any>("/storage"), []);
  const [probe, setProbe] = useState<any>(null); const { run, busy } = useAction();
  if (!st.data) return <Loading rows={3} />;
  const s = st.data;
  return (
    <div className="card" style={{ maxWidth: 760, marginTop: 14 }}>
      <h2>Object storage <Chip tone={s.problems.length ? "bad" : "good"}>{s.backend}</Chip></h2>
      <table><tbody>
        {s.backend === "s3" ? <>
          <tr><td className="muted">Bucket</td><td className="mono">{s.bucket}/{s.prefix}</td></tr>
          <tr><td className="muted">Region / endpoint</td><td>{s.region ?? "—"} {s.endpoint_host && <span className="muted">· {s.endpoint_host}</span>}</td></tr>
          <tr><td className="muted">Encryption</td><td>{s.encryption}{s.kms_key && <Chip tone="good">KMS key set</Chip>}</td></tr>
          <tr><td className="muted">Export retention</td><td>{s.object_lock ? <><Chip tone={s.object_lock.enabled === false ? "bad" : "good"}>{s.object_lock.mode}</Chip> <span className="muted">{s.object_lock.retention_days} days{s.object_lock.enabled === false ? " — the bucket does not have Object Lock enabled, so exports will be refused" : ""}</span></> : <span className="muted">none (set S3_OBJECT_LOCK_MODE and S3_RETENTION_DAYS for write-once exports)</span>}</td></tr></> : <>
          <tr><td className="muted">Exports directory</td><td className="mono">{s.exports_dir}</td></tr><tr><td className="muted">Landing zone</td><td className="mono">{s.feeds_dir}</td></tr></>}
      </tbody></table>
      {s.problems.map((p: string, i: number) => <div key={i} className="note bad">{p}</div>)}
      <div className="row mt"><button className="btn" disabled={busy || !can("admin", "finance_head", "treasury")} onClick={async () => { setProbe(null); const x = await run(() => api.post<any>("/storage/test"), "Storage test passed"); if (x) setProbe(x); }}>Test storage</button></div>
      {probe && <div className="mt">{["exports", "feeds"].map((a) => <div key={a} className="row between"><span>{a}: write → read → verify → delete</span><Chip tone={probe[a].ok ? "good" : "bad"}>{probe[a].ok ? `OK · ${probe[a].ms} ms` : "FAILED"}</Chip></div>)}</div>}
      {s.backend === "local" && <div className="note warn mt">Local storage means files live on this host/volume. For several replicas, or to survive node loss, use S3-compatible object storage (<span className="mono">STORAGE_BACKEND=s3</span>).</div>}
    </div>
  );
}

function Residency() {
  const { entity } = useApp();
  const r = useAsync(() => api.get<any>(`/entities/${entity.id}/integrations`), [entity.id]);
  if (!r.data) return <Loading />;
  const x = r.data.residency;
  return (<>
    <div className="card" style={{ maxWidth: 760 }}>
      <h2>Data residency</h2>
      <table><tbody>
        <tr><td className="muted">Strict mode</td><td>{x.strict ? <Chip tone="good">ON — outbound calls limited to the allow-list</Chip> : <Chip tone="warn">OFF — development default</Chip>}</td></tr>
        <tr><td className="muted">Deployment region</td><td>{x.region ?? "not declared"} {x.region && (x.indian_region ? <Chip tone="good">India</Chip> : <Chip tone="bad">not an Indian region</Chip>)}</td></tr>
        <tr><td className="muted">Allowed egress hosts</td><td>{x.allowed_egress.length ? x.allowed_egress.map((h: string) => <Chip key={h}>{h}</Chip>) : <span className="muted">none</span>}</td></tr>
      </tbody></table>
      <p className="muted">These are set by the operator through environment variables (<span className="mono">DATA_RESIDENCY_STRICT</span>, <span className="mono">DEPLOY_REGION</span>, <span className="mono">ALLOWED_EGRESS_HOSTS</span>), not by users. In strict mode the service refuses to start outside an Indian region, and the LLM extractor, webhooks, bank APIs and IMAP can only reach allow-listed hosts.</p>
      <div className="note warn">The software enforces egress and startup rules; it cannot verify where your database, backups and logs physically live. See <span className="mono">docs/DEPLOYMENT_INDIA.md</span>.</div>
    </div>
    <StorageCard />
    </>
  );
}
