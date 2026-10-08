import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import type { OperationStatus } from "../api/types";

// ---------- formatting ----------
export const fmt = {
  ago(iso?: string) {
    if (!iso) return "-";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${(s / 3600).toFixed(1)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  },
  dt(iso?: string) { return iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "-"; },
  dur(start?: string, end?: string) {
    if (!start) return "-";
    const s = Math.max(0, ((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime()) / 1000);
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
    return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  },
  secs(s?: number) { if (s == null) return "-"; if (s === 0) return "off"; if (s < 60) return `${s} s`; if (s < 3600) return `${Math.round(s / 60)} min`; return `${(s / 3600).toFixed(1)} h`; },
  bytes(b?: number) { if (b == null) return "-"; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; } return `${b.toFixed(i ? 1 : 0)} ${u[i]}`; },
  usd(v?: number) { return v == null ? "-" : `$${v.toFixed(2)}`; },
};

// ---------- status badge ----------
const tone: Record<string, string> = {
  SUCCEEDED: "success", RUNNING: "violet", ADMITTED: "info", QUEUED: "info", PREEMPTED: "warning", FAILED: "danger", CANCELLED: "",
  ready: "success", "scaled-to-zero": "", deploying: "info", unavailable: "danger", error: "danger",
  active: "success", exhausted: "danger", expired: "",
};
export function Badge({ status, children }: { status: OperationStatus | string; children?: ReactNode }) {
  const t = tone[status] ?? "";
  return <span className={`badge ${t ? "badge--" + t : ""}`}>{children ?? status.toLowerCase().replace(/-/g, " ")}</span>;
}

// ---------- async hook ----------
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], opts: { every?: number } = {}) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fn().then((d) => { if (alive) { setData(d); setError(null); } }).catch((e) => { if (alive) setError((e as Error).message); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  useEffect(() => { if (!opts.every) return; const i = setInterval(reload, opts.every); return () => clearInterval(i); }, [opts.every, reload]);
  return { data, error, loading, reload };
}

// ---------- toasts ----------
type Toast = { id: number; text: string; err?: boolean };
const ToastCtx = createContext<(text: string, err?: boolean) => void>(() => {});
export function useToast() { return useContext(ToastCtx); }
export function ToastProvider({ children }: { children: ReactNode }) {
  const [list, setList] = useState<Toast[]>([]);
  const push = useCallback((text: string, err = false) => {
    const id = Date.now() + Math.random();
    setList((l) => [...l, { id, text, err }]);
    setTimeout(() => setList((l) => l.filter((t) => t.id !== id)), err ? 7000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts">{list.map((t) => <div key={t.id} className={`toast ${t.err ? "toast--err" : ""}`}>{t.text}</div>)}</div>
    </ToastCtx.Provider>
  );
}

// ---------- modal ----------
export function Modal({ title, onClose, children, width }: { title: string; onClose: () => void; children: ReactNode; width?: number }) {
  useEffect(() => { const f = (e: KeyboardEvent) => e.key === "Escape" && onClose(); addEventListener("keydown", f); return () => removeEventListener("keydown", f); }, [onClose]);
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={width ? { maxWidth: width } : undefined}>
        <div className="card__head"><h2>{title}</h2><button className="btn btn--ghost btn--sm" onClick={onClose} aria-label="Close">x</button></div>
        <div className="card__body">{children}</div>
      </div>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) { return <div className="empty">{children}</div>; }
export function Loading() { return <div className="empty"><span className="spin" /> Loading</div>; }
export function ErrorBox({ msg, retry }: { msg: string; retry?: () => void }) {
  return <div className="banner banner--danger"><span>{msg}</span>{retry && <button className="btn btn--sm" onClick={retry}>Retry</button>}</div>;
}
export function Stat({ label, value }: { label: string; value: ReactNode }) { return <div className="stat"><div className="label">{label}</div><div className="value">{value}</div></div>; }
export function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return <div className="field"><label>{label}</label>{children}{help && <span className="help">{help}</span>}</div>;
}
