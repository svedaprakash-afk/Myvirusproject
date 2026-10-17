import { useEffect, useState } from "react";
import { api, setToken, type Entity, type User } from "../api";

const DEMO = [["analyst@meridian.example", "AR analyst", "Works the match queue and exceptions"], ["manager@meridian.example", "AR manager", "Approves write-offs, builds posting batches"],
  ["treasury@meridian.example", "Treasury", "Releases and exports posting batches"], ["cfo@meridian.example", "Finance head", "Approves large adjustments, tunes the AI"],
  ["auditor@meridian.example", "Auditor", "Read-only, verifies the audit chain"], ["admin@meridian.example", "Admin", "Configuration and demo data (no financial authority)"]];

export default function Login({ onLogin, ssoError }: { onLogin: (u: User, e: Entity[]) => void; ssoError?: string }) {
  const [email, setEmail] = useState(""), [pw, setPw] = useState(""), [err, setErr] = useState(ssoError ?? ""), [busy, setBusy] = useState(false);
  const [sso, setSso] = useState<{ enabled: boolean; enforce: boolean; label: string } | null>(null);
  useEffect(() => { api.get<any>("/auth/sso/config").then(setSso).catch(() => setSso(null)); }, []);
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr("");
    try {
      const r = await api.post<{ token: string; user: User }>("/auth/login", { email, password: pw });
      setToken(r.token);
      onLogin(r.user, await api.get<Entity[]>("/entities"));
    } catch (x) { setErr(x instanceof Error ? x.message : "Sign-in failed"); } finally { setBusy(false); }
  }
  return (
    <div className="login">
      <div className="card">
        <div className="brand" style={{ padding: 0, marginBottom: 6 }}><i>✓</i> Cash Application</div>
        <p className="muted" style={{ marginTop: 0 }}>AI-assisted cash application and receivables exceptions for Indian mid-market finance teams.</p>
        {import.meta.env.VITE_DEMO && <div className="note" role="note"><b>Interactive demo.</b> Everything here is synthetic (a made-up manufacturer, "Meridian"). It runs on a frozen snapshot with no server: you can accept or reject matches, work exceptions and decide approvals, but changes live only in this tab. Pick a role below, then sign in.</div>}
        {sso?.enabled && <><a className="btn primary" href="/api/v1/auth/sso/login" style={{ width: "100%", justifyContent: "center", boxSizing: "border-box", marginBottom: 10 }}>{sso.label}</a>
          {err && <div className="note bad" role="alert">{err}</div>}
          {!sso.enforce && <p className="faint" style={{ textAlign: "center", margin: "4px 0 10px" }}>or sign in with a password</p>}</>}
        {(!sso?.enforce || import.meta.env.VITE_DEMO) && <form onSubmit={submit}>
          <div className="field"><label htmlFor="em">Email</label><input id="em" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></div>
          <div className="field"><label htmlFor="pw">Password</label><input id="pw" type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} required /></div>
          {err && !sso?.enabled && <div className="note bad" role="alert">{err}</div>}
          <button className="btn primary" disabled={busy} style={{ width: "100%", justifyContent: "center" }}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>}
        {sso?.enforce && <p className="faint">Password sign-in is turned off. An administrator can still use a password as a break-glass route: <a href="#password" onClick={(e) => { e.preventDefault(); setSso({ ...sso, enforce: false }); }}>show the password form</a>.</p>}
        <h3 style={{ marginTop: 22 }}>Demo accounts <span className="faint" style={{ textTransform: "none" }}>(password: demo1234)</span></h3>
        <div className="grid" style={{ gap: 4 }}>
          {DEMO.map(([e, r, d]) => (
            <button key={e} type="button" className="btn" style={{ justifyContent: "space-between" }} onClick={() => { setEmail(e); setPw("demo1234"); }} title={d}>
              <b>{r}</b><span className="faint" style={{ fontSize: 12 }}>{d}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
