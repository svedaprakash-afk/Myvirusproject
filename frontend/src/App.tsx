import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api, hasToken, setToken, setUnauthorizedHandler, type Entity, type User } from "./api";
import { AppProvider, useApp } from "./ctx";
import { Loading } from "./components/ui";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Review from "./pages/Review";
import Receipts from "./pages/Receipts";
import Exceptions from "./pages/Exceptions";
import Approvals from "./pages/Approvals";
import Posting from "./pages/Posting";
import Customers from "./pages/Customers";
import Tds from "./pages/Tds";
import Import from "./pages/Import";
import Audit from "./pages/Audit";
import Advances from "./pages/Advances";
import TaxReports from "./pages/TaxReports";
import Settings from "./pages/Settings";
import Connections from "./pages/Connections";

function Shell() {
  const { user, entity, entities, setEntityId, counts } = useApp();
  const nav = useNavigate();
  const [theme, setTheme] = useState<string>(() => { try { return localStorage.getItem("theme") || "auto"; } catch { return "auto"; } });
  useEffect(() => {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch {}
  }, [theme]);
  const item = (to: string, label: string, n?: number) => <NavLink to={to} end={to === "/"}>{label}{!!n && <span className="badge-n">{n > 99 ? "99+" : n}</span>}</NavLink>;
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><i>✓</i> Cash Application</div>
        <nav className="nav" aria-label="Main">
          {item("/", "Dashboard")}
          <div className="group">Work</div>
          {item("/review", "Match review", counts.review)}
          {item("/exceptions", "Exceptions", counts.exceptions)}
          {item("/approvals", "Approvals", counts.approvals)}
          {item("/receipts", "Receipts")}
          {item("/advances", "On account")}
          <div className="group">Close the loop</div>
          {item("/posting", "ERP posting")}
          {item("/tds", "TDS tracker")}
          {item("/tax", "Tax & reports")}
          <div className="group">Data</div>
          {item("/customers", "Customers & invoices")}
          {item("/import", "Import")}
          {item("/connections", "Connections")}
          <div className="group">Governance</div>
          {item("/audit", "Audit trail")}
          {item("/settings", "Settings & AI")}
        </nav>
      </aside>
      <div className="main">
        <header className="top">
          <select aria-label="Legal entity" value={entity.id} onChange={(e) => setEntityId(Number(e.target.value))} style={{ width: "auto", maxWidth: 320 }}>
            {entities.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
          </select>
          <span className="muted mono">{entity.gstin}</span>
          <span className="sp" />
          <select aria-label="Theme" value={theme} onChange={(e) => setTheme(e.target.value)} style={{ width: "auto" }}>
            <option value="auto">Theme: system</option><option value="light">Light</option><option value="dark">Dark</option>
          </select>
          <span><b>{user.name}</b> <span className="chip">{user.role.replace("_", " ")}</span></span>
          <button className="btn sm" onClick={() => { setToken(null); nav("/login"); window.location.reload(); }}>Sign out</button>
        </header>
        <main className="page">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/review" element={<Review />} />
            <Route path="/exceptions" element={<Exceptions />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/receipts" element={<Receipts />} />
            <Route path="/advances" element={<Advances />} />
            <Route path="/posting" element={<Posting />} />
            <Route path="/tds" element={<Tds />} />
            <Route path="/tax" element={<TaxReports />} />
            <Route path="/customers" element={<Customers />} />
            <Route path="/import" element={<Import />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="/connections" element={<Connections />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

/** The identity provider sends the browser back to /#sso_token=… or /#sso_error=…; the fragment never reaches a server or its logs. */
function takeSsoFragment(): { token?: string; error?: string } {
  const m = window.location.hash.match(/^#sso_(token|error)=(.*)$/);
  if (!m) return {};
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  const v = decodeURIComponent(m[2]);
  return m[1] === "token" ? { token: v } : { error: v };
}

const SSO_RESULT = takeSsoFragment();     // once, at load: React StrictMode runs component initialisers twice, which would swallow the one-shot fragment
if (SSO_RESULT.token) setToken(SSO_RESULT.token);

export default function App() {
  const nav = useNavigate();
  const sso = SSO_RESULT;
  const [boot, setBoot] = useState<{ user: User; entities: Entity[] } | null>(null);
  const [loading, setLoading] = useState(hasToken());
  useEffect(() => { setUnauthorizedHandler(() => { setBoot(null); nav("/login"); }); }, [nav]);
  useEffect(() => {
    if (!hasToken()) return;
    Promise.all([api.get<User>("/auth/me"), api.get<Entity[]>("/entities")]).then(([user, entities]) => setBoot({ user, entities })).catch(() => setToken(null)).finally(() => setLoading(false));
  }, []);
  if (loading) return <Loading rows={6} />;
  if (!boot) return <Routes><Route path="*" element={<Login ssoError={sso.error} onLogin={(user, entities) => { setBoot({ user, entities }); nav("/"); }} />} /></Routes>;
  return <AppProvider user={boot.user} entities={boot.entities}><Shell /></AppProvider>;
}
