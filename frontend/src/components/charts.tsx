import { compact } from "../format";

export interface Series { key: string; label: string; color: string }

/** Stacked columns, pure SVG. Values are in minor units when `money` is set. */
export function Columns({ data, series, money = false, height = 190 }: { data: { label: string; [k: string]: number | string }[]; series: Series[]; money?: boolean; height?: number }) {
  const W = 560, H = height, pl = 62, pb = 24, pt = 8, pr = 8;
  const totals = data.map((d) => series.reduce((s, x) => s + (Number(d[x.key]) || 0), 0));
  const max = Math.max(...totals, 1);
  const niceMax = (() => { const p = Math.pow(10, Math.floor(Math.log10(max))); return Math.ceil(max / p) * p; })();
  const bw = Math.min(46, ((W - pl - pr) / Math.max(data.length, 1)) * 0.62);
  const fmt = (v: number) => (money ? compact(v) : String(Math.round(v)));
  const y = (v: number) => pt + (H - pt - pb) * (1 - v / niceMax);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Column chart" style={{ display: "block" }}>
      {[0, 0.5, 1].map((t) => (
        <g key={t}><line x1={pl} x2={W - pr} y1={y(niceMax * t)} y2={y(niceMax * t)} stroke="var(--border)" strokeDasharray={t ? "3 3" : ""} />
          <text x={pl - 6} y={y(niceMax * t) + 4} textAnchor="end" fontSize="10.5" fill="var(--faint)">{fmt(niceMax * t)}</text></g>
      ))}
      {data.map((d, i) => {
        const cx = pl + ((W - pl - pr) / data.length) * (i + 0.5);
        let acc = 0;
        return (
          <g key={i}>
            {series.map((s) => { const v = Number(d[s.key]) || 0; const y0 = y(acc + v), h = y(acc) - y0; acc += v; return h > 0 ? <rect key={s.key} x={cx - bw / 2} y={y0} width={bw} height={h} fill={s.color} rx="2"><title>{`${d.label} · ${s.label}: ${fmt(v)}`}</title></rect> : null; })}
            <text x={cx} y={H - 8} textAnchor="middle" fontSize="10.5" fill="var(--muted)">{d.label}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function HBars({ rows, money = false }: { rows: { label: string; value: number; color?: string; sub?: string }[]; money?: boolean }) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <div className="grid" style={{ gap: 9 }}>
      {rows.map((r) => (
        <div key={r.label}>
          <div className="row between" style={{ fontSize: 12.5 }}><span>{r.label}{r.sub && <span className="faint"> · {r.sub}</span>}</span><b className="num">{money ? compact(r.value) : r.value}</b></div>
          <div className="bar-track"><i style={{ left: 0, width: `${Math.max((r.value / max) * 100, r.value ? 1.5 : 0)}%`, background: r.color || "var(--c1)" }} /></div>
        </div>
      ))}
    </div>
  );
}

export const Legend = ({ series }: { series: Series[] }) => <div className="legend">{series.map((s) => <span key={s.key}><i style={{ background: s.color }} />{s.label}</span>)}</div>;
