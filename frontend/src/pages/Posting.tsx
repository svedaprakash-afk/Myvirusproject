import { Fragment } from "react";
import { useState } from "react";
import { api, download, type Batch } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { ago, date, money, title } from "../format";
import { Chip, Empty, ErrorBox, Field, Loading, Modal, PageHead, StatusChip } from "../components/ui";

export default function Posting() {
  const { entity, can, toast } = useApp();
  const list = useAsync(() => api.get<{ items: Batch[]; ready_to_batch: number }>(`/posting/batches?entity_id=${entity.id}`), [entity.id]);
  const conns = useAsync(() => api.get<{ name: string; label: string; status: string; unverified?: boolean }[]>(`/connectors?entity_id=${entity.id}`), [entity.id]);
  const unc = useAsync(() => api.get<any>(`/posting/unconfirmed?entity_id=${entity.id}`), [entity.id]);
  const [open, setOpen] = useState<number | null>(null);
  const { run, busy } = useAction();
  return (
    <>
      <PageHead title="ERP posting" sub="Balanced canonical vouchers, built by one person, released by another, exported through a connector, acknowledged by the ERP.">
        {can("ar_manager", "treasury") && <button className="btn primary" disabled={busy || !list.data?.ready_to_batch} onClick={async () => { const b = await run(() => api.post<Batch>("/posting/batches", { entity_id: entity.id }), (b) => `Batch #${b.id} built (${b.voucher_count} vouchers)`); if (b) setOpen(b.id); }}>Build batch ({list.data?.ready_to_batch ?? 0} ready)</button>}
      </PageHead>
      {list.error && <ErrorBox msg={list.error} />}
      {unc.data?.items.length > 0 && <div className="note warn mb" role="alert"><b>{unc.data.items.length} batch(es) exported more than {unc.data.hours} hours ago have not been confirmed by the ERP:</b> {unc.data.items.map((b: any) => `#${b.id}`).join(", ")}. Check the ERP import log, then upload its confirmation or mark the batch failed.</div>}
      <div className="grid g-main">
        <div className="card flush">
          {!list.data ? <Loading /> : !list.data.items.length ? <Empty>No batches yet. Accepted and auto-approved matches wait here until someone builds a batch.</Empty> : (
            <table><thead><tr><th>Batch</th><th>Status</th><th className="num">Vouchers</th><th className="num">Total</th><th>Connector</th></tr></thead>
              <tbody>{list.data.items.map((b) => <tr key={b.id} className={`click ${open === b.id ? "sel" : ""}`} onClick={() => setOpen(b.id)}><td><b>#{b.id}</b><div className="faint" style={{ fontSize: 12 }}>{ago(b.created_at)}</div></td><td><StatusChip s={b.status} /></td><td className="num">{b.voucher_count}</td><td className="num">{money(b.total_minor)}</td><td>{b.connector ?? "—"}</td></tr>)}</tbody></table>
          )}
        </div>
        <div className="card">
          <h2>ERP connectors</h2>
          {conns.data?.map((c) => <div key={c.name} className="row between" style={{ padding: "5px 0" }}><span>{c.label}{c.unverified && <span className="faint" title="Written from the vendor's published format; not yet imported into a real ERP"> · untested</span>}</span><Chip tone={c.status === "available" ? "good" : ""}>{c.status}</Chip></div>)}
          <p className="faint" style={{ fontSize: 12, marginBottom: 0 }}>Platform-specific connectors plug into the same canonical journal. “Planned” means not built — it is never faked.</p>
        </div>
      </div>
      {open && <BatchDetail id={open} onClose={() => setOpen(null)} reload={list.reload} toast={toast} connectors={(conns.data ?? []).filter((c) => c.status === "available")} />}
    </>
  );
}

function BatchDetail({ id, onClose, reload, toast, connectors }: { id: number; onClose: () => void; reload: () => void; toast: (m: string, bad?: boolean) => void; connectors: { name: string; label: string }[] }) {
  const { can } = useApp();
  const d = useAsync(() => api.get<Batch>(`/posting/batches/${id}`), [id]);
  const { run, busy } = useAction();
  const [confOpen, setConfOpen] = useState(false); const [confRes, setConfRes] = useState<any>(null); const [failOn, setFailOn] = useState(false);
  const [conn, setConn] = useState("generic_csv"); const [ackOpen, setAckOpen] = useState(false); const [prefix, setPrefix] = useState("ERP");
  if (!d.data) return <div className="card mt"><Loading /></div>;
  const b = d.data; const lines = b.lines ?? [];
  const vouchers = Array.from(new Set(lines.map((l) => l.voucher_no)));
  const after = () => { d.reload(); reload(); };
  const can2 = can("treasury", "finance_head");
  const ext = conn === "tally_prime" ? "xml" : /json|sap_s4|d365_bc/.test(conn) ? "json" : "csv";
  const doExport = async () => { try { await download(`/posting/batches/${id}/export`, `batch_${id}.${ext}`, { connector: conn }); toast("Exported"); after(); } catch (e) { toast((e as Error).message, true); } };
  return (
    <div className="card mt">
      <div className="row between wrap"><h2 style={{ margin: 0 }}>Batch #{b.id} <StatusChip s={b.status} /></h2>
        <div className="row wrap">
          {b.status === "draft" && can2 && <button className="btn primary" disabled={busy} onClick={() => run(() => api.post(`/posting/batches/${id}/release`), "Released").then(after)}>Release (four-eyes)</button>}
          {["approved", "exported", "failed"].includes(b.status) && can("ar_manager", "treasury", "finance_head") && <>
            <select value={conn} onChange={(e) => setConn(e.target.value)} style={{ width: "auto" }} aria-label="Connector">{connectors.map((c) => <option key={c.name} value={c.name}>{c.label}</option>)}</select>
            <button className="btn primary" onClick={doExport}>Export</button></>}
          {b.status === "exported" && can("ar_manager", "treasury", "finance_head") && <button className="btn" onClick={() => setAckOpen(true)}>Record ERP acknowledgement…</button>}
          {b.status === "exported" && can("ar_manager", "treasury", "finance_head") && <button className="btn" onClick={() => setConfOpen(true)}>Upload ERP confirmation…</button>}
          {(b as any).stored && can("ar_manager", "treasury", "finance_head", "auditor") && <button className="btn" title="Re-reads the stored file and verifies its SHA-256 before serving" onClick={async () => { try { await download(`/posting/batches/${id}/download`, `batch_${id}_stored.${(b.connector ?? "").includes("json") ? "json" : "csv"}`); toast("Stored copy verified and downloaded"); } catch (e) { toast((e as Error).message, true); } }}>Download stored copy</button>}
          {["draft", "approved", "failed"].includes(b.status) && can("ar_manager", "treasury", "finance_head") && <button className="btn danger" disabled={busy} onClick={() => confirm("Cancel this batch? Its matches return to the ready pool.") && run(() => api.post(`/posting/batches/${id}/cancel`), "Cancelled").then(() => { onClose(); reload(); })}>Cancel</button>}
          <button className="btn" onClick={onClose}>Close</button></div></div>
      {b.status === "draft" && !can2 && <div className="note warn">Waiting for a treasury or finance-head user (who did not build or accept anything in this batch) to release it.</div>}
      {b.checksum && <div className="faint mt mono" style={{ fontSize: 11.5 }}>SHA-256 {b.checksum}</div>}
      <VoucherTable lines={lines} acked={b.status === "acknowledged"} />
      <Modal open={confOpen} onClose={() => { setConfOpen(false); setConfRes(null); }} title="Upload the ERP's confirmation">
        <p className="muted">A CSV from the ERP with a <span className="mono">voucher_no</span> column and the ERP's document number (<span className="mono">erp_doc_no</span>), optionally <span className="mono">status</span> and <span className="mono">error</span>. The batch is acknowledged only when every voucher is confirmed.</p>
        <Field label="Confirmation file"><input type="file" accept=".csv" id="conf-file" /></Field>
        <label className="row" style={{ textTransform: "none" }}><input type="checkbox" style={{ width: "auto" }} checked={failOn} onChange={(e) => setFailOn(e.target.checked)} /> Mark the batch failed if the file reports rejected vouchers</label>
        {confRes && <div className="mt">{confRes.acknowledged ? <div className="note good">All {confRes.vouchers} vouchers confirmed. Batch acknowledged.</div> : <div className="note warn">Not acknowledged: {confRes.confirmed} of {confRes.vouchers} confirmed.
          {confRes.errors.map((e: any) => <div key={e.voucher_no}>Rejected {e.voucher_no}: {e.error}</div>)}{confRes.missing.length > 0 && <div>No answer for {confRes.missing.length} voucher(s), e.g. {confRes.missing[0]}</div>}{confRes.unknown_in_file.length > 0 && <div>Not in this batch: {confRes.unknown_in_file.slice(0, 3).join(", ")}</div>}{confRes.batch_failed && <div><b>Batch marked failed.</b></div>}</div>}</div>}
        <div className="row between mt"><button className="btn" onClick={() => { setConfOpen(false); setConfRes(null); }}>Close</button>
          <button className="btn primary" disabled={busy} onClick={async () => { const f = (document.getElementById("conf-file") as HTMLInputElement)?.files?.[0]; if (!f) return toast("Choose the confirmation file first", true); const fd = new FormData(); fd.append("file", f); fd.append("fail_on_errors", String(failOn)); const r = await run(() => api.upload<any>(`/posting/batches/${id}/ack-file`, fd)); if (r) { setConfRes(r); after(); } }}>Check and apply</button></div>
      </Modal>
      <Modal open={ackOpen} onClose={() => setAckOpen(false)} title="Record ERP acknowledgement">
        <p className="muted">In production the connector posts back the ERP document numbers. For manual ERPs, enter a prefix and the platform numbers the {vouchers.length} vouchers sequentially — replace with real references if your ERP returns them.</p>
        <Field label="ERP document prefix"><input value={prefix} onChange={(e) => setPrefix(e.target.value)} /></Field>
        <div className="row between"><button className="btn" onClick={() => setAckOpen(false)}>Cancel</button><button className="btn primary" disabled={busy || !prefix.trim()} onClick={async () => { if (await run(() => api.post(`/posting/batches/${id}/ack`, { erp_docs: Object.fromEntries(vouchers.map((v, i) => [v, `${prefix}-${String(i + 1).padStart(4, "0")}`])) }), "Acknowledged — receipts are now posted")) { setAckOpen(false); after(); } }}>Acknowledge</button></div>
      </Modal>
    </div>
  );
}

function VoucherTable({ lines, acked }: { lines: NonNullable<Batch["lines"]>; acked: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const [limit, setLimit] = useState(25);
  const groups = new Map<string, typeof lines>();
  lines.forEach((l) => groups.set(l.voucher_no, [...(groups.get(l.voucher_no) ?? []), l]));
  const all = Array.from(groups.entries());
  return (
    <>
      <p className="muted mt" style={{ marginBottom: 6 }}>{all.length} vouchers · {lines.length} lines. Every voucher balances in base currency (foreign legs show their document amount and rate, with realised FX as its own line) or the batch cannot be built.</p>
      <table><thead><tr><th>Voucher</th><th>Date</th><th className="num">Lines</th><th className="num">Debit</th><th className="num">Credit</th>{acked && <th>ERP doc</th>}</tr></thead>
        <tbody>{all.slice(0, limit).map(([v, ls]) => (<Fragment key={v}>
          <tr className="click" onClick={() => setOpen(open === v ? null : v)}><td className="mono">{open === v ? "▾" : "▸"} {v}</td><td>{date(ls[0].posting_date)}</td><td className="num">{ls.length}</td>
            <td className="num">{money(ls.reduce((a, l) => a + l.base_debit_minor, 0))}{ls.some((l) => l.currency !== "INR") && <span className="chip info" style={{ marginLeft: 6 }}>{Array.from(new Set(ls.map((l) => l.currency).filter((c) => c !== "INR"))).join("/")}</span>}</td><td className="num">{money(ls.reduce((a, l) => a + l.base_credit_minor, 0))}</td>{acked && <td className="mono">{ls[0].erp_doc_no}</td>}</tr>
          {open === v && <tr><td colSpan={acked ? 6 : 5} style={{ background: "var(--surface-2)" }}>
            <table><tbody>{ls.map((l) => <tr key={l.id}><td>{title(l.line_type)}</td><td className="mono">{l.account}</td><td className="num">{l.debit_minor ? money(l.debit_minor, l.currency) : ""}</td><td className="num">{l.credit_minor ? money(l.credit_minor, l.currency) : ""}</td><td className="faint">{l.rate_micro ? `@ ${(l.rate_micro / 1e6).toFixed(4)}` : ""}</td><td className="muted">{l.narration}</td></tr>)}</tbody></table></td></tr>}</Fragment>))}</tbody></table>
      {all.length > limit && <button className="btn mt" onClick={() => setLimit(limit + 50)}>Show more ({all.length - limit} remaining)</button>}
    </>
  );
}
