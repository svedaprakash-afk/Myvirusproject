import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type Entity, type User } from "./api";

interface Ctx {
  user: User; entity: Entity; entities: Entity[]; setEntityId: (id: number) => void;
  toast: (msg: string, bad?: boolean) => void; counts: { review: number; exceptions: number; approvals: number }; refresh: () => void; tick: number;
  can: (...roles: User["role"][]) => boolean;
}
const C = createContext<Ctx | null>(null);
export const useApp = () => { const c = useContext(C); if (!c) throw new Error("outside provider"); return c; };

export function AppProvider({ user, entities, children }: { user: User; entities: Entity[]; children: ReactNode }) {
  const [entityId, setEntityId] = useState<number>(() => { try { return Number(localStorage.getItem("entity")) || entities[0].id; } catch { return entities[0].id; } });
  const entity = entities.find((e) => e.id === entityId) ?? entities[0];
  const [msg, setMsg] = useState<{ t: string; bad: boolean } | null>(null);
  const timer = useRef<number>();
  const [counts, setCounts] = useState({ review: 0, exceptions: 0, approvals: 0 });
  const [tick, setTick] = useState(0);

  const toast = useCallback((t: string, bad = false) => { setMsg({ t, bad }); window.clearTimeout(timer.current); timer.current = window.setTimeout(() => setMsg(null), bad ? 7000 : 3500); }, []);
  const refresh = useCallback(() => setTick((x) => x + 1), []);
  const setE = (id: number) => { try { localStorage.setItem("entity", String(id)); } catch {} setEntityId(id); };

  useEffect(() => {
    let live = true;
    api.get<any>(`/analytics/summary?entity_id=${entity.id}`).then((s) => live && setCounts({ review: s.queues.review, exceptions: s.queues.exceptions, approvals: s.queues.approvals_pending })).catch(() => {});
    return () => { live = false; };
  }, [entity.id, tick]);

  const can = useCallback((...r: User["role"][]) => r.includes(user.role), [user.role]);
  const value = useMemo(() => ({ user, entity, entities, setEntityId: setE, toast, counts, refresh, tick, can }), [user, entity, entities, toast, counts, refresh, tick, can]);
  return (
    <C.Provider value={value}>
      {children}
      {msg && <div className={`toast ${msg.bad ? "bad" : ""}`} role="status" aria-live="polite">{msg.t}</div>}
    </C.Provider>
  );
}
