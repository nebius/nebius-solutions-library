import {
  cloneElement,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useId,
  useState,
  type ReactNode,
  type ReactElement,
} from "react";
import { Modal as GravityModal, Loader, Label } from "@gravity-ui/uikit";
import { Button } from "./controls";
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
  dt(iso?: string) {
    return iso
      ? new Date(iso).toLocaleString("en-GB", {
          dateStyle: "medium",
          timeStyle: "short",
        })
      : "-";
  },
  dur(start?: string, end?: string) {
    if (!start) return "-";
    const s = Math.max(
      0,
      ((end ? new Date(end).getTime() : Date.now()) -
        new Date(start).getTime()) /
        1000,
    );
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
    return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  },
  secs(s?: number) {
    if (s == null) return "—";
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  },
  bytes(b?: number) {
    if (b == null) return "-";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) {
      b /= 1024;
      i++;
    }
    return `${b.toFixed(i ? 1 : 0)} ${u[i]}`;
  },
  usd(v?: number | null) {
    return v == null ? "—" : `$${v.toFixed(2)}`;
  },
};

// ---------- status badge ----------
const tone: Record<string, string> = {
  SUCCEEDED: "success",
  RUNNING: "violet",
  ADMITTED: "info",
  QUEUED: "info",
  PREEMPTED: "warning",
  FAILED: "danger",
  CANCELLED: "",
  ready: "success",
  "scaled-to-zero": "",
  deploying: "info",
  unavailable: "danger",
  error: "danger",
  active: "success",
  exhausted: "danger",
  expired: "",
};
export function Badge({
  status,
  children,
}: {
  status: OperationStatus | string;
  children?: ReactNode;
}) {
  const t = tone[status] ?? "";
  const theme =
    (
      {
        success: "success",
        violet: "info",
        info: "info",
        warning: "warning",
        danger: "danger",
      } as const
    )[t as "success" | "violet" | "info" | "warning" | "danger"] ?? "normal";
  return (
    <Label theme={theme} size="m" className="status-label">
      <span className={`status-dot ${t}`} />
      {children ?? status.toLowerCase().replace(/-/g, " ")}
    </Label>
  );
}

// ---------- async hook ----------
export function useAsync<T>(
  fn: () => Promise<T>,
  deps: unknown[],
  opts: { every?: number } = {},
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    setData(null);
    setError(null);
  }, deps);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fn()
      .then((d) => {
        if (alive) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (alive) setError((e as Error).message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  useEffect(() => {
    if (!opts.every) return;
    const i = setInterval(reload, opts.every);
    return () => clearInterval(i);
  }, [opts.every, reload]);
  return { data, error, loading, reload };
}

// ---------- toasts ----------
type Toast = { id: number; text: string; err?: boolean };
const ToastCtx = createContext<(text: string, err?: boolean) => void>(() => {});
export function useToast() {
  return useContext(ToastCtx);
}
export function ToastProvider({ children }: { children: ReactNode }) {
  const [list, setList] = useState<Toast[]>([]);
  const push = useCallback((text: string, err = false) => {
    const id = Date.now() + Math.random();
    setList((l) => [...l, { id, text, err }]);
    setTimeout(
      () => setList((l) => l.filter((t) => t.id !== id)),
      err ? 7000 : 3500,
    );
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {list.map((t) => (
          <div
            key={t.id}
            role={t.err ? "alert" : "status"}
            className={`toast ${t.err ? "toast--err" : ""}`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ---------- modal ----------
export function Modal({
  title,
  onClose,
  children,
  width,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  width?: number;
}) {
  const id = useId();
  return (
    <GravityModal
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      aria-labelledby={id}
      contentClassName="console-modal"
    >
      <div style={{ width: width ?? 520, maxWidth: "calc(100vw - 32px)" }}>
        <div className="card__head">
          <h2 id={id}>{title}</h2>
          <Button view="flat" onClick={onClose} aria-label="Close dialog">
            ×
          </Button>
        </div>
        <div className="card__body">{children}</div>
      </div>
    </GravityModal>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
export function Loading() {
  return (
    <div className="empty" role="status">
      <Loader size="m" />
      <span>Loading resources…</span>
    </div>
  );
}
export function ErrorBox({ msg, retry }: { msg: string; retry?: () => void }) {
  return (
    <div className="banner banner--danger" role="alert">
      <span>{msg}</span>
      {retry && (
        <Button size="m" onClick={retry}>
          Retry
        </Button>
      )}
    </div>
  );
}
export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}
export function Field({
  label,
  help,
  children,
  error,
}: {
  label: string;
  help?: string;
  children: ReactNode;
  error?: string;
}) {
  const id = useId();
  const child = isValidElement(children)
    ? (children as ReactElement<Record<string, unknown>>)
    : null;
  const control =
    child && child.type !== "div" && child.type !== "label"
      ? cloneElement(child, {
          id: child.props.id ?? id,
          "aria-describedby": `${id}-help`,
          "aria-invalid": !!error,
        })
      : children;
  return (
    <div className="field">
      <label htmlFor={child ? String(child.props.id ?? id) : undefined}>
        {label}
      </label>
      {control}
      <span id={`${id}-help`} className={error ? "field-error" : "help"}>
        {error ?? help}
      </span>
    </div>
  );
}
