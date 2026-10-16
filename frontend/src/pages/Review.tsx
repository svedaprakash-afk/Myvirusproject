import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Proposal, type Txn } from "../api";
import { useApp } from "../ctx";
import { useAction, useAsync } from "../hooks";
import { date, money, pct, title } from "../format";
import { Chip, ConfBar, Empty, ErrorBox, Loading, Modal, PageHead, Field, StatusChip } from "../components/ui";
import ManualMatch from "../components/ManualMatch";

const HIT: Record<string, string> = { exact: "Exact ref", irn: "e-Invoice IRN", remittance: "Remittance", serial: "Serial only", po: "PO ref", manual: "Manual" };

export default function Review() {
  const { entity, can, toast, refresh } = useApp();
  const [band, setBand] = useState<"all" | "low" | "mid" | "high">("all");
  const q = band === "low" ? "&max_conf=0.6" : band === "mid" ? "&min_conf=0.6&max_conf=0.85" : band === "high" ? "&min_conf=0.85" : "";
  const list = useAsync(() => api.get<{ total: number; items: Proposal[] }>(`/proposals?entity_id=${entity.id}&status=pending_review&limit=200${q}`), [entity.id, band]);
  const [sel, setSel] = useState<number | null>(null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const { run, busy } = useAction();
  const items = list.data?.items ?? [];
  const cur = items.find((p) => p.id === sel) ?? items[0] ?? null;
  const writable = can("ar_analyst", "ar_manager");

  const next = useCallback((dir: 1 | -1) => { if (!items.length) return; const i = Math.max(0, items.findIndex((p) => p.id === cur?.id)); setSel(items[(i + dir + items.length) % items.length].id); }, [items, cur]);
  const accept = useCallback(async (p: Proposal) => { await run(() => api.post(`/proposals/${p.id}/accept`), "Match accepted"); setSel(null); }, [run]);
  const [rejecting, setRejecting] = useState<Proposal | null>(null);
  const [manual, setManual] = useState(false);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement; if (/INPUT|TEXTAREA|SELECT/.test(t.tagName) || e.metaKey || e.ctrlKey || rejecting || manual) return;
      if (e.key === "j") next(1); else if (e.key === "k") next(-1);
      else if (e.key === "a" && cur && writable) accept(cur); else if (e.key === "r" && cur && writable) setRejecting(cur);
    };
    window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k);
  }, [next, cur, accept, writable, rejecting, manual]);

  const bulk = async () => {
    const ps = items.filter((p) => checked.has(p.id));
    let ok = 0; const fails: string[] = [];
    for (const p of ps) { try { await api.post(`/proposals/${p.id}/accept`); ok++; } catch (e) { fails.push(`#${p.id}: ${(e as Error).message}`); } }
    setChecked(new Set()); list.reload(); refresh();
    toast(`${ok} accepted${fails.length ? `, ${fails.length} failed — ${fails.slice(0, 2).join("; ")}` : ""}`, fails.length > 0);
  };
  const safeToBulk = (p: Proposal) => p.confidence >= 0.8 && !p.explanation.warnings.length && !p.unapplied_minor && !p.short_minor;

  return (
    <>
      <PageHead title="Match review" sub={<>Proposals the engine would not post on its own. <span className="kbd">J</span>/<span className="kbd">K</span> move · <span className="kbd">A</span> accept · <span className="kbd">R</span> reject</>}>
        {checked.size > 0 && <button className="btn primary" onClick={bulk}>Accept {checked.size} selected</button>}
      </PageHead>
      <div className="tabs" role="tablist">{([["all", "All"], ["low", "Low (<60%)"], ["mid", "Medium"], ["high", "High (≥85%)"]] as const).map(([k, l]) => <button key={k} role="tab" aria-selected={band === k} className={band === k ? "on" : ""} onClick={() => setBand(k)}>{l}</button>)}</div>
      {list.error && <ErrorBox msg={list.error} />}
      {list.loading && !list.data ? <Loading rows={6} /> : !items.length ? <div className="card"><Empty>Review queue is clear. New bank lines will appear here when the engine needs a human.</Empty></div> : (
        <div className="split">
          <div className="card flush"><div className="list" role="listbox" aria-label="Proposals">
            {items.map((p) => (
              <div key={p.id} className={`list-item ${cur?.id === p.id ? "sel" : ""}`} role="option" aria-selected={cur?.id === p.id} onClick={() => setSel(p.id)}>
                <div className="row between">
                  <span className="row">{writable && safeToBulk(p) && <input type="checkbox" aria-label="Select for bulk accept" style={{ width: "auto" }} checked={checked.has(p.id)} onClick={(e) => e.stopPropagation()} onChange={(e) => { const s = new Set(checked); e.target.checked ? s.add(p.id) : s.delete(p.id); setChecked(s); }} />}<b className="num">{money(p.txn?.receivable_minor)}</b></span>
                  <span className="muted">{pct(p.confidence)}</span>
                </div>
                <div style={{ fontSize: 13 }}>{p.customer?.name ?? <i className="muted">customer unknown</i>}</div>
                <div className="faint" style={{ fontSize: 12 }}>{date(p.txn?.txn_date)} · {p.txn?.channel} · {p.allocations.map((a) => a.number).join(", ").slice(0, 60) || "no invoices"}</div>
                <div style={{ marginTop: 4 }}><ConfBar value={p.confidence} /></div>
              </div>
            ))}
          </div></div>
          {cur && <Detail key={cur.id} p={cur} writable={writable} busy={busy} onAccept={() => accept(cur)} onReject={() => setRejecting(cur)} onManual={() => setManual(true)} />}
        </div>
      )}
      <RejectModal p={rejecting} onClose={() => setRejecting(null)} onDone={() => { setRejecting(null); setSel(null); list.reload(); }} />
      {cur?.txn && <ManualMatch txn={cur.txn} customerId={cur.customer?.id} open={manual} onClose={() => setManual(false)} onDone={() => { setSel(null); list.reload(); }} />}
    </>
  );
}

