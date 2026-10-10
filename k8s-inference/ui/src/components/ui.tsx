import {
  Children,
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
import { Button, Icon, Modal as GravityModal } from "@gravity-ui/uikit";
import { Copy, Xmark } from "@gravity-ui/icons";
export { Button, Icon };
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
    if (s == null) return "-";
    if (s === 0) return "off";
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
  usd(v?: number) {
    return v == null ? "-" : `$${v.toFixed(2)}`;
  },
};

// ---------- status badge ----------
const tone: Record<string, string> = {
  SUCCEEDED: "success",
  RUNNING: "success",
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
  const names: Record<string, string> = {
    ready: "Running",
    "scaled-to-zero": "Idle",
    SUCCEEDED: "Completed",
    PREEMPTED: "Recovering",
  };
  return (
    <span className={`badge ${t ? "badge--" + t : ""}`}>
      {children ??
        names[status] ??
        status.slice(0, 1).toUpperCase() +
          status.slice(1).toLowerCase().replace(/-/g, " ")}
    </span>
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
      <div className="toasts">
        {list.map((t) => (
          <div key={t.id} className={`toast ${t.err ? "toast--err" : ""}`}>
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
  const heading = useId();
  return (
    <GravityModal
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      aria-labelledby={heading}
    >
      <div className="modal" style={{ width: width ?? 560 }}>
        <div className="card__head">
          <h2 id={heading}>{title}</h2>
          <Button view="flat" onClick={onClose} aria-label="Close">
            <Icon data={Xmark} />
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
    <div className="empty">
      <span className="spin" /> Loading
    </div>
  );
}
export function ErrorBox({ msg, retry }: { msg: string; retry?: () => void }) {
  return (
    <div className="banner banner--danger" role="alert">
      <span>{msg}</span>
      {retry && (
        <button className="btn btn--sm" onClick={retry}>
          Retry
        </button>
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
  error,
  children,
}: {
  label: string;
  help?: string;
  error?: string;
  children: ReactNode;
}) {
  const id = useId();
  const controls = Children.map(children, (child) =>
    isValidElement(child) &&
    (typeof child.type !== "string" ||
      ["input", "select", "textarea"].includes(child.type))
      ? cloneElement(child as ReactElement<Record<string, unknown>>, {
          id,
          "aria-describedby": help || error ? id + "-help" : undefined,
          "aria-invalid": Boolean(error),
        })
      : child,
  );
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {controls}
      {(help || error) && (
        <span id={id + "-help"} className={error ? "help field-error" : "help"}>
          {error ?? help}
        </span>
      )}
    </div>
  );
}

export function CopyButton({
  value,
  label = "Copy ID",
}: {
  value: string;
  label?: string;
}) {
  const toast = useToast();
  return (
    <Button
      view="flat"
      size="s"
      className="copy-button"
      aria-label={label}
      title={label}
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(value).then(
          () => toast("Copied"),
          () => toast("Could not copy", true),
        );
      }}
    >
      <Icon data={Copy} size={13} />
    </Button>
  );
}

export function Tabs({
  items,
  active,
  onChange,
}: {
  items: string[];
  active: string;
  onChange: (tab: string) => void;
}) {
  return (
    <div className="tabs" role="tablist" aria-label="Resource sections">
      {items.map((tab) => (
        <button
          key={tab}
          role="tab"
          aria-selected={tab === active}
          tabIndex={tab === active ? 0 : -1}
          className={tab === active ? "active" : ""}
          onClick={() => onChange(tab)}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
              return;
            event.preventDefault();
            const index = items.indexOf(tab);
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? items.length - 1
                  : (index +
                      (event.key === "ArrowRight" ? 1 : -1) +
                      items.length) %
                    items.length;
            onChange(items[next]);
            event.currentTarget.parentElement
              ?.querySelectorAll<HTMLButtonElement>("[role=tab]")
              [next]?.focus();
          }}
        >
          {tab}
        </button>
      ))}
    </div>
  );
}

export function Section({
  title,
  children,
  help,
}: {
  title: string;
  children: ReactNode;
  help?: string;
}) {
  return (
    <section className="form-section">
      <h2>{title}</h2>
      {help && <p className="help">{help}</p>}
      <div className="form">{children}</div>
    </section>
  );
}
