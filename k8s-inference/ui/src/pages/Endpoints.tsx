import { useEffect, useState } from "react";
import { ArrowsRotateRight, ChartLine, Play, Plus } from "@gravity-ui/icons";
import { api } from "../api/client";
import type { Endpoint, ScalingSpec } from "../api/types";
import {
  ScalingFields,
  endpointScaling,
  scalingThreshold,
  scalingValidation,
  scalingMetricLabel,
  isRequestRate,
} from "../components/ScalingFields";
import { useSession } from "../components/Session";
import {
  Badge,
  Button,
  CopyButton,
  ErrorBox,
  Field,
  Icon,
  Loading,
  Modal,
  Tabs,
  fmt,
  useAsync,
  useToast,
} from "../components/ui";
import {
  DetailCard,
  EmptyList,
  PageHeader,
  ResourceHeader,
  ResourceName,
  RowMenu,
  SearchToolbar,
  resourcePath,
  useResourceTab,
} from "../components/Resource";
import { ResourceMonitoring } from "../components/LazyMonitoring";
import { href, navigate, useRoute } from "../router";

export function Endpoints() {
  const { admin } = useSession();
  const { data, error, loading, reload } = useAsync(
    () => api().listEndpoints(),
    [],
    { every: 10000 },
  );
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [region, setRegion] = useState("");
  const modelFilter = useRoute().query.get("model");
  useEffect(() => {
    setSearch(modelFilter ?? "");
  }, [modelFilter]);
  const rows = (data ?? [])
    .filter(
      (e) =>
        (!search ||
          `${e.name} ${e.id} ${e.model}`
            .toLowerCase()
            .includes(search.toLowerCase())) &&
        (!status || e.status === status) &&
        (!region || e.region === region),
    )
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
  const reset = () => {
    setSearch("");
    setStatus("");
    setRegion("");
  };
  return (
    <>
      <PageHeader
        title="Endpoints"
        count={data?.length}
        create={
          admin ? { label: "Create endpoint", to: "/endpoints/new" } : undefined
        }
      />
      <SearchToolbar
        {...{ search, setSearch, status, setStatus, region, setRegion }}
        statuses={[
          ["ready", "Running"],
          ["scaled-to-zero", "Idle"],
          ["deploying", "Deploying"],
          ["error", "Error"],
          ["unavailable", "Unavailable"],
        ]}
        regions={[...new Set(data?.map((e) => e.region) ?? [])]}
      >
        <Button
          view="flat"
          size="l"
          aria-label="Refresh endpoints"
          onClick={reload}
        >
          <Icon data={ArrowsRotateRight} />
        </Button>
      </SearchToolbar>
      {error && <ErrorBox msg={error} retry={reload} />}{" "}
      {loading && !data && <Loading />}
      {data &&
        (rows.length ? (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Name and ID</th>
                  <th>Status</th>
                  <th>Hardware</th>
                  <th>Region</th>
                  <th>Replicas</th>
                  <th>Created</th>
                  <th></th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e) => {
                  const path = resourcePath("endpoints", e.id, e.region);
                  return (
                    <tr key={e.id + e.region}>
                      <td className="name-cell">
                        <ResourceName name={e.name} id={e.id} to={path} />
                      </td>
                      <td>
                        <Badge status={e.status} />
                      </td>
                      <td>
                        {e.gpu ?? "—"}
                        <div className="small muted">
                          {e.cpu ? `${e.cpu} CPUs · ${e.memory}` : e.model}
                        </div>
                      </td>
                      <td>{e.region}</td>
                      <td>
                        {e.replicas_ready ?? "—"}
                        {(e.nodes ?? 1) > 1 && (
                          <span className="small muted">
                            {" "}
                            × {e.nodes} nodes
                          </span>
                        )}
                        <div className="small muted">
                          {e.min_replicas != null
                            ? (e.nodes ?? 1) > 1
                              ? e.min_replicas
                                ? "started"
                                : "stopped"
                              : `${e.min_replicas}–${e.max_replicas} configured`
                            : "State unavailable"}
                          {e.startup_s != null && (
                            <span title={`started ${fmt.dt(e.started_at)}, ready ${fmt.dt(e.ready_at)}`}>
                              {" "}
                              · last start-up {fmt.secs(e.startup_s)}
                            </span>
                          )}
                        </div>
                      </td>
                      <td title={fmt.dt(e.created_at)}>
                        {fmt.ago(e.created_at)}
                      </td>
                      <td>
                        <Button
                          view="outlined"
                          size="l"
                          href={href(
                            resourcePath("endpoints", e.id, e.region, "Logs"),
                          )}
                        >
                          View logs
                        </Button>
                      </td>
                      <td>
                        <RowMenu
                          actions={[
                            {
                              label: "View endpoint",
                              action: () => navigate(path),
                            },
                            {
                              label: "View metrics",
                              action: () =>
                                navigate(
                                  resourcePath(
                                    "endpoints",
                                    e.id,
                                    e.region,
                                    "Metrics",
                                  ),
                                ),
                            },
                            {
                              label: "Test request",
                              action: () =>
                                navigate(
                                  resourcePath(
                                    "endpoints",
                                    e.id,
                                    e.region,
                                    "Test request",
                                  ),
                                ),
                            },
                          ]}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyList
            resource="endpoints"
            filtered={Boolean(search || status || region)}
            reset={reset}
            action={
              admin && (
                <Button view="action" size="l" href={href("/endpoints/new")}>
                  <Icon data={Plus} /> Create endpoint
                </Button>
              )
            }
          />
        ))}
    </>
  );
}

const TABS = [
  "Overview",
  "Metrics",
  "Logs",
  "Scaling",
  "Test request",
  "Settings",
];
export function sampleBody(e: Endpoint) {
  return e.protocol?.startsWith("openai")
    ? {
        messages: [{ role: "user", content: "Say hello in one sentence." }],
        max_tokens: 64,
      }
    : {};
}
export function curlFor(endpoint: Endpoint, body: string) {
  let input: unknown;
  try {
    input = JSON.parse(body);
  } catch {
    return "Enter valid JSON to generate the example.";
  }
  const payload = JSON.stringify({
    mode: "sync",
    region: endpoint.region,
    input,
  }).replace(/'/g, "'\\''");
  return `curl -sS '${endpoint.url.replace(/'/g, "'\\''")}' \\\n  -H "Authorization: Bearer $API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${payload}'`;
}
export function EndpointDetail({
  id,
  region,
}: {
  id: string;
  region?: string;
}) {
  const { admin } = useSession();
  const toast = useToast();
  const { tab, setTab } = useResourceTab(TABS);
  const {
    data: e,
    error,
    loading,
    reload,
  } = useAsync(() => api().getEndpoint(id, region), [id, region], {
    every: 10000,
  });
  const [edit, setEdit] = useState(false);
  const [busy, setBusy] = useState(false);
  const [scale, setScale] = useState<ScalingSpec>({
    min: 0,
    max: 1,
    idle_s: 120,
    target: 4,
  });
  const [timeout, setTimeoutValue] = useState(600);
  const [scaleError, setScaleError] = useState("");
  const [remove, setRemove] = useState(false);
  useEffect(() => {
    if (e && !edit) {
      setScale(endpointScaling(e));
      setTimeoutValue(e.timeout_s ?? 600);
    }
  }, [e, edit]);
  async function saveScale(event: React.FormEvent) {
    event.preventDefault();
    if (!e) return;
    if (scalingValidation(scale)) {
      setScaleError(scalingValidation(scale)!);
      return;
    }
    setBusy(true);
    setScaleError("");
    try {
      await api().updateEndpoint(
        id,
        {
          scaling: scale,
          timeout_s: timeout,
        },
        e.region,
      );
      toast("Scaling updated in all deployment regions");
      setEdit(false);
      reload();
    } catch (err) {
      setScaleError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function startStop(min: 0 | 1) {
    // a multi-node endpoint runs its replica group of whole nodes while scaling.min is 1 (no autoscaling);
    // the platform stops it after scaling.idle_s without a request
    if (!e) return;
    setBusy(true);
    try {
      await api().updateEndpoint(id, { scaling: { min, max: 1 } }, e.region);
      toast(min ? "Starting: the nodes boot and the model loads, usually 10-20 minutes" : "Stopping");
      reload();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  async function deleteEndpoint() {
    if (!e) return;
    setBusy(true);
    try {
      await api().deleteModel(e.model);
      toast("Endpoint deleted");
      navigate("/endpoints");
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  if (error) return <ErrorBox msg={error} retry={reload} />;
  if (loading && !e) return <Loading />;
  if (!e) return null;
  const editable = admin && e.managed_by === "api";
  return (
    <>
      <ResourceHeader
        name={e.name}
        id={e.id}
        status={<Badge status={e.status} />}
        created={e.created_at}
        region={e.region}
        actions={
          <>
            {(e.nodes ?? 1) > 1 && editable && (
              <Button
                view="action"
                size="l"
                disabled={busy}
                onClick={() => startStop(e.min_replicas ? 0 : 1)}
              >
                {e.min_replicas ? "Stop" : "Start"}
              </Button>
            )}
            <Button
              view="normal"
              size="l"
              onClick={() => setTab("Test request")}
            >
              <Icon data={Play} /> Test request
            </Button>
            <Button view="normal" size="l" onClick={() => setTab("Metrics")}>
              <Icon data={ChartLine} /> Monitoring
            </Button>
            <Button
              view="outlined"
              size="l"
              onClick={() =>
                navigator.clipboard.writeText(e.url).then(
                  () => toast("Endpoint URL copied"),
                  () => toast("Could not copy", true),
                )
              }
            >
              Copy endpoint URL
            </Button>
            {editable && (
              <Button
                view="outlined"
                size="l"
                href={href(`/models/${e.model}`)}
              >
                Edit configuration
              </Button>
            )}
          </>
        }
      />
      <Tabs items={TABS} active={tab} onChange={setTab} />
      {tab === "Overview" && (
        <>
          <div style={{ marginBottom: 32 }}>
            <ResourceMonitoring
              key={id + e.region}
              kind="endpoints"
              id={id}
              region={e.region}
              compact
            />
          </div>
          <div className="overview" style={{ marginBottom: 32 }}>
            <DetailCard title="Container settings">
              <dl className="kv">
                <dt>Image path</dt>
                <dd>
                  <code>{e.image ?? e.model}</code>
                  {e.image && (
                    <CopyButton value={e.image} label="Copy image path" />
                  )}
                </dd>
                <dt>Protocol</dt>
                <dd>{e.protocol ?? "HTTP"}</dd>
                <dt>Endpoint URL</dt>
                <dd>
                  {e.url}
                  <CopyButton value={e.url} label="Copy endpoint URL" />
                </dd>
              </dl>
            </DetailCard>
            <DetailCard title="Resources">
              <dl className="kv">
                <dt>GPU</dt>
                <dd>{e.gpu ?? "—"}</dd>
                <dt>CPU / memory</dt>
                <dd>
                  {e.cpu ? `${e.cpu} CPUs / ${e.memory}` : "Not reported"}
                </dd>
                <dt>Region</dt>
                <dd>{e.region}</dd>
                <dt>Ready replicas</dt>
                <dd>
                  {e.replicas_ready ?? "Unknown"}
                  {(e.nodes ?? 1) > 1 &&
                    ` (${e.pods_ready ?? 0} of ${e.nodes} node pods ready)`}
                </dd>
                {(e.nodes ?? 1) > 1 && (
                  <>
                    <dt>Nodes per replica</dt>
                    <dd>
                      {e.nodes} whole nodes, interconnect {e.interconnect}
                    </dd>
                  </>
                )}
                <dt>Last start-up</dt>
                <dd title={e.started_at ? `started ${fmt.dt(e.started_at)}, ready ${fmt.dt(e.ready_at)}` : undefined}>
                  {e.startup_s != null
                    ? `${fmt.secs(e.startup_s)} (pod created to Ready: node boot, driver, image, weights)`
                    : "No pod has become ready yet"}
                </dd>
                {e.last_request_at && (
                  <>
                    <dt>Last request</dt>
                    <dd>{fmt.ago(e.last_request_at)}</dd>
                  </>
                )}
              </dl>
            </DetailCard>
            <DetailCard title="Scaling">
              <dl className="kv">
                <dt>Replica range</dt>
                <dd>
                  {e.min_replicas}–{e.max_replicas}
                </dd>
                <dt>Scaling metric</dt>
                <dd>{scalingMetricLabel(endpointScaling(e))}</dd>
                <dt>Average demand target</dt>
                <dd>
                  {Number(scalingThreshold(endpointScaling(e)).toFixed(2))}{" "}
                  {isRequestRate(endpointScaling(e))
                    ? "requests/s"
                    : "concurrent requests"}
                </dd>
                <dt>Idle retention</dt>
                <dd>{fmt.secs(e.scale_to_zero_after_s)}</dd>
                <dt>Idle behavior</dt>
                <dd>
                  {e.min_replicas === 0
                    ? "Scale to zero"
                    : "Keep replicas warm"}
                </dd>
              </dl>
              {editable && (
                <Button
                  view="outlined"
                  size="l"
                  style={{ marginTop: 20 }}
                  onClick={() => setTab("Scaling")}
                >
                  Edit scaling
                </Button>
              )}
            </DetailCard>
            <DetailCard title="Access">
              <dl className="kv">
                <dt>Authentication</dt>
                <dd>API key</dd>
                <dt>Definition</dt>
                <dd>{e.model}</dd>
                <dt>Managed by</dt>
                <dd>
                  {e.managed_by === "api"
                    ? "Saved definition"
                    : "Fleet configuration"}
                </dd>
              </dl>
              <p className="help" style={{ marginTop: 20 }}>
                Use Test request to send an authenticated call. Idle endpoints
                wake on the next request.
              </p>
            </DetailCard>
          </div>
        </>
      )}
      {tab === "Metrics" && (
        <ResourceMonitoring kind="endpoints" id={id} region={e.region} />
      )}
      {tab === "Logs" && (
        <ResourceMonitoring
          kind="endpoints"
          id={id}
          region={e.region}
          view="logs"
        />
      )}
      {tab === "Test request" && <EndpointTest endpoint={e} />}
      {tab === "Settings" && (
        <>
          <DetailCard title="Configuration">
            <p>
              {e.managed_by === "api"
                ? "Configuration changes apply to every region in this endpoint's saved definition."
                : "This endpoint is managed by the fleet configuration. Update its source definition to change it."}
            </p>
            {editable && (
              <div className="actions">
                <Button
                  view="outlined"
                  size="l"
                  onClick={() => setTab("Scaling")}
                >
                  Edit scaling
                </Button>
                <Button
                  view="outlined"
                  size="l"
                  href={href(`/models/${e.model}`)}
                >
                  Edit configuration
                </Button>
              </div>
            )}
          </DetailCard>
          {editable && (
            <section className="danger-zone">
              <h2>Delete endpoint</h2>
              <p>
                Remove this endpoint and its saved definition from every
                deployment region. New requests will no longer be served.
              </p>
              <Button
                view="outlined-danger"
                size="l"
                onClick={() => setRemove(true)}
              >
                Delete endpoint
              </Button>
            </section>
          )}
        </>
      )}
      {tab === "Scaling" && (
        <div className="form-layout scaling-layout">
          <form className="form" onSubmit={saveScale}>
            <div>
              <h2>Scaling</h2>
              <p className="help">
                Pod replica settings apply to every deployment region and are
                saved with the endpoint definition.
              </p>
            </div>
            {!editable && (
              <p className="inline-note">
                {admin
                  ? "Managed by fleet configuration. Update the source definition to change these settings."
                  : "An administrator API key is required to change these settings."}
              </p>
            )}
            {scaleError && <ErrorBox msg={scaleError} />}
            <ScalingFields
              value={scale}
              timeout={timeout}
              advancedOpen
              disabled={!editable || busy}
              onChange={(value) => {
                setScale(value);
                setEdit(true);
              }}
              onTimeoutChange={(value) => {
                setTimeoutValue(value);
                setEdit(true);
              }}
            />
            {editable && (
              <div className="form-actions">
                <Button
                  view="action"
                  size="l"
                  type="submit"
                  loading={busy}
                  disabled={!edit || !!scalingValidation(scale)}
                >
                  Save changes
                </Button>
                <Button
                  view="flat"
                  size="l"
                  disabled={!edit || busy}
                  onClick={() => {
                    setScale(endpointScaling(e));
                    setTimeoutValue(e.timeout_s ?? 600);
                    setEdit(false);
                    setScaleError("");
                  }}
                >
                  Discard changes
                </Button>
              </div>
            )}
          </form>
          <aside className="card summary">
            <div className="card__head">
              <h2>Scaling behavior</h2>
            </div>
            <div className="card__body">
              <dl className="kv">
                <dt>Replica range per region</dt>
                <dd>
                  {scale.min ?? 0}–{scale.max ?? 1} pods
                </dd>
                <dt>Ready replicas in {e.region}</dt>
                <dd>{e.replicas_ready ?? "Unknown"}</dd>
                <dt>Average demand target</dt>
                <dd>
                  {Number(scalingThreshold(scale).toFixed(2))}{" "}
                  {isRequestRate(scale) ? "requests/s" : "concurrent requests"}{" "}
                  per pod
                </dd>
                <dt>Hard concurrency limit</dt>
                <dd>
                  {scale.container_concurrency
                    ? `${scale.container_concurrency} per pod`
                    : "Unlimited"}
                </dd>
                <dt>Idle behavior</dt>
                <dd>
                  {(scale.min ?? 0) === 0
                    ? "Scale to zero"
                    : "Keep minimum replicas warm"}
                </dd>
              </dl>
              <p className="help">
                Utilization headroom leaves room within each replica. A shared
                warm GPU buffer reserves capacity across apps and needs a
                separate pool policy.
              </p>
              <p className="help">
                Disabled controls remain visible for future deployment support.
                CPU and RAM utilization require resource-metric autoscaler
                integration and a minimum of one replica.
              </p>
            </div>
          </aside>
        </div>
      )}
      {remove && (
        <Modal
          title="Delete endpoint?"
          onClose={() => {
            if (!busy) setRemove(false);
          }}
        >
          <p>
            Delete <strong>{e.name}</strong> from every region? This removes its
            saved definition.
          </p>
          <div className="actions">
            <Button disabled={busy} onClick={() => setRemove(false)}>
              Keep endpoint
            </Button>
            <Button
              view="outlined-danger"
              loading={busy}
              onClick={deleteEndpoint}
            >
              Delete endpoint
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}

function EndpointTest({ endpoint: e }: { endpoint: Endpoint }) {
  const [body, setBody] = useState(JSON.stringify(sampleBody(e), null, 2));
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [duration, setDuration] = useState(0);
  async function send(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const input = JSON.parse(body);
      if (!input || Array.isArray(input) || typeof input !== "object")
        throw new Error("Enter a JSON object.");
      const start = performance.now();
      const response = await api().invoke(e.model, {
        mode: "sync",
        input,
        region: e.region,
        timeout_s: 600,
      });
      setDuration(Math.round(performance.now() - start));
      setResult("result" in response ? response.result : response);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="overview">
      <DetailCard title="Test request">
        <form className="form" onSubmit={send}>
          <Field label="Endpoint URL">
            <input className="input" value={e.url} readOnly />
          </Field>
          <Field label="Request body (JSON)">
            <textarea
              className="textarea mono"
              rows={10}
              value={body}
              onChange={(ev) => setBody(ev.target.value)}
            />
          </Field>
          <div className="inline-note">
            This request uses your API key and counts toward its budget. An idle
            endpoint may take longer to respond while starting.
          </div>
          <Button view="action" size="l" type="submit" loading={busy}>
            Send request
          </Button>
          <CopyButton value={curlFor(e, body)} label="Copy cURL" />
          {error && <ErrorBox msg={error} />}
        </form>
      </DetailCard>
      <DetailCard title="Response">
        {result == null ? (
          <p className="muted">Send a request to see its response here.</p>
        ) : (
          <>
            <p className="muted">Completed in {duration} ms</p>
            <pre className="out">{JSON.stringify(result, null, 2)}</pre>
          </>
        )}
      </DetailCard>
    </div>
  );
}
