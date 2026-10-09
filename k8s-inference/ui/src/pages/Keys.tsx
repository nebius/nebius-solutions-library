import { useState } from "react";
import { api, settings } from "../api/client";
import type { ApiKey } from "../api/types";
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
import {
  Button,
  Input,
  MultiPick,
  Search,
  PageHeader,
} from "../components/controls";
import { useAdmin, useSession } from "../components/Session";
export function Keys() {
  const admin = useAdmin();
  const { key: currentKey } = useSession();
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(
    () =>
      admin
        ? api().listKeys()
        : api()
            .keyInfo()
            .then((k) => [k]),
    [admin],
  );
  const models = useAsync(() => api().listModels(), []);
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [f, setF] = useState({
    alias: "",
    budget: 25,
    models: [] as string[],
    expires: 90,
  });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const rows = (data ?? []).filter((k) =>
    `${k.alias} ${k.key_preview}`.toLowerCase().includes(search.toLowerCase()),
  );
  async function create(ev: React.FormEvent) {
    ev.preventDefault();
    setFormError("");
    if (
      !f.alias.trim() ||
      !Number.isFinite(f.budget) ||
      f.budget < 0 ||
      !Number.isInteger(f.expires) ||
      f.expires < 1
    ) {
      setFormError(
        "Enter a name, a non-negative budget and a positive whole number of days.",
      );
      return;
    }
    setBusy(true);
    try {
      const k = await api().createKey({
        alias: f.alias.trim(),
        budget: f.budget,
        models: f.models,
        expires_days: f.expires,
      });
      setOpen(false);
      if (k.key) setCreated(k.key);
      else toast("Key created, but no secret was returned by the API.", true);
      reload();
    } catch (err) {
      setFormError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function revoke() {
    if (!revoking) return;
    setBusy(true);
    setFormError("");
    try {
      await api().deleteKey(revoking.alias);
      toast("API key revoked");
      if (revoking.alias === currentKey?.alias) {
        settings.key = "";
        location.reload();
        return;
      }
      setRevoking(null);
      reload();
    } catch (err) {
      setFormError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function copy() {
    try {
      if (!navigator.clipboard)
        throw new Error("Clipboard unavailable. Select and copy the key.");
      await navigator.clipboard.writeText(created!);
      toast("API key copied");
    } catch (err) {
      toast((err as Error).message, true);
    }
  }
  return (
    <>
      <PageHeader
        title="API keys"
        count={data?.length}
        description={
          admin
            ? "Manage access, usage budgets and model permissions."
            : "Your current API key and usage."
        }
      >
        {admin && (
          <Button
            primary
            onClick={() => {
              setF({ alias: "", budget: 25, models: [], expires: 90 });
              setFormError("");
              setOpen(true);
            }}
          >
            Create API key
          </Button>
        )}
      </PageHeader>
      {data && (
        <div className="stats" style={{ marginBottom: 24 }}>
          <Stat label="API keys" value={data.length} />
          <Stat
            label="Total spend"
            value={fmt.usd(data.reduce((a, k) => a + k.spend, 0))}
          />
          <Stat
            label="Active keys"
            value={data.filter((k) => k.status === "active").length}
          />
        </div>
      )}
      {error && <ErrorBox msg={error} retry={reload} />}
      <div className="toolbar">
        <div className="toolbar__search">
          <Search value={search} onChange={setSearch} label="Search API keys" />
        </div>
        <span className="toolbar__count">{rows.length} keys</span>
        <Button size="m" onClick={reload}>
          Refresh
        </Button>
      </div>
      {loading && !data ? (
        <Loading />
      ) : (
        data && (
          <div className="table-surface tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                  <th>Spend / budget</th>
                  <th>Models</th>
                  <th>Expires</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {!rows.length && (
                  <tr>
                    <td colSpan={6}>
                      <Empty>
                        <h2>
                          {search ? "No matching API keys" : "No API keys"}
                        </h2>
                        <p>
                          {search
                            ? "Try a different name."
                            : "Create an API key to give an application access."}
                        </p>
                      </Empty>
                    </td>
                  </tr>
                )}
                {rows.map((k) => {
                  const pct =
                    k.budget != null && k.budget > 0
                      ? Math.min(100, (k.spend / k.budget) * 100)
                      : 0;
                  return (
                    <tr key={k.id}>
                      <td>
                        <strong>{k.alias || "Unnamed key"}</strong>
                        <div className="mono small muted">{k.key_preview}</div>
                        {k.role === "admin" && (
                          <span className="chip">Administrator</span>
                        )}
                      </td>
                      <td>
                        <Badge status={k.status} />
                      </td>
                      <td>
                        {fmt.usd(k.spend)}{" "}
                        <span className="muted">
                          / {k.budget == null ? "Unlimited" : fmt.usd(k.budget)}
                        </span>
                        {k.budget != null && k.budget > 0 && (
                          <div
                            className="meter"
                            role="meter"
                            aria-label={`${k.alias} budget usage`}
                            aria-valuenow={pct}
                            aria-valuemin={0}
                            aria-valuemax={100}
                          >
                            <i
                              className={pct === 100 ? "over" : ""}
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                        )}
                      </td>
                      <td>
                        {k.models.length ? k.models.join(", ") : "All models"}
                      </td>
                      <td>
                        {k.expires_at ? fmt.dt(k.expires_at) : "No expiration"}
                      </td>
                      <td>
                        {admin && (
                          <Button
                            size="m"
                            view="flat-danger"
                            onClick={() => {
                              setFormError("");
                              setRevoking(k);
                            }}
                          >
                            Revoke
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}
      {open && (
        <Modal title="Create API key" onClose={() => !busy && setOpen(false)}>
          <form className="form" onSubmit={create}>
            <Field label="Name">
              <Input
                value={f.alias}
                onChange={(v) => setF({ ...f, alias: v })}
                autoFocus
                placeholder="my-application"
                controlProps={{ required: true }}
              />
            </Field>
            <div className="form-grid">
              <Field
                label="Budget (USD)"
                help="Requests stop when the budget is reached."
              >
                <Input
                  type="number"
                  value={f.budget}
                  onChange={(v) => setF({ ...f, budget: Number(v) })}
                  controlProps={{ min: 0, step: "any", required: true }}
                />
              </Field>
              <Field label="Expires in (days)">
                <Input
                  type="number"
                  value={f.expires}
                  onChange={(v) => setF({ ...f, expires: Number(v) })}
                  controlProps={{ min: 1, step: 1, required: true }}
                />
              </Field>
            </div>
            <Field
              label="Allowed models"
              help="Leave empty to allow all models."
            >
              <MultiPick
                value={f.models}
                onUpdate={(v) => setF({ ...f, models: v })}
                placeholder="All models"
                options={(models.data ?? []).map((m) => ({
                  value: m.id,
                  label: m.name,
                }))}
                loading={models.loading}
              />
            </Field>
            {models.error && (
              <ErrorBox msg={models.error} retry={models.reload} />
            )}
            {formError && <ErrorBox msg={formError} />}
            <div className="dialog-actions">
              <Button disabled={busy} onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button
                primary
                type="submit"
                loading={busy}
                disabled={!models.data}
              >
                Create API key
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {created && (
        <Modal title="API key created" onClose={() => setCreated(null)}>
          <div className="form">
            <p>Copy this key now. You will not be able to view it again.</p>
            <pre className="out">{created}</pre>
            <div className="dialog-actions">
              <Button onClick={copy}>Copy key</Button>
              <Button primary onClick={() => setCreated(null)}>
                Done
              </Button>
            </div>
          </div>
        </Modal>
      )}
      {revoking && (
        <Modal
          title="Revoke API key"
          onClose={() => !busy && setRevoking(null)}
        >
          <div className="form">
            <p>
              Revoke <strong>{revoking.alias}</strong>? Applications using this
              key will lose access.
              {revoking.alias === currentKey?.alias
                ? " You will also be signed out."
                : ""}
            </p>
            {formError && <ErrorBox msg={formError} />}
            <div className="dialog-actions">
              <Button disabled={busy} onClick={() => setRevoking(null)}>
                Cancel
              </Button>
              <Button danger loading={busy} onClick={revoke}>
                Revoke key
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
