import { useState } from "react";
import { api } from "../api/client";
import { grafanaUrl } from "../config";
import type { Operation, OperationResult } from "../api/types";
import { Badge, Empty, ErrorBox, Loading, Modal, Stat, fmt, useAsync, useToast } from "../components/ui";
import { href, navigate } from "../router";

const ACTIVE = new Set(["QUEUED", "ADMITTED", "RUNNING", "PREEMPTED"]);

export function Jobs() {
  const [filter, setFilter] = useState<"all" | "active" | "done">("all");
  const { data, error, loading, reload } = useAsync(() => api().listOperations(), [], { every: 5000 });
  const rows = (data ?? []).filter((o) => filter === "all" || (filter === "active" ? ACTIVE.has(o.status) : !ACTIVE.has(o.status)));
  return (
    <>
      <div className="page-head">
        <div><h1>Jobs</h1><p>Run-class and async operations: queued in Kueue by priority, checkpointed, resumed after spot preemption. Billed per second while running.</p></div>
        <div className="actions"><button className="btn" onClick={reload}>Refresh</button><a className="btn btn--primary" href={href("/jobs/new")}>New job</a></div>
      </div>
      <div className="tabs">
        {(["all", "active", "done"] as const).map((f) => <button key={f} className={filter === f ? "active" : ""} onClick={() => setFilter(f)}>{f[0].toUpperCase() + f.slice(1)}{data && ` (${data.filter((o) => f === "all" || (f === "active" ? ACTIVE.has(o.status) : !ACTIVE.has(o.status))).length})`}</button>)}
      </div>
      {error && <ErrorBox msg={error} retry={reload} />}
      {loading && !data && <Loading />}
      {data && (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead><tr><th>Name</th><th>Model</th><th>Mode</th><th>Status</th><th>Priority</th><th>Region</th><th>Started</th><th>Duration</th><th>Attempts</th><th className="num">Cost</th></tr></thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={10}><Empty>No jobs.</Empty></td></tr>}
              {rows.map((o) => (
                <tr key={o.id} className="row--link" onClick={() => navigate(`/jobs/${o.id}`)}>
                  <td><a className="strong" href={href(`/jobs/${o.id}`)}>{o.name ?? o.id}</a><div className="small muted mono">{o.id}</div></td>
                  <td>{o.model}</td>
                  <td><span className="chip">{o.mode}</span></td>
                  <td><Badge status={o.status} />{o.status === "QUEUED" && o.queue_position != null && <span className="small muted"> #{o.queue_position}</span>}</td>
                  <td>{o.priority ?? "normal"}</td>
                  <td>{o.region}</td>
                  <td title={fmt.dt(o.started_at ?? o.created_at)}>{o.started_at ? fmt.ago(o.started_at) : <span className="muted">queued {fmt.ago(o.created_at)}</span>}</td>
                  <td>{fmt.dur(o.started_at, o.ended_at)}</td>
                  <td>{o.attempts?.length ?? 0}</td>
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
  const { data: op, error, loading, reload } = useAsync(() => api().getOperation(id), [id], { every: 4000 });
  const [result, setResult] = useState<OperationResult | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);

  async function cancel() {
    setBusy(true);
    try { await api().cancel(id); toast("Cancel requested"); setConfirm(false); reload(); } catch (e) { toast((e as Error).message, true); } finally { setBusy(false); }
  }
  async function loadResult() {
    try { setResult(await api().getResult(id)); } catch (e) { toast((e as Error).message, true); }
  }
  async function resubmit(o: Operation) {
    try {
      const r = await api().invoke(o.model, { name: (o.name ?? o.id) + "-resubmit", mode: o.mode, input: o.input, region: o.region, priority: o.priority, timeout_s: o.timeout_s });
      const n = "operation" in r ? r.operation : r; toast(`Submitted ${n.id}`); navigate(`/jobs/${n.id}`);
    } catch (e) { toast((e as Error).message, true); }
  }

  if (error) return <ErrorBox msg={error} retry={reload} />;
  if (loading && !op) return <Loading />;
  if (!op) return null;
  const active = ACTIVE.has(op.status);
  return (
    <>
      <div className="page-head">
        <div>
          <div className="row"><h1>{op.name ?? op.id}</h1><Badge status={op.status} /></div>
          <p className="mono small">{op.id} · {op.model} · {op.mode} · {op.region}{op.gpu_class ? ` · ${op.gpu_class}` : ""}</p>
        </div>
        <div className="actions">
          {op.logs_url && <a className="btn" href={op.logs_url} target="_blank" rel="noreferrer">Logs</a>}
          {grafanaUrl(op.region) && <a className="btn" href={`${grafanaUrl(op.region)}/explore?left=${encodeURIComponent(JSON.stringify({ datasource: "Loki", queries: [{ expr: `{namespace=~".+"} |= "${op.id}"` }] }))}`} target="_blank" rel="noreferrer">Metrics</a>}
          {!active && <button className="btn" onClick={() => resubmit(op)}>Resubmit</button>}
          {active && <button className="btn btn--danger" onClick={() => setConfirm(true)}>Cancel</button>}
        </div>
      </div>
      {op.error && <div className="banner banner--danger" style={{ marginBottom: 16 }}>{op.error}</div>}
      <div className="stats" style={{ marginBottom: 16 }}>
        <Stat label="Created" value={<span title={fmt.dt(op.created_at)}>{fmt.ago(op.created_at)}</span>} />
        <Stat label="Started" value={op.started_at ? fmt.ago(op.started_at) : op.queue_position != null ? `queue #${op.queue_position}` : "-"} />
        <Stat label="Duration" value={fmt.dur(op.started_at, op.ended_at)} />
        <Stat label="Priority" value={op.priority ?? "normal"} />
        <Stat label="Timeout" value={fmt.secs(op.timeout_s)} />
        <Stat label="Attempts" value={op.attempts?.length ?? 0} />
        <Stat label="Cost so far" value={fmt.usd(op.cost)} />
      </div>
      <div className="grid grid--2">
        <div className="card">
          <div className="card__head"><h2>Attempts</h2><span className="small muted">retried on node loss; the run class resumes from its last checkpoint</span></div>
          <div className="card__body">
            {!op.attempts?.length && <span className="muted">Not started yet.</span>}
            <ul className="timeline">
              {op.attempts?.map((a) => (
                <li key={a.index}>
                  <span className={`dot ${a.status === "SUCCEEDED" ? "dot--ok" : a.status === "RUNNING" ? "dot--run" : a.status === "PREEMPTED" ? "dot--warn" : a.status === "FAILED" ? "dot--bad" : ""}`} />
                  <div>
                    <strong>Attempt {a.index}</strong> <Badge status={a.status} />
                    <small>{fmt.dt(a.started_at)} → {a.ended_at ? fmt.dt(a.ended_at) : "running"} ({fmt.dur(a.started_at, a.ended_at)}){a.node && <> · node <span className="mono">{a.node}</span></>}</small>
                    {a.reason && <small>{a.reason}</small>}
                    {a.resumed_from_checkpoint && <small>resumed from <span className="mono">{a.resumed_from_checkpoint}</span></small>}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
        <div className="card">
          <div className="card__head"><h2>Input</h2></div>
          <div className="card__body"><pre className="out">{JSON.stringify(op.input ?? {}, null, 2)}</pre></div>
        </div>
        <div className="card">
          <div className="card__head"><h2>Result</h2>{!result && <button className="btn btn--sm" onClick={loadResult}>Fetch result</button>}</div>
          <div className="card__body">
            {!result && <span className="muted">{op.status === "SUCCEEDED" ? "Fetch presigned artifact links." : "Available once the operation succeeds."}</span>}
            {result?.artifacts && (
              <table className="tbl"><thead><tr><th>Artifact</th><th className="num">Size</th><th></th></tr></thead>
                <tbody>{result.artifacts.map((a) => <tr key={a.name}><td className="mono">{a.name}</td><td className="num">{fmt.bytes(a.size_bytes)}</td><td><a className="btn btn--sm" href={a.url} target="_blank" rel="noreferrer">Download</a></td></tr>)}</tbody></table>
            )}
            {result && result.result != null && <pre className="out" style={{ marginTop: 10 }}>{JSON.stringify(result.result, null, 2)}</pre>}
            {result && !result.artifacts && result.result == null && <span className="muted">Status {result.status}, no result yet.</span>}
          </div>
        </div>
      </div>
      {confirm && (
        <Modal title="Cancel job" onClose={() => setConfirm(false)}>
          <p>Cancel <strong>{op.name ?? op.id}</strong>? Running attempts are stopped; checkpoints are kept in the tenant bucket.</p>
          <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn" onClick={() => setConfirm(false)}>Keep running</button><button className="btn btn--danger" disabled={busy} onClick={cancel}>Cancel job</button></div>
        </Modal>
      )}
    </>
  );
}
