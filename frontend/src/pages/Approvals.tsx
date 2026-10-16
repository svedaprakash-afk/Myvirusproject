import { useState } from "react";
import { api, type Approval } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { ago, money, title } from "../format";
import { Chip, Empty, ErrorBox, Field, Loading, Modal, PageHead, StatusChip } from "../components/ui";

export default function Approvals() {
  const { entity, can } = useApp();
  const [scope, setScope] = useState<"pending" | "approved,rejected">("pending");
  const r = useAsync(() => api.get<Approval[]>(`/approvals?entity_id=${entity.id}&status=${scope}`), [entity.id, scope]);
  const [dec, setDec] = useState<{ a: Approval; approve: boolean } | null>(null);
  const [note, setNote] = useState("");
  const { run, busy } = useAction();
  return (
    <>
      <PageHead title="Approvals" sub="Maker-checker: you can never decide your own request, and approvers need sufficient authority for the amount." />
      <div className="toolbar"><button className={`btn ${scope === "pending" ? "primary" : ""}`} onClick={() => setScope("pending")}>Pending</button><button className={`btn ${scope !== "pending" ? "primary" : ""}`} onClick={() => setScope("approved,rejected")}>History</button></div>
      {r.error && <ErrorBox msg={r.error} />}
      <div className="card flush">
        {!r.data ? <Loading /> : !r.data.length ? <Empty>Nothing {scope === "pending" ? "waiting" : "decided yet"}.</Empty> : (
          <table><thead><tr><th>Request</th><th className="num">Amount</th><th>Needs</th><th>Reason</th><th>By</th><th /></tr></thead>
            <tbody>{r.data.map((a) => (
              <tr key={a.id}><td><b>{title(a.kind)}</b><div className="faint" style={{ fontSize: 12 }}>{ago(a.created_at)}</div></td><td className="num">{money(a.amount_minor)}</td>
                <td><Chip tone="brand">{title(a.required_role)}</Chip></td><td style={{ maxWidth: 360 }}>{a.reason}{a.decision_note && <div className="muted" style={{ fontSize: 12 }}>Decision: {a.decision_note}</div>}</td>
                <td>{a.requested_by_name}{a.decided_by_name && <div className="faint" style={{ fontSize: 12 }}>→ {a.decided_by_name}</div>}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{a.status === "pending" ? (
                  can("ar_manager", "finance_head") ? (a.can_decide ? <><button className="btn sm primary" onClick={() => { setDec({ a, approve: true }); setNote(""); }}>Approve</button> <button className="btn sm danger" onClick={() => { setDec({ a, approve: false }); setNote(""); }}>Reject</button></> : <span className="faint" title="Your own request, or needs higher authority">Can’t decide</span>) : null) : <StatusChip s={a.status} />}</td></tr>
            ))}</tbody></table>
        )}
      </div>
      <Modal open={!!dec} onClose={() => setDec(null)} title={dec?.approve ? "Approve request" : "Reject request"}>
        {dec && <><p>{title(dec.a.kind)} of <b>{money(dec.a.amount_minor)}</b>: {dec.a.reason}</p>
          <Field label={dec.approve ? "Note (optional)" : "Reason for rejecting (required)"}><textarea value={note} onChange={(e) => setNote(e.target.value)} /></Field>
          <div className="row between"><button className="btn" onClick={() => setDec(null)}>Cancel</button>
            <button className={`btn ${dec.approve ? "primary" : "danger"}`} disabled={busy || (!dec.approve && !note.trim())} onClick={async () => { if (await run(() => api.post(`/approvals/${dec.a.id}/decide`, { approve: dec.approve, note }), dec.approve ? "Approved and executed" : "Rejected")) setDec(null); }}>{dec.approve ? "Approve" : "Reject"}</button></div></>}
      </Modal>
    </>
  );
}