function Detail({ p, writable, busy, onAccept, onReject, onManual }: { p: Proposal; writable: boolean; busy: boolean; onAccept: () => void; onReject: () => void; onManual: () => void }) {
  const t = p.txn as Txn;
  const full = useAsync(() => api.get<any>(`/transactions/${t.id}`), [t.id]);
  const ex = p.explanation;
  const maxC = Math.max(...ex.contributions.map((c) => Math.abs(c.contribution)), 1);
  return (
    <div className="grid">
      <div className="card">
        <div className="row between wrap">
          <div><div style={{ fontSize: 24, fontWeight: 700 }} className="num">{money(t.receivable_minor)}</div>
            <div className="muted">{date(t.txn_date)} · <Chip tone="info">{t.channel}</Chip> {t.fee_minor > 0 && <Chip title="Gateway fee deducted before settlement">fee {money(t.fee_minor)}</Chip>} · {t.bank}</div></div>
          <div style={{ textAlign: "right" }}><div className="muted" style={{ fontSize: 12 }}>Confidence</div><b style={{ fontSize: 20 }}>{pct(p.confidence)}</b><div style={{ width: 120 }}><ConfBar value={p.confidence} /></div></div>
        </div>
        <div className="pre mt">{t.narration}</div>
        <div className="row wrap mt" style={{ gap: 14 }}>
          <span><span className="faint">Payer:</span> <b>{t.payer_name ?? "—"}</b></span>
          <span><span className="faint">Identified as:</span> <b>{p.customer?.name ?? "unknown"}</b> {t.customer_confidence !== null && <span className="muted">({pct(t.customer_confidence)})</span>}</span>
          {t.utr && <span><span className="faint">UTR:</span> <span className="mono">{t.utr}</span></span>}
        </div>
      </div>

      <div className="card">
        <h2>Proposed application</h2>
        <div className="note good" style={{ marginTop: 0 }}>{ex.summary}</div>
        {p.allocations.length ? (
          <table><thead><tr><th>Invoice</th><th>Due</th><th className="num">Open</th><th className="num">Cash applied</th><th className="num">TDS</th><th className="num">Write-off</th><th>Evidence</th></tr></thead>
            <tbody>{p.allocations.map((a) => (
              <tr key={a.invoice_id}><td style={{ whiteSpace: "nowrap" }}>{a.number} {a.disputed && <Chip tone="bad">disputed</Chip>}</td><td style={{ whiteSpace: "nowrap" }}>{date(a.due_date)}</td><td className="num">{money(a.invoice_open_minor)}</td><td className="num"><b>{money(a.applied_minor)}</b></td>
                <td className="num">{a.tds_minor ? <span title={a.tds_section ?? ""}>{money(a.tds_minor)}{a.tds_rate_bp ? <span className="faint"> @{(a.tds_rate_bp / 100).toFixed(a.tds_rate_bp % 100 ? 2 : 0)}%</span> : null}</span> : "—"}</td>
                <td className="num">{a.writeoff_minor ? money(a.writeoff_minor) : "—"}</td><td>{a.reference_hit ? <Chip tone={a.reference_hit === "serial" ? "warn" : "good"}>{HIT[a.reference_hit] ?? a.reference_hit}</Chip> : <Chip>amount / history</Chip>}</td></tr>
            ))}</tbody></table>
        ) : <Empty>No invoices — cash would be held on account.</Empty>}
        {p.short_minor > 0 && <div className="note warn">Short-paid by {money(p.short_minor)} — accepting opens a short-pay exception for follow-up.</div>}
        {p.unapplied_minor > 0 && <div className="note warn">{money(p.unapplied_minor)} is left over — accepting holds it on account and opens an overpayment exception.</div>}
        {writable && <div className="row mt wrap"><button className="btn primary" disabled={busy} onClick={onAccept}>Accept <span className="kbd">A</span></button><button className="btn" disabled={busy} onClick={onReject}>Reject <span className="kbd">R</span></button><button className="btn" onClick={onManual}>Allocate manually…</button></div>}
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>Why this score</h2>
          {ex.contributions.map((c) => (
            <div className="contrib" key={c.feature}><span>{c.label}</span>
              <div className="bar-track"><i style={{ left: c.contribution >= 0 ? "50%" : `${50 - (Math.abs(c.contribution) / maxC) * 50}%`, width: `${(Math.abs(c.contribution) / maxC) * 50}%`, background: c.contribution >= 0 ? "var(--good)" : "var(--bad)" }} /></div>
              <span className="num" style={{ color: c.contribution >= 0 ? "var(--good)" : "var(--bad)" }}>{c.contribution > 0 ? "+" : ""}{c.contribution.toFixed(1)}</span></div>
          ))}
          <p className="faint" style={{ fontSize: 12, marginBottom: 0 }}>Log-odds contributions from model <span className="mono">{p.model_version}</span>. Positive pushes toward “correct”.</p>
        </div>
        <div className="card">
          <h2>Why a person is needed</h2>
          {p.guardrails.length ? p.guardrails.map((g, i) => <div key={i} className="note warn">{g}</div>) : <div className="muted">All guardrails passed.</div>}
          {ex.warnings.map((w, i) => <div key={i} className="note bad">{w}</div>)}
          <h3 className="mt">Evidence</h3>
          <ul style={{ margin: 0, paddingLeft: 18 }}>{ex.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
          {ex.alternatives.length > 0 && <><h3 className="mt">Other candidates considered</h3>{ex.alternatives.map((a, i) => <div key={i} className="row between"><span>{a.invoices.join(", ")}</span><span className="muted">{pct(a.confidence)} · {title(a.strategy)}</span></div>)}</>}
        </div>
      </div>
      {full.data?.remittance && <div className="card"><h2>Remittance advice <Chip>{full.data.remittance.extractor}</Chip></h2><div className="pre">{full.data.remittance.text}</div></div>}
    </div>
  );
}

function RejectModal({ p, onClose, onDone }: { p: Proposal | null; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const { run, busy } = useAction();
  useEffect(() => setReason(""), [p?.id]);
  return (
    <Modal open={!!p} onClose={onClose} title="Reject this proposal">
      <p className="muted">The receipt returns to the pool and your decision becomes a learning signal for the scoring model.</p>
      <Field label="Why is it wrong? (optional, audited)"><textarea value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
      <div className="row between"><button className="btn" onClick={onClose}>Cancel</button><button className="btn danger" disabled={busy} onClick={async () => { if (p && (await run(() => api.post(`/proposals/${p.id}/reject`, { reason }), "Rejected"))) onDone(); }}>Reject</button></div>
    </Modal>
  );
}
