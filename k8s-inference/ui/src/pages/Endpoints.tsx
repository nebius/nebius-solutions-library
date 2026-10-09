import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { grafanaUrl } from "../config";
import type { Endpoint } from "../api/types";
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
  Area,
  Pick,
  Search,
  PageHeader,
  Tabs,
} from "../components/controls";
import { useAdmin, useSession } from "../components/Session";
import { href } from "../router";
import { endpointPath } from "./Models";

export function Endpoints() {
  const admin = useAdmin();
  const { fleet } = useSession();
  const [region, setRegion] = useState("");
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const { data, error, loading, reload } = useAsync(
    () => api().listEndpoints(region || undefined),
    [region],
    { every: 10000 },
  );
  const regions = [
    ...new Set([
      ...(fleet?.regions ?? []),
      ...(data ?? []).map((e) => e.region),
    ]),
  ];
  const rows = (data ?? []).filter(
    (e) =>
      (status === "all" || status === e.status) &&
      `${e.name} ${e.model} ${e.id}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  return (
    <>
      <PageHeader
        title="Endpoints"
        count={data?.length}
        description="Deploy inference APIs that scale with demand."
      >
        {admin && (
          <Button primary href={href("/endpoints/new")}>
            Create endpoint
          </Button>
        )}
      </PageHeader>
      {error && <ErrorBox msg={error} retry={reload} />}
      <div className="toolbar">
        <div className="toolbar__search">
          <Search
            value={search}
            onChange={setSearch}
            label="Search endpoints"
          />
        </div>
        <Pick
          value={region}
          onChange={setRegion}
          aria-label="Region"
          placeholder="All regions"
          options={[{ value: "", label: "All regions" }, ...regions]}
        />
        <Pick
          value={status}
          onChange={setStatus}
          aria-label="Endpoint status"
          options={[
            { value: "all", label: "All statuses" },
            { value: "ready", label: "Ready" },
            { value: "scaled-to-zero", label: "Scaled to zero" },
            { value: "deploying", label: "Deploying" },
            { value: "error", label: "Error" },
          ]}
        />
        <span className="toolbar__count">{rows.length} endpoints</span>
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
                  <th>Region</th>
                  <th>GPU</th>
                  <th>Ready replicas</th>
                  <th>Created</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {!rows.length && (
                  <tr>
                    <td colSpan={7}>
                      <Empty>
                        <div className="empty-symbol">↗</div>
                        <h2>
                          {search || status !== "all"
                            ? "No matching endpoints"
                            : "Deploy your first endpoint"}
                        </h2>
                        <p>
                          {search || status !== "all"
                            ? "Try changing the search or filters."
                            : "Serve a model from your container image with automatic scaling."}
                        </p>
                        {admin && !search && status === "all" && (
                          <Button primary href={href("/endpoints/new")}>
                            Create endpoint
                          </Button>
                        )}
                      </Empty>
                    </td>
                  </tr>
                )}
                {rows.map((e) => (
                  <tr key={`${e.region}:${e.id}`}>
                    <td>
                      <a
                        className="strong"
                        href={href(endpointPath(e.id, e.region))}
                      >
                        {e.name}
                      </a>
                      <div className="small muted mono">{e.model}</div>
                    </td>
                    <td>
                      <Badge status={e.status} />
                    </td>
                    <td>{e.region}</td>
                    <td>{e.gpu || "—"}</td>
                    <td>
                      {e.replicas_ready}
                      <span className="muted"> / {e.max_replicas}</span>
                    </td>
                    <td title={fmt.dt(e.created_at)}>
                      {fmt.ago(e.created_at)}
                    </td>
                    <td>
                      <Button
                        size="m"
                        view="flat"
                        href={href(endpointPath(e.id, e.region))}
                      >
                        Open
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </>
  );
}
export function sampleBody(e: Endpoint) {
  return ["openai", "openai-chat"].includes(e.protocol ?? "")
    ? {
        messages: [{ role: "user", content: "Say hello in one sentence." }],
        max_tokens: 64,
      }
    : {};
}
export function curlFor(e: Endpoint, body: string) {
  let input: unknown;
  try {
    input = JSON.parse(body);
  } catch {
    return "Enter valid JSON to generate the example.";
  }
  const json = JSON.stringify({
    mode: "sync",
    region: e.region,
    input,
  }).replace(/'/g, "'\\''");
  return `curl -sS '${e.url.replace(/'/g, "'\\''")}' \\\n  -H "Authorization: Bearer $API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${json}'`;
}
export function EndpointDetail({
  id,
  region,
}: {
  id: string;
  region?: string;
}) {
  const admin = useAdmin();
  const toast = useToast();
  const {
    data: e,
    error,
    loading,
    reload,
  } = useAsync(() => api().getEndpoint(id, region), [id, region], {
    every: 10000,
  });
  const [tab, setTab] = useState("overview");
  const [body, setBody] = useState("");
  const [output, setOutput] = useState<{
    status?: number;
    ms: number;
    body: unknown;
    error?: boolean;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState(false);
  const [scaleBusy, setScaleBusy] = useState(false);
  const [scaleError, setScaleError] = useState("");
  const [scale, setScale] = useState({ min: 0, max: 1, idle: 120, conc: 4 });
  useEffect(() => {
    if (e) setBody((v) => v || JSON.stringify(sampleBody(e), null, 2));
  }, [e?.model]);
  function openScale() {
    if (!e) return;
    setScale({
      min: e.min_replicas,
      max: e.max_replicas,
      idle: e.scale_to_zero_after_s,
      conc: e.target_concurrency ?? 4,
    });
    setScaleError("");
    setEdit(true);
  }
  async function send(ev: React.FormEvent) {
    ev.preventDefault();
    if (!e) return;
    setBusy(true);
    setOutput(null);
    const t0 = performance.now();
    try {
      const input: unknown = JSON.parse(body);
      if (!input || Array.isArray(input) || typeof input !== "object")
        throw new Error("Request body must be a JSON object.");
      const r = await api().invoke(e.model, {
        mode: "sync",
        input: input as Record<string, unknown>,
        region: e.region,
        timeout_s: 600,
      });
      setOutput({
        status: 200,
        ms: Math.round(performance.now() - t0),
        body: "result" in r ? r.result : r,
      });
    } catch (err) {
      setOutput({
        status: err instanceof ApiError ? err.status : undefined,
        ms: Math.round(performance.now() - t0),
        body: (err as Error).message,
        error: true,
      });
    } finally {
      setBusy(false);
    }
  }
  async function saveScale(ev: React.FormEvent) {
    ev.preventDefault();
    if (!e) return;
    if (
      ![scale.min, scale.max, scale.idle, scale.conc].every(Number.isInteger) ||
      scale.min < 0 ||
      scale.max < Math.max(1, scale.min) ||
      scale.idle < 0 ||
      scale.conc < 1
    ) {
      setScaleError(
        "Use whole numbers. Maximum must be at least one and no less than minimum; concurrency must be positive.",
      );
      return;
    }
    setScaleBusy(true);
    setScaleError("");
    try {
      await api().updateEndpoint(
        id,
        {
          min_replicas: scale.min,
          max_replicas: scale.max,
          scale_to_zero_after_s: scale.idle,
          target_concurrency: scale.conc,
        },
        e.region,
      );
      toast("Scaling updated");
      setEdit(false);
      reload();
    } catch (err) {
      setScaleError((err as Error).message);
    } finally {
      setScaleBusy(false);
    }
  }
  if (error && !e) return <ErrorBox msg={error} retry={reload} />;
  if (loading && !e) return <Loading />;
  if (!e) return null;
  const grafana = grafanaUrl(e.region);
  const canTry = !["grpc", "websocket"].includes(e.protocol ?? "");
  return (
    <>
      <PageHeader
        title={
          <>
            {e.name}
            <Badge status={e.status} />
          </>
        }
        description={`${e.model} · ${e.region}`}
      >
        {grafana && (
          <Button
            href={`${grafana}/d/knative-serving?var-service=${encodeURIComponent(e.id)}`}
            target="_blank"
            rel="noreferrer"
          >
            Metrics ↗
          </Button>
        )}
        {admin && e.managed_by === "api" && (
          <Button href={href(`/models/${encodeURIComponent(e.model)}/edit`)}>
            Edit configuration
          </Button>
        )}
      </PageHeader>
      {error && <ErrorBox msg={error} retry={reload} />}
      <Tabs
        className="resource-tabs"
        activeTab={tab}
        onSelectTab={setTab}
        aria-label="Endpoint details"
        items={[
          { id: "overview", title: "Overview" },
          { id: "configuration", title: "Configuration" },
          { id: "request", title: "Try it" },
        ]}
      />
      {tab === "overview" && (
        <div className="detail-stack">
          <div className="stats">
            <Stat
              label="Ready replicas"
              value={`${e.replicas_ready} / ${e.max_replicas}`}
            />
            <Stat label="GPU" value={e.gpu || "—"} />
            <Stat
              label="Scale to zero after"
              value={fmt.secs(e.scale_to_zero_after_s)}
            />
            {e.in_flight != null && (
              <Stat label="In-flight requests" value={e.in_flight} />
            )}
            {e.last_cold_start_s != null && (
              <Stat
                label="Last cold start"
                value={`${e.last_cold_start_s} s`}
              />
            )}
          </div>
          <section className="card">
            <div className="card__head">
              <h2>Endpoint information</h2>
              <Button size="m" onClick={() => setTab("request")}>
                Try it
              </Button>
            </div>
            <div className="card__body">
              <dl className="details-kv">
                <dt>Endpoint ID</dt>
                <dd className="mono">{e.id}</dd>
                <dt>Model</dt>
                <dd>{e.model}</dd>
                <dt>Region</dt>
                <dd>{e.region}</dd>
                <dt>Protocol</dt>
                <dd>{e.protocol || "—"}</dd>
                <dt>Created</dt>
                <dd>{fmt.dt(e.created_at)}</dd>
                <dt>API URL</dt>
                <dd className="mono break-word">{e.url}</dd>
              </dl>
            </div>
          </section>
          <section className="card">
            <div className="card__body">
              <div className="row row-between">
                <div>
                  <h2>Monitoring</h2>
                  <p className="muted">
                    {grafana
                      ? "View live metrics and logs for this endpoint."
                      : "Monitoring is not configured for this console."}
                  </p>
                </div>
                {grafana && (
                  <Button
                    href={`${grafana}/explore?left=${encodeURIComponent(JSON.stringify({ datasource: "Loki", queries: [{ expr: `{app="${e.id}-predictor"}` }] }))}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open logs ↗
                  </Button>
                )}
              </div>
            </div>
          </section>
        </div>
      )}
      {tab === "configuration" && (
        <section className="card">
          <div className="card__head">
            <h2>Autoscaling</h2>
            {admin && (
              <Button size="m" onClick={openScale}>
                Edit scaling
              </Button>
            )}
          </div>
          <div className="card__body form">
            <dl className="details-kv">
              <dt>Minimum replicas</dt>
              <dd>{e.min_replicas}</dd>
              <dt>Maximum replicas</dt>
              <dd>{e.max_replicas}</dd>
              <dt>Target concurrency</dt>
              <dd>{e.target_concurrency ?? "—"}</dd>
              <dt>Scale to zero after</dt>
              <dd>{fmt.secs(e.scale_to_zero_after_s)}</dd>
              {e.placement && (
                <>
                  <dt>Placement</dt>
                  <dd>{e.placement.toLowerCase()}</dd>
                </>
              )}
              <dt>Configuration source</dt>
              <dd>
                {e.managed_by === "api"
                  ? "Custom model"
                  : "Deployment configuration"}
              </dd>
            </dl>
            <div className="banner banner--info">
              {e.managed_by === "git"
                ? "This endpoint is managed by the deployment. Live scaling changes may be replaced during synchronization."
                : "Live scaling changes apply to this region. Edit the model configuration to persist replica limits across redeployments."}
            </div>
          </div>
        </section>
      )}
      {tab === "request" &&
        (canTry ? (
          <div className="grid grid--2">
            <form className="card" onSubmit={send}>
              <div className="card__head">
                <h2>Send a request</h2>
                <span className="context-tag">POST</span>
              </div>
              <div className="card__body form">
                <Field label="API URL">
                  <Input value={e.url} mono readOnly />
                </Field>
                <Field label="Request body (JSON)">
                  <Area rows={10} value={body} onChange={setBody} />
                </Field>
                {e.status === "scaled-to-zero" && (
                  <div className="banner banner--info">
                    The first request waits for a replica to start.
                  </div>
                )}
                <div className="form-actions">
                  <Button primary type="submit" loading={busy}>
                    Send request
                  </Button>
                </div>
              </div>
            </form>
            <div className="detail-stack">
              <section className="card">
                <div className="card__head">
                  <h2>Code example</h2>
                </div>
                <div className="card__body">
                  <p className="help">
                    Set API_KEY to your key before running this example.
                  </p>
                  <pre className="out">{curlFor(e, body)}</pre>
                </div>
              </section>
              <section className="card">
                <div className="card__head">
                  <h2>Response</h2>
                  {output && (
                    <span
                      className={output.error ? "field-error" : "small muted"}
                    >
                      {output.status ? `HTTP ${output.status} · ` : ""}
                      {output.ms} ms
                    </span>
                  )}
                </div>
                <div className="card__body">
                  {output ? (
                    <pre
                      className="out"
                      role={output.error ? "alert" : undefined}
                    >
                      {typeof output.body === "string"
                        ? output.body
                        : JSON.stringify(output.body, null, 2)}
                    </pre>
                  ) : (
                    <p className="muted">Send a request to see the response.</p>
                  )}
                </div>
              </section>
            </div>
          </div>
        ) : (
          <div className="empty">
            <h2>Use a {e.protocol} client</h2>
            <p>
              The JSON request tester supports HTTP and OpenAI compatible
              endpoints.
            </p>
          </div>
        ))}
      {edit && (
        <Modal
          title="Edit scaling"
          onClose={() => !scaleBusy && setEdit(false)}
        >
          <form className="form" onSubmit={saveScale}>
            <div className="form-grid">
              {(
                [
                  ["min", "Minimum replicas"],
                  ["max", "Maximum replicas"],
                  ["conc", "Target concurrency"],
                  ["idle", "Scale to zero after (seconds)"],
                ] as const
              ).map(([k, label]) => (
                <Field key={k} label={label}>
                  <Input
                    type="number"
                    value={scale[k]}
                    onChange={(v) => setScale({ ...scale, [k]: Number(v) })}
                    controlProps={{
                      min: k === "max" || k === "conc" ? 1 : 0,
                      step: 1,
                      required: true,
                    }}
                  />
                </Field>
              ))}
            </div>
            {scaleError && <ErrorBox msg={scaleError} />}
            <div className="dialog-actions">
              <Button disabled={scaleBusy} onClick={() => setEdit(false)}>
                Cancel
              </Button>
              <Button primary type="submit" loading={scaleBusy}>
                Save changes
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
