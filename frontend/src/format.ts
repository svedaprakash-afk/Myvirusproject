// All money on the wire is integer minor units (paise/cents). Never use floats for arithmetic; only for display.
const SYM: Record<string, string> = { INR: "₹", USD: "$", EUR: "€", GBP: "£", SGD: "S$", JPY: "¥", AED: "AED ", CHF: "CHF ", AUD: "A$", CAD: "C$" };
const fmtIN = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtEN = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const sym = (ccy: string) => SYM[ccy] ?? `${ccy} `;

/** money(minor) -> ₹ with Indian grouping; money(minor, "USD") -> $ with international grouping. */
export function money(minor: number | null | undefined, ccyOrOpts: string | { sign?: boolean; ccy?: string } = "INR"): string {
  if (minor === null || minor === undefined) return "—";
  const o = typeof ccyOrOpts === "string" ? { ccy: ccyOrOpts } : ccyOrOpts;
  const ccy = o.ccy ?? "INR";
  const s = (ccy === "INR" ? fmtIN : fmtEN).format(Math.abs(minor) / 100);
  const neg = minor < 0 ? "−" : o.sign && minor > 0 ? "+" : "";
  return `${neg}${sym(ccy)}${s}`;
}

/** Compact units. INR: K / L / Cr. Other currencies: K / M / B. */
export function compact(minor: number | null | undefined, ccy = "INR"): string {
  if (minor === null || minor === undefined) return "—";
  const v = Math.abs(minor) / 100, sg = minor < 0 ? "−" : "", y = sym(ccy);
  if (ccy === "INR") {
    if (v >= 1e7) return `${sg}${y}${(v / 1e7).toFixed(2)} Cr`;
    if (v >= 1e5) return `${sg}${y}${(v / 1e5).toFixed(2)} L`;
    if (v >= 1e3) return `${sg}${y}${(v / 1e3).toFixed(1)} K`;
    return `${sg}${y}${v.toFixed(0)}`;
  }
  if (v >= 1e9) return `${sg}${y}${(v / 1e9).toFixed(2)} B`;
  if (v >= 1e6) return `${sg}${y}${(v / 1e6).toFixed(2)} M`;
  if (v >= 1e3) return `${sg}${y}${(v / 1e3).toFixed(1)} K`;
  return `${sg}${y}${v.toFixed(0)}`;
}

export const pct = (x: number | null | undefined, d = 0) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`);

export function date(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(s.length <= 10 ? s + "T00:00:00" : s + "Z");
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "2-digit" });
}

export function ago(s: string | null | undefined): string {
  if (!s) return "";
  const ms = Date.now() - new Date(s + "Z").getTime();
  const h = ms / 36e5;
  return h < 1 ? `${Math.max(1, Math.round(h * 60))}m ago` : h < 48 ? `${Math.round(h)}h ago` : `${Math.round(h / 24)}d ago`;
}

export const title = (s: string) => s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export function rateLabel(bp: number | null | undefined): string {
  return bp ? `${(bp / 100).toFixed(bp % 100 ? 2 : 0).replace(/\.?0+$/, "")}%` : "—";
}

export function toMinor(rupees: string): number {
  const clean = rupees.replace(/[,\s₹]/g, "");
  if (!/^-?\d+(\.\d{0,2})?$/.test(clean)) return NaN;
  const [i, f = ""] = clean.split(".");
  const neg = i.startsWith("-");
  return (neg ? -1 : 1) * (Math.abs(parseInt(i, 10)) * 100 + parseInt((f + "00").slice(0, 2), 10));
}

const LABELS: Record<string, string> = { non_ar_credit: "Non-AR credit", short_pay_tds: "Short-pay (TDS)", on_account: "On account", pending_review: "Pending review" };
export const label = (s: string) => LABELS[s] ?? title(s);
export const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

export const rate6 = (micro: number | null | undefined) => (micro ? (micro / 1e6).toFixed(4) : "—");
