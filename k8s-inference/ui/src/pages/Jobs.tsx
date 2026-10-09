import { useState } from "react";
import { api } from "../api/client";
import { grafanaUrl } from "../config";
import type { Operation, OperationResult } from "../api/types";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  Stat,
  fmt,
  useAsync,
  useToast,
} from "../components/ui";
import { Button, PageHeader, Search, Tabs } from "../components/controls";
import { href, navigate } from "../router";

const ACTIVE = new Set(["QUEUED", "ADMITTED", "RUNNING", "PREEMPTED"]);

export function Jobs() {
  const [filter, setFilter] = useState<"all" | "active" | "done">("all");
  const [search, setSearch] = useState("");
  const { data, error, loading, reload } = useAsync(
    () => api().listOperations(),
    [],
    { every: 5000 },
  );
  const rows = (data ?? []).filter(
    (o) =>
      (filter === "all" ||
        (filter === "active" ? ACTIVE.has(o.status) : !ACTIVE.has(o.status))) &&
      `${o.name ?? ""} ${o.id} ${o.model}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  return (
    <>
      <PageHeader
        title="Jobs"
        count={data?.length}
        description="Track background runs, attempts and results."
      >
        <Button primary href={href("/jobs/new")}>
          Create job
        </Button>
      </PageHeader>
      <Tabs
        className="resource-tabs"
        activeTab={filter}
        onSelectTab={(v) => setFilter(v as typeof filter)}
        aria-label="Job status"
        items={[
          { id: "all", title: "All jobs" },
          { id: "active", title: "Active" },
          { id: "done", title: "Completed" },
        ]}
      />
      <div className="toolbar">
        <div className="toolbar__search">
          <Search value={search} onChange={setSearch} label="Search jobs" />
        </div>
        <span className="toolbar__count">
          {rows.length} of the latest 100 jobs
        </span>
        <Button size="m" onClick={reload}>
          Refresh
        </Button>
      </div>
      {error && <ErrorBox msg={error} retry={reload} />}
      {loading && !data && <Loading />}
      {data && (
        <div className="table-surface tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                <th>Model</th>
                <th>Status</th>
                <th>Region</th>
                <th>Started</th>
                <th>Duration</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr>
                  <td colSpan={7}>
                    <Empty>
                      <h2>
                        {search || filter !== "all"
                          ? "No matching jobs"
                          : "Run your first job"}
                      </h2>
                      <p>
                        {search || filter !== "all"
                          ? "Try a different search or filter."
                          : "Run a model or container and track its progress here."}
                      </p>
                      {!search && filter === "all" && (
                        <Button primary href={href("/jobs/new")}>
                          Create job
                        </Button>
                      )}
                    </Empty>
                  </td>
                </tr>
              )}
              {rows.map((o) => (
                <tr key={o.id}>
                  <td>
                    <a className="strong" href={href(`/jobs/${o.id}`)}>
                      {o.name ?? o.id}
                    </a>
                    <div className="small muted mono">{o.id}</div>
                  </td>
                  <td>{o.model}</td>
                  <td>
                    <Badge status={o.status} />
                    {o.status === "QUEUED" && o.queue_position != null && (
                      <span className="small muted"> #{o.queue_position}</span>
                    )}
                  </td>
                  <td>{o.region}</td>
                  <td title={fmt.dt(o.started_at ?? o.created_at)}>
                    {o.started_at ? (
                      fmt.ago(o.started_at)
                    ) : (
                      <span className="muted">
                        queued {fmt.ago(o.created_at)}
                      </span>
                    )}
                  </td>
                  <td>{fmt.dur(o.started_at, o.ended_at)}</td>
                  <td className="num">{fmt.usd(o.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function JobDetail({ id }: { id: string }) {
  const toast = useToast();
  const {
    data: op,
    error,
    loading,
    reload,
  } = useAsync(() => api().getOperation(id), [id], { every: 5000 });
  const [tab, setTab] = useState("overview");
  const [result, setResult] = useState<OperationResult | null>(null);
  const [resultError, setResultError] = useState("");
  const [resultBusy, setResultBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  async function cancel() {
    setBusy(true);
    try {
      await api().cancel(id);
      toast("Cancellation requested");
      setConfirm(false);
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  async function loadResult() {
    setResultBusy(true);
    setResultError("");
    try {
      setResult(await api().getResult(id));
    } catch (e) {
      setResultError((e as Error).message);
    } finally {
      setResultBusy(false);
    }
  }
  async function resubmit(o: Operation) {
    setBusy(true);
    try {
      const r = await api().invoke(o.model, {
        name: (o.name ?? o.id) + "-resubmit",
        mode: o.mode,
        input: o.input,
        region: o.region,
        priority: o.priority,
        timeout_s: o.timeout_s,
      });
      const n = "operation" in r ? r.operation : r;
      toast("Job resubmitted");
      navigate(`/jobs/${n.id}`);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  if (error && !op) return <ErrorBox msg={error} retry={reload} />;
  if (loading && !op) return <Loading />;
  if (!op) return null;
  const active = ACTIVE.has(op.status);
  return (
    <>
      <PageHeader
        title={
          <>
            {op.name ?? op.id}
            <Badge status={op.status} />
          </>
        }
        description={`${op.model} · ${op.region}${op.gpu_class ? " · " + op.gpu_class : ""}`}
      >
        {op.logs_url ? (
          <Button href={op.logs_url} target="_blank" rel="noreferrer">
            Logs ↗
          </Button>
        ) : (
          grafanaUrl(op.region) && (
            <Button
              href={`${grafanaUrl(op.region)}/explore?left=${encodeURIComponent(JSON.stringify({ datasource: "Loki", queries: [{ expr: `{namespace=~".+"} |= "${op.id}"` }] }))}`}
              target="_blank"
              rel="noreferrer"
            >
              Logs ↗
            </Button>
          )
        )}
        {active ? (
          <Button danger disabled={busy} onClick={() => setConfirm(true)}>
            Cancel job
          </Button>
        ) : (
          <Button loading={busy} onClick={() => resubmit(op)}>
            Resubmit
          </Button>
        )}
      </PageHeader>
      {error && <ErrorBox msg={error} retry={reload} />}
      {op.error && <ErrorBox msg={op.error} />}
      <Tabs
        className="resource-tabs"
        activeTab={tab}
        onSelectTab={setTab}
        aria-label="Job details"
        items={[
          { id: "overview", title: "Overview" },
          { id: "input", title: "Input" },
          { id: "result", title: "Results" },
        ]}
      />
      {tab === "overview" && (
        <div className="detail-stack">
          <div className="stats">
            <Stat
              label="Duration"
              value={fmt.dur(op.started_at, op.ended_at)}
            />
            <Stat label="Attempts" value={op.attempts?.length ?? 0} />
            <Stat label="Priority" value={op.priority ?? "normal"} />
            <Stat label="Cost" value={fmt.usd(op.cost)} />
          </div>
          <div className="grid grid--2">
            <section className="card">
              <div className="card__head">
                <h2>Job information</h2>
              </div>
              <div className="card__body">
                <dl className="details-kv">
                  <dt>Job ID</dt>
                  <dd className="mono">{op.id}</dd>
                  <dt>Model</dt>
                  <dd>{op.model}</dd>
                  <dt>Mode</dt>
                  <dd>
                    {op.mode === "run" ? "Container job" : "Async inference"}
                  </dd>
                  <dt>Region</dt>
                  <dd>{op.region}</dd>
                  <dt>Created</dt>
                  <dd>{fmt.dt(op.created_at)}</dd>
                  <dt>Started</dt>
                  <dd>
                    {op.started_at
                      ? fmt.dt(op.started_at)
                      : op.queue_position != null
                        ? `Queue position ${op.queue_position}`
                        : "Waiting"}
                  </dd>
                  <dt>Timeout</dt>
                  <dd>{fmt.secs(op.timeout_s)}</dd>
                  {op.image && (
                    <>
                      <dt>Image</dt>
                      <dd className="mono">{op.image}</dd>
                    </>
                  )}
                </dl>
              </div>
            </section>
            <section className="card">
              <div className="card__head">
                <h2>Attempts</h2>
              </div>
              <div className="card__body">
                {!op.attempts?.length && (
                  <p className="muted">This job has not started yet.</p>
                )}
                <ul className="timeline">
                  {op.attempts?.map((a) => (
                    <li key={a.index}>
                      <span
                        className={`dot ${a.status === "SUCCEEDED" ? "dot--ok" : a.status === "RUNNING" ? "dot--run" : a.status === "PREEMPTED" ? "dot--warn" : a.status === "FAILED" ? "dot--bad" : ""}`}
                      />
                      <div>
                        <strong>Attempt {a.index}</strong>{" "}
                        <Badge status={a.status} />
                        <small>
                          {fmt.dt(a.started_at)} →{" "}
                          {a.ended_at ? fmt.dt(a.ended_at) : "Running"} ·{" "}
                          {fmt.dur(a.started_at, a.ended_at)}
                        </small>
                        {a.node && (
                          <small>
                            Node: <span className="mono">{a.node}</span>
                          </small>
                        )}
                        {a.reason && <small>{a.reason}</small>}
                        {a.resumed_from_checkpoint && (
                          <small>
                            Resumed from{" "}
                            <span className="mono">
                              {a.resumed_from_checkpoint}
                            </span>
                          </small>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            </section>
          </div>
        </div>
      )}
      {tab === "input" && (
        <section className="card">
          <div className="card__head">
            <h2>Input parameters</h2>
          </div>
          <div className="card__body">
            <pre className="out">{JSON.stringify(op.input ?? {}, null, 2)}</pre>
          </div>
        </section>
      )}
      {tab === "result" && (
        <section className="card">
          <div className="card__head">
            <h2>Results and artifacts</h2>
            <Button size="m" loading={resultBusy} onClick={loadResult}>
              {result ? "Refresh" : "Fetch results"}
            </Button>
          </div>
          <div className="card__body form">
            {resultError && <ErrorBox msg={resultError} retry={loadResult} />}
            {!result && (
              <p className="muted">
                {op.status === "SUCCEEDED"
                  ? "Fetch results to view output and download artifacts."
                  : "Results are available when the job completes."}
              </p>
            )}
            {!!result?.artifacts?.length && (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Artifact</th>
                      <th className="num">Size</th>
                      <th>
                        <span className="sr-only">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.artifacts.map((a) => (
                      <tr key={a.name}>
                        <td className="mono">{a.name}</td>
                        <td className="num">{fmt.bytes(a.size_bytes)}</td>
                        <td>
                          <Button
                            size="m"
                            href={a.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            Download
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {result?.result != null && (
              <pre className="out">
                {JSON.stringify(result.result, null, 2)}
              </pre>
            )}
            {result && !result.artifacts?.length && result.result == null && (
              <p className="muted">
                Status: {result.status.toLowerCase()}. No result artifacts are
                available.
              </p>
            )}
          </div>
        </section>
      )}
      {confirm && (
        <Modal title="Cancel job" onClose={() => !busy && setConfirm(false)}>
          <p>
            Cancel <strong>{op.name ?? op.id}</strong>? Running attempts will
            stop. Checkpoints are kept.
          </p>
          <div className="dialog-actions">
            <Button disabled={busy} onClick={() => setConfirm(false)}>
              Keep running
            </Button>
            <Button danger loading={busy} onClick={cancel}>
              Cancel job
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}
