import { useState } from "react";
import { api } from "../api/client";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  Stat,
  fmt,
  useAsync,
  useToast,
} from "../components/ui";
import { useSession } from "../components/Session";

export function Keys() {
  const { admin, key } = useSession();
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(
    () => (admin ? api().listKeys() : Promise.resolve(key ? [key] : [])),
    [admin, key?.id],
  );
  const [revoking, setRevoking] = useState<string | null>(null);
  const models = useAsync(() => api().listModels(), []);
  const [open, setOpen] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const [f, setF] = useState({
    alias: "",
    budget: 25,
    models: [] as string[],
    expires: 90,
  });
  const [busy, setBusy] = useState(false);
  const totalSpend = (data ?? []).reduce((a, k) => a + k.spend, 0);
  const totalBudget = (data ?? []).reduce((a, k) => a + (k.budget ?? 0), 0);
  const unlimitedBudget = (data ?? []).some((k) => k.budget == null);

  async function create() {
    if (
      !f.alias.trim() ||
      !Number.isFinite(f.budget) ||
      f.budget < 0 ||
      !Number.isInteger(f.expires) ||
      f.expires < 1
    ) {
      toast(
        "Enter an alias, a nonnegative budget, and a positive expiry in days.",
        true,
      );
      return;
    }
    setBusy(true);
    try {
      const k = await api().createKey({
        alias: f.alias,
        budget: f.budget,
        models: f.models,
        expires_days: f.expires,
      });
      setCreated(k.key ?? "(key not returned)");
      setOpen(false);
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  async function revoke(id: string) {
    setBusy(true);
    try {
      await api().deleteKey(id);
      toast("Key revoked");
      setRevoking(null);
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>API keys</h1>
          <p>
            Manage access, spending limits, and allowed definitions for your
            tenant.
          </p>
        </div>
        {admin && (
          <div className="actions">
            <button className="btn btn--primary" onClick={() => setOpen(true)}>
              Create key
            </button>
          </div>
        )}
      </div>
      {data && (
        <div className="stats" style={{ marginBottom: 16 }}>
          <Stat label="Keys" value={data.length} />
          <Stat
            label={admin ? "Spend (all keys)" : "Your spend"}
            value={fmt.usd(totalSpend)}
          />
          <Stat
            label={admin ? "Budget (all keys)" : "Your budget"}
            value={unlimitedBudget ? "Unlimited" : fmt.usd(totalBudget)}
          />
          <Stat
            label="Exhausted"
            value={data.filter((k) => k.status === "exhausted").length}
          />
        </div>
      )}
      {error && <ErrorBox msg={error} retry={reload} />}
      {loading && !data && <Loading />}
      {data && (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Alias</th>
                <th>Key</th>
                <th>Role</th>
                <th>Status</th>
                <th>Spend / budget</th>
                <th style={{ width: 160 }}></th>
                <th>Models</th>
                <th>Created</th>
                <th>Expires</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.length === 0 && (
                <tr>
                  <td colSpan={10}>
                    <Empty>No keys yet.</Empty>
                  </td>
                </tr>
              )}
              {data.map((k) => {
                const pct = k.budget
                  ? Math.min(100, (k.spend / k.budget) * 100)
                  : 0;
                return (
                  <tr key={k.id}>
                    <td>
                      <strong>{k.alias}</strong>
                      <div className="small muted mono">{k.id}</div>
                    </td>
                    <td className="mono">{k.key_preview}</td>
                    <td>
                      {k.role ? (
                        <span
                          className={`badge badge--plain ${k.role === "admin" ? "badge--violet" : ""}`}
                        >
                          {k.role}
                        </span>
                      ) : (
                        "-"
                      )}
                    </td>
                    <td>
                      <Badge status={k.status} />
                    </td>
                    <td className="mono">
                      {fmt.usd(k.spend)} /{" "}
                      {k.budget == null ? "Unlimited" : fmt.usd(k.budget)}
                    </td>
                    <td>
                      <div className="meter">
                        <i
                          className={pct >= 100 ? "over" : ""}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </td>
                    <td>
                      {k.models.length ? (
                        k.models.map((m) => (
                          <span className="chip" key={m}>
                            {m}
                          </span>
                        ))
                      ) : (
                        <span className="muted">all</span>
                      )}
                    </td>
                    <td>{fmt.ago(k.created_at)}</td>
                    <td>
                      {k.expires_at ? (
                        fmt.dt(k.expires_at)
                      ) : (
                        <span className="muted">never</span>
                      )}
                    </td>
                    <td>
                      {admin && (
                        <button
                          className="btn btn--sm btn--danger"
                          onClick={() => setRevoking(k.alias)}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {revoking && (
        <Modal
          title="Revoke API key?"
          onClose={() => {
            if (!busy) setRevoking(null);
          }}
        >
          <p>
            Revoke <strong>{revoking}</strong>? Applications using this key will
            lose access.
          </p>
          <div className="actions">
            <button
              className="btn"
              disabled={busy}
              onClick={() => setRevoking(null)}
            >
              Keep key
            </button>
            <button
              className="btn btn--danger"
              disabled={busy}
              onClick={() => revoke(revoking)}
            >
              Revoke key
            </button>
          </div>
        </Modal>
      )}
      {open && admin && (
        <Modal title="Create API key" onClose={() => setOpen(false)}>
          <div className="form">
            <Field label="Alias">
              <input
                className="input"
                value={f.alias}
                onChange={(e) => setF({ ...f, alias: e.target.value })}
                placeholder="notebook-rene"
                autoFocus
              />
            </Field>
            <div className="row">
              <Field
                label="Budget (USD)"
                help="Calls are refused once spend reaches the budget."
              >
                <input
                  className="input"
                  type="number"
                  min={0}
                  step={1}
                  value={f.budget}
                  onChange={(e) =>
                    setF({ ...f, budget: Number(e.target.value) })
                  }
                />
              </Field>
              <Field label="Expires in (days)">
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={f.expires}
                  onChange={(e) =>
                    setF({ ...f, expires: Number(e.target.value) })
                  }
                />
              </Field>
            </div>
            <Field
              label="Allowed models"
              help="None selected = all models of the tenant."
            >
              <div>
                {(models.data ?? []).map((m) => (
                  <label
                    key={m.id}
                    className="chip"
                    style={{ cursor: "pointer" }}
                  >
                    <input
                      type="checkbox"
                      checked={f.models.includes(m.id)}
                      onChange={(e) =>
                        setF({
                          ...f,
                          models: e.target.checked
                            ? [...f.models, m.id]
                            : f.models.filter((x) => x !== m.id),
                        })
                      }
                    />{" "}
                    {m.id}
                  </label>
                ))}
              </div>
            </Field>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn--primary"
                disabled={!f.alias || busy}
                onClick={create}
              >
                {busy && <span className="spin" />} Create
              </button>
            </div>
          </div>
        </Modal>
      )}
      {created && (
        <Modal title="Key created" onClose={() => setCreated(null)}>
          <p>Copy it now; it is not shown again.</p>
          <pre className="out">{created}</pre>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button
              className="btn"
              onClick={() =>
                navigator.clipboard
                  ?.writeText(created)
                  .then(() => toast("Copied"))
              }
            >
              Copy
            </button>
            <button
              className="btn btn--primary"
              onClick={() => setCreated(null)}
            >
              Done
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
