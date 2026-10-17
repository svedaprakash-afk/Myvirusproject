import { Fragment } from "react";
import { useState } from "react";
import { api, download } from "../api";
import { useApp } from "../ctx";
import { useAsync, useDebounced } from "../hooks";
import { Chip, Empty, ErrorBox, Field, Loading, Modal, PageHead } from "../components/ui";

export default function Audit() {
  const { entity, can, toast } = useApp();
  const [action, setAction] = useState(""); const [actor, setActor] = useState(""); const da = useDebounced(action), dc = useDebounced(actor);
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  const [pack, setPack] = useState(false); const [from, setFrom] = useState(() => iso(new Date(Date.now() - 90 * 864e5))); const [to, setTo] = useState(() => iso(new Date()));
  const [ver, setVer] = useState<any>(null); const [open, setOpen] = useState<number | null>(null);
  const allowed = can("auditor", "finance_head", "admin");
  const r = useAsync(() => (allowed ? api.get<any>(`/audit?entity_id=${entity.id}&limit=200${da ? `&action=${encodeURIComponent(da)}` : ""}${dc ? `&actor=${encodeURIComponent(dc)}` : ""}`) : Promise.resolve(null)), [entity.id, da, dc, allowed]);
  if (!allowed) return <><PageHead title="Audit trail" /><div className="note warn">The audit trail is restricted to auditors, finance heads and administrators.</div></>;
  return (
    <>
      <PageHead title="Audit trail" sub="Append-only and hash-chained: altering or deleting any row breaks verification for everything after it.">
        <button className="btn" onClick={async () => setVer(await api.get("/audit/verify"))}>Verify chain</button>
        <button className="btn" onClick={() => download(`/audit/export?entity_id=${entity.id}`, "audit_log.csv").catch((e) => toast(e.message, true))}>Export CSV</button>
        <button className="btn" title="A zip of the approvals, match decisions, postings, segregation checks, configuration and the audit trail, with a SHA-256 manifest" onClick={() => setPack(true)}>Evidence pack…</button>
      </PageHead>
      <Modal open={pack} onClose={() => setPack(false)} title="Auditor evidence pack">
        <p className="muted">One zip an auditor can re-perform controls from: approvals, match decisions with the model version and the guardrails that applied, posting batches and lines, segregation-of-duties findings, configuration and the hash-chained audit trail. The chain is verified when the pack is built and the result is in the manifest.</p>
        <div className="grid g2"><Field label="From"><input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></Field><Field label="To"><input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></Field></div>
        <div className="row between"><button className="btn" onClick={() => setPack(false)}>Cancel</button>
          <button className="btn primary" disabled={!from || !to || from > to} onClick={async () => { try { await download(`/audit/evidence-pack?entity_id=${entity.id}&date_from=${from}&date_to=${to}`, `evidence_${from}_${to}.zip`); toast("Evidence pack built and downloaded"); setPack(false); } catch (e) { toast((e as Error).message, true); } }}>Build and download</button></div>
      </Modal>
      {ver && <div className={`note ${ver.ok ? "good" : "bad"}`} role="status">{ver.ok ? <>Chain intact — {ver.checked} entries verified. Head <span className="mono">{ver.head?.slice(0, 16)}…</span></> : <>Chain BROKEN at sequence {ver.broken_at_seq}: {ver.reason}</>}</div>}
      <div className="toolbar"><input placeholder="Action prefix, e.g. proposal. or batch." value={action} onChange={(e) => setAction(e.target.value)} aria-label="Action" /><input placeholder="Actor" value={actor} onChange={(e) => setActor(e.target.value)} aria-label="Actor" />{r.data && <span className="muted">{r.data.total} entries</span>}</div>
      {r.error && <ErrorBox msg={r.error} />}
      <div className="card flush">{!r.data ? <Loading rows={8} /> : !r.data.items.length ? <Empty>No entries.</Empty> : (
        <table><thead><tr><th>#</th><th>Time (UTC)</th><th>Actor</th><th>Action</th><th>Object</th></tr></thead>
          <tbody>{r.data.items.map((a: any) => (<Fragment key={a.seq}>
            <tr className="click" onClick={() => setOpen(open === a.seq ? null : a.seq)}><td className="mono">{a.seq}</td><td className="mono">{a.ts.slice(0, 19).replace("T", " ")}</td><td>{a.actor}</td><td><Chip tone={a.action.startsWith("auth.failed") ? "bad" : ""}>{a.action}</Chip></td><td>{a.object_type} #{a.object_id}</td></tr>
            {open === a.seq && <tr><td colSpan={5}><div className="pre">{JSON.stringify(a.detail, null, 2)}{"\n\nhash      " + a.hash + "\nprev_hash " + a.prev_hash}</div></td></tr>}</Fragment>))}</tbody></table>
      )}</div>
    </>
  );
}
