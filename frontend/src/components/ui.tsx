import { useEffect, useRef, type ReactNode } from "react";
import { title } from "../format";

export function Chip({ tone = "", children, title: t }: { tone?: "good" | "warn" | "bad" | "info" | "brand" | ""; children: ReactNode; title?: string }) {
  return <span className={`chip ${tone}`} title={t}>{children}</span>;
}

const TONES: Record<string, "good" | "warn" | "bad" | "info" | "brand" | ""> = {
  matched: "good", posted: "good", acknowledged: "good", resolved: "good", approved: "good", accepted: "good", auto_approved: "brand", available: "good",
  review: "warn", pending_review: "warn", pending: "warn", partially_matched: "warn", in_progress: "info", pending_approval: "warn", draft: "", exported: "info", planned: "",
  exception: "bad", open: "bad", rejected: "bad", failed: "bad", written_off: "", ignored: "", new: "info", reversed: "bad", superseded: "",
};
export const StatusChip = ({ s }: { s: string }) => <Chip tone={TONES[s] ?? ""}>{title(s)}</Chip>;

export function Loading({ rows = 4 }: { rows?: number }) {
  return <div className="grid" style={{ padding: 16 }} aria-busy="true">{Array.from({ length: rows }).map((_, i) => <div key={i} className="skeleton" style={{ width: `${90 - i * 12}%` }} />)}</div>;
}
export const Empty = ({ children }: { children: ReactNode }) => <div className="empty">{children}</div>;
export const ErrorBox = ({ msg }: { msg: string }) => <div className="note bad" role="alert">{msg}</div>;

export function PageHead({ title: t, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return <div className="page-head"><div><h1>{t}</h1>{sub && <p>{sub}</p>}</div><div className="row wrap">{children}</div></div>;
}

export function Modal({ open, onClose, title: t, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    ref.current?.querySelector<HTMLElement>("input,textarea,select,button")?.focus();
    return () => { window.removeEventListener("keydown", k); prev?.focus(); };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={t} ref={ref}><h2>{t}</h2>{children}</div>
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <div className="field"><label>{label}</label>{children}{hint && <div className="faint" style={{ fontSize: 12, marginTop: 3 }}>{hint}</div>}</div>;
}

export function Tabs<T extends string>({ value, onChange, tabs }: { value: T; onChange: (v: T) => void; tabs: [T, string][] }) {
  return <div className="tabs" role="tablist">{tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={value === k} className={value === k ? "on" : ""} onClick={() => onChange(k)}>{l}</button>)}</div>;
}

export function ConfBar({ value, threshold }: { value: number; threshold?: number }) {
  const tone = value >= (threshold ?? 0.9) ? "var(--good)" : value >= 0.6 ? "var(--warn)" : "var(--bad)";
  return <div className="conf" role="img" aria-label={`Confidence ${(value * 100).toFixed(0)}%`}><i style={{ width: `${Math.round(value * 100)}%`, background: tone }} /></div>;
}

export const channelChip = (c: string) => <Chip tone="info">{c}</Chip>;
