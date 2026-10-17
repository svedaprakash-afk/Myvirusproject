import { useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { Chip, Field, PageHead, Tabs } from "../components/ui";

export default function Import() {
  const { entity, can, toast } = useApp();
  const [tab, setTab] = useState<"bank" | "erp" | "remit">("bank");
  const banks = useAsync(() => api.get<any[]>(`/entities/${entity.id}/bank-accounts`), [entity.id]);
  const [bank, setBank] = useState<number | null>(null);
  const [out, setOut] = useState<any>(null);
  const file = useRef<HTMLInputElement>(null);
  const [text, setText] = useState(""); const [full, setFull] = useState(false);
  const { run, busy } = useAction();
  const ok = can("ar_analyst", "ar_manager", "treasury");
  const acct = bank ?? banks.data?.[0]?.id;
  const send = async (path: string, extra: Record<string, string>) => {
    const f = file.current?.files?.[0];
    const fd = new FormData();
    Object.entries(extra).forEach(([k, v]) => fd.append(k, v));
    if (f) fd.append("file", f);
    const r = await run(() => api.upload<any>(path, fd), "Imported");
    if (r) { setOut(r); if (file.current) file.current.value = ""; setText(""); }
  };
  if (!ok) return <><PageHead title="Import" /><div className="note warn">Your role cannot import data.</div></>;
  return (
    <>
      <PageHead title="Import" sub="Bank statements, ERP open items and remittance advices. Re-importing the same file is safe (idempotent)." />
      <Tabs value={tab} onChange={(t) => { setTab(t); setOut(null); }} tabs={[["bank", "Bank statement"], ["erp", "ERP open items"], ["remit", "Remittance advice"]]} />
      <div className="grid g-main">
        <div className="card">
          {tab === "bank" && <>
            <Field label="Bank account"><select value={acct ?? ""} onChange={(e) => setBank(Number(e.target.value))}>{banks.data?.map((b) => <option key={b.id} value={b.id}>{b.bank_name} · {b.account_no}{b.is_gateway ? " (gateway)" : ""}</option>)}</select></Field>
            <Field label="Statement file" hint="CSV or XLSX (HDFC / ICICI / SBI / Axis / Kotak layouts auto-detected), MT940, camt.053 XML, or a payment-gateway settlement CSV."><input ref={file} type="file" accept=".csv,.xlsx,.xml,.txt,.sta,.mt940" /></Field>
            <button className="btn primary" disabled={busy || !acct} onClick={() => file.current?.files?.[0] ? send("/ingest/bank-statement", { bank_account_id: String(acct) }) : toast("Choose a file first", true)}>Import statement</button>
          </>}
          {tab === "erp" && <>
            <Field label="Open items CSV" hint="Columns recognised: invoice no, customer code/name, GSTIN, PAN, invoice date, due date, taxable value, GST, total, open amount, doc type (credit note), IRN, PO."><input ref={file} type="file" accept=".csv" /></Field>
            <label className="row"><input type="checkbox" style={{ width: "auto" }} checked={full} onChange={(e) => setFull(e.target.checked)} /> This is a full snapshot — close anything missing from it</label>
            <p className="faint" style={{ fontSize: 12 }}>Balances are never resurrected for cash you already applied but have not yet posted to the ERP.</p>
            <button className="btn primary" disabled={busy} onClick={() => file.current?.files?.[0] ? send("/ingest/open-items", { entity_id: String(entity.id), full_snapshot: String(full) }) : toast("Choose a file first", true)}>Import open items</button>
          </>}
          {tab === "remit" && <>
            <Field label="Paste email / portal text"><textarea style={{ minHeight: 150 }} value={text} onChange={(e) => setText(e.target.value)} placeholder={"From: Acme Industries\nUTR: HDFCN26274000111\nINV-1001  1,18,000.00  TDS 2,000.00  Net 1,16,000.00\nTotal payment: 1,16,000.00"} /></Field>
            <Field label="…or upload a file (PDF, scanned image, XLSX, CSV, EML, TXT)" hint="Scanned PDFs and photos are read with OCR on the server; nothing leaves your infrastructure."><input ref={file} type="file" accept=".pdf,.xlsx,.eml,.txt,.csv,.png,.jpg,.jpeg,.tif,.tiff" /></Field>
            <button className="btn primary" disabled={busy} onClick={() => (text.trim() || file.current?.files?.[0]) ? send("/ingest/remittance", { entity_id: String(entity.id), source: "manual", ...(text.trim() ? { text } : {}) }) : toast("Paste text or choose a file", true)}>Extract & link</button>
          </>}
        </div>
        <div className="card">
          <h2>Result</h2>
          {!out ? <span className="muted">Nothing imported in this session.</span> : <>
            <table><tbody>{Object.entries(out).filter(([k, v]) => typeof v !== "object").map(([k, v]) => <tr key={k}><td className="muted">{k.replace(/_/g, " ")}</td><td className="num">{String(v)}</td></tr>)}</tbody></table>
            {out.warnings?.length > 0 && <div className="mt"><h3>Warnings</h3>{out.warnings.map((w: string, i: number) => <div key={i} className="note warn">{w}</div>)}</div>}
            {out.extracted?.meta && <div className="mt">
              {out.extracted.meta.reconciled === true && <div className="note good">Lines add up to the stated total.</div>}
              {out.extracted.meta.reconciled === false && <div className="note warn">Lines do not add up to the stated total.</div>}
              {(out.extracted.meta.warnings ?? []).map((w: string, i: number) => <div key={i} className="note warn">{w}</div>)}
              <div className="faint" style={{ fontSize: 12 }}>Confidence {Math.round((out.extracted.confidence ?? 0) * 100)}%{out.extracted.meta.source_method === "ocr" ? ` · read by OCR (${out.extracted.meta.ocr_confidence}% word confidence)` : ""}{out.extractor === "llm" || out.extractor === "rules+llm" ? " · AI fallback consulted" : ""}</div></div>}
            {out.extracted && <><h3 className="mt">Extracted ({out.extractor}) {out.linked_txn_id ? <Chip tone="good">linked to receipt #{out.linked_txn_id}</Chip> : <Chip tone="warn">no receipt linked yet</Chip>}</h3><div className="pre">{JSON.stringify(out.extracted, null, 2)}</div></>}
          </>}
          {out && tab !== "remit" && <p className="mt"><button className="btn primary" disabled={busy} onClick={() => run(() => api.post<any>("/matching/run", { entity_id: entity.id }), (r) => `${r.processed} processed: ${r.auto} auto, ${r.review} review, ${r.exception} exceptions`)}>Run matching now</button></p>}
        </div>
      </div>
    </>
  );
}
