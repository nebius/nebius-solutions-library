import { useState } from "react";
import {
  ArrowRotateRight,
  ArrowsRotateRight,
  Play,
  Plus,
} from "@gravity-ui/icons";
import { api } from "../api/client";
import type { Operation } from "../api/types";
import {
  Badge,
  Button,
  Empty,
  ErrorBox,
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
import { href, navigate } from "../router";

const ACTIVE = new Set(["QUEUED", "ADMITTED", "RUNNING", "PREEMPTED"]);
const TABS = ["Overview", "Metrics", "Logs", "Attempts", "Results", "Settings"];
export function Jobs() {
  const jobs = useAsync(() => api().listOperations(), [], { every: 8000 });
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [region, setRegion] = useState("");
  const rows = (jobs.data ?? []).filter(
    (o) =>
      (!search ||
        `${o.name ?? ""} ${o.id} ${o.model}`
          .toLowerCase()
          .includes(search.toLowerCase())) &&
      (!region || o.region === region) &&
      (!status ||
        (status === "active"
          ? ACTIVE.has(o.status)
          : status === "inactive"
            ? !ACTIVE.has(o.status)
            : o.status === status)),
  );
  const reset = () => {
    setSearch("");
    setStatus("");
    setRegion("");
  };
  return (
    <>
      <PageHeader
        title="Jobs"
        count={jobs.data?.length}
        create={{ label: "Create job", to: "/jobs/new" }}
      />
      <SearchToolbar
        {...{ search, setSearch, status, setStatus, region, setRegion }}
        statuses={[
          ["active", "Active"],
          ["inactive", "Inactive"],
          ["RUNNING", "Running"],
          ["QUEUED", "Queued"],
          ["SUCCEEDED", "Completed"],
          ["FAILED", "Failed"],
          ["CANCELLED", "Cancelled"],
        ]}
        regions={[
          ...new Set(jobs.data?.map((j) => j.region).filter(Boolean) ?? []),
        ]}
      >
        <Button
          view="flat"
          size="l"
          aria-label="Refresh jobs"
          onClick={jobs.reload}
        >
          <Icon data={ArrowsRotateRight} />
        </Button>
      </SearchToolbar>
      {jobs.error && <ErrorBox msg={jobs.error} retry={jobs.reload} />}{" "}
      {jobs.loading && !jobs.data && <Loading />}
      {jobs.data &&
        (rows.length ? (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Name and ID</th>
                  <th>Status</th>
                  <th>Definition</th>
                  <th>Region</th>
                  <th>Created</th>
                  <th>Duration</th>
                  <th></th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((o) => (
                  <tr key={o.id}>
                    <td className="name-cell">
                      <ResourceName
                        name={o.name ?? o.id}
                        id={o.id}
                        to={resourcePath("jobs", o.id)}
                      />
                    </td>
                    <td>
                      <Badge status={o.status} />
                      {o.queue_position != null && o.status === "QUEUED" && (
                        <div className="small muted">
                          Queue position {o.queue_position}
                        </div>
                      )}
                    </td>
                    <td>
                      {o.model}
                      <div className="small muted">
                        {o.priority ?? "normal"} priority
                        {o.gpu_class ? ` · ${o.gpu_class}` : ""}
                        {o.nodes && o.nodes > 1 ? ` · ${o.nodes} nodes` : ""}
                      </div>
                    </td>
                    <td>{o.region ?? "Awaiting placement"}</td>
                    <td title={fmt.dt(o.created_at)}>
                      {fmt.ago(o.created_at)}
                    </td>
                    <td>{fmt.dur(o.started_at, o.ended_at)}</td>
                    <td>
                      <Button
                        view="outlined"
                        size="l"
                        href={href(
                          resourcePath("jobs", o.id, undefined, "Logs"),
                        )}
                      >
                        View logs
                      </Button>
                    </td>
                    <td>
                      <RowMenu
                        actions={[
                          {
                            label: "View job",
                            action: () => navigate(resourcePath("jobs", o.id)),
                          },
                          {
                            label: "View metrics",
                            action: () =>
                              navigate(
                                resourcePath(
                                  "jobs",
                                  o.id,
                                  undefined,
                                  "Metrics",
                                ),
                              ),
                          },
                          {
                            label: "View attempts",
                            action: () =>
                              navigate(
                                resourcePath(
                                  "jobs",
                                  o.id,
                                  undefined,
                                  "Attempts",
                                ),
                              ),
                          },
                        ]}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyList
            resource="jobs"
            filtered={Boolean(search || status || region)}
            reset={reset}
            action={
              <Button view="action" size="l" href={href("/jobs/new")}>
                <Icon data={Plus} /> Create job
              </Button>
            }
          />
        ))}
    </>
  );
}

export function JobDetail({ id }: { id: string }) {
  const toast = useToast();
  const { tab, setTab } = useResourceTab(TABS);
  const job = useAsync(() => api().getOperation(id), [id], { every: 6000 });
  const [confirm, setConfirm] = useState<"cancel" | "resume" | "again" | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  async function action() {
    if (!job.data || !confirm) return;
    setBusy(true);
    try {
      if (confirm === "cancel") {
        await api().cancel(id);
        toast("Cancel requested");
        job.reload();
      } else if (confirm === "resume") {
        const op = await api().resume(id);
        toast("Resume submitted");
        navigate(`/jobs/${op.id}`);
      } else {
        const o = job.data;
        const result = await api().invoke(o.model, {
          name: (o.name ?? o.id) + "-again",
          mode: o.mode,
          input: o.input,
          region: o.region ?? undefined,
          priority: o.priority,
          timeout_s: o.timeout_s,
        });
        const op = "operation" in result ? result.operation : result;
        toast("Job submitted");
        navigate(`/jobs/${op.id}`);
      }
      setConfirm(null);
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  if (job.error) return <ErrorBox msg={job.error} retry={job.reload} />;
  if (job.loading && !job.data) return <Loading />;
  if (!job.data) return null;
  const o = job.data;
  const active = ACTIVE.has(o.status);
  const resumable =
    o.mode === "run" && ["FAILED", "CANCELLED"].includes(o.status);
  const end = o.ended_at ? new Date(o.ended_at).getTime() / 1000 : undefined;
  return (
    <>
      <ResourceHeader
        name={o.name ?? o.id}
        id={id}
        status={<Badge status={o.status} />}
        created={o.created_at}
        region={o.region}
        actions={
          <>
            {active ? (
              <Button
                view="outlined-danger"
                size="l"
                onClick={() => setConfirm("cancel")}
              >
                Cancel job
              </Button>
            ) : (
              <Button
                view="normal"
                size="l"
                onClick={() => setConfirm("again")}
              >
                <Icon data={Play} /> Run again
              </Button>
            )}
            {resumable && (
              <Button
                view="normal"
                size="l"
                onClick={() => setConfirm("resume")}
              >
                <Icon data={ArrowRotateRight} /> Resume checkpoint
              </Button>
            )}
            <Button view="outlined" size="l" onClick={() => setTab("Results")}>
              View results
            </Button>
          </>
        }
      />
      <Tabs items={TABS} active={tab} onChange={setTab} />
      {o.error && (
        <div className="banner banner--danger" style={{ marginBottom: 24 }}>
          {o.error}
        </div>
      )}
      {tab === "Overview" && (
        <>
          <div style={{ marginBottom: 32 }}>
            <ResourceMonitoring
              key={id}
              kind="operations"
              id={id}
              region={o.region}
              end={end}
              compact
            />
          </div>
          <div className="overview" style={{ marginBottom: 32 }}>
            <DetailCard title="Container settings">
              <dl className="kv">
                <dt>Definition</dt>
                <dd>{o.model}</dd>
                <dt>Image path</dt>
                <dd>
                  <code>
                    {o.image ??
                      String(
                        o.input?.image ?? "Defined by saved configuration",
                      )}
                  </code>
                </dd>
                <dt>Execution mode</dt>
                <dd>
                  {o.mode === "run" ? "Container job" : "Asynchronous request"}
                </dd>
                <dt>Timeout</dt>
                <dd>{o.timeout_s ? fmt.secs(o.timeout_s) : "No deadline"}</dd>
              </dl>
            </DetailCard>
            <DetailCard title="Scheduling">
              <dl className="kv">
                <dt>Region</dt>
                <dd>{o.region ?? "Awaiting placement"}</dd>
                <dt>GPU class</dt>
                <dd>{o.gpu_class ?? "Selected at placement"}</dd>
                {o.nodes && o.nodes > 1 ? (
                  <>
                    <dt>Nodes</dt>
                    <dd>{o.nodes} (one pod per node, ranks 0 to {o.nodes - 1})</dd>
                    <dt>Interconnect</dt>
                    <dd>
                      {o.interconnect === "required"
                        ? "InfiniBand required"
                        : o.interconnect === "preferred"
                          ? "InfiniBand preferred"
                          : o.interconnect === "none"
                            ? "None (TCP)"
                            : "Not reported"}
                    </dd>
                  </>
                ) : null}
                <dt>Priority</dt>
                <dd>{o.priority ?? "Normal"}</dd>
                <dt>Started</dt>
                <dd>{fmt.dt(o.started_at)}</dd>
                <dt>Duration</dt>
                <dd>{fmt.dur(o.started_at, o.ended_at)}</dd>
                <dt>Cost</dt>
                <dd>{fmt.usd(o.cost)}</dd>
              </dl>
            </DetailCard>
            <DetailCard title="Recovery">
              <dl className="kv">
                <dt>Attempts</dt>
                <dd>{o.attempts?.length ?? 0}</dd>
                <dt>Last attempt</dt>
                <dd>
                  {o.attempts?.[o.attempts.length - 1]?.reason ??
                    "No interruption reported"}
                </dd>
                <dt>Checkpoint</dt>
                <dd>
                  {o.attempts?.[o.attempts.length - 1]
                    ?.resumed_from_checkpoint ?? "Managed by job configuration"}
                </dd>
              </dl>
              <Button
                view="outlined"
                size="l"
                style={{ marginTop: 20 }}
                onClick={() => setTab("Attempts")}
              >
                View attempts
              </Button>
            </DetailCard>
            <DetailCard title="Input">
              <pre className="out">
                {JSON.stringify(o.input ?? {}, null, 2)}
              </pre>
            </DetailCard>
          </div>
        </>
      )}
      {tab === "Metrics" && (
        <ResourceMonitoring
          kind="operations"
          id={id}
          region={o.region}
          end={end}
        />
      )}
      {tab === "Logs" && (
        <ResourceMonitoring
          kind="operations"
          id={id}
          region={o.region}
          end={end}
          view="logs"
        />
      )}
      {tab === "Attempts" && (
        <DetailCard title="Attempts">
          {!o.attempts?.length ? (
            <Empty>This job has not started yet.</Empty>
          ) : (
            <ul className="timeline">
              {o.attempts.map((a) => (
                <li key={a.index}>
                  <span
                    className={`dot ${a.status === "SUCCEEDED" ? "dot--ok" : a.status === "RUNNING" ? "dot--run" : a.status === "PREEMPTED" ? "dot--warn" : "dot--bad"}`}
                  />
                  <div>
                    <strong>Attempt {a.index}</strong>{" "}
                    <Badge status={a.status}>
                      {a.status === "PREEMPTED" ? "Interrupted" : undefined}
                    </Badge>
                    <small>
                      {fmt.dt(a.started_at)} →{" "}
                      {a.ended_at ? fmt.dt(a.ended_at) : "In progress"} ·{" "}
                      {fmt.dur(a.started_at, a.ended_at)}
                    </small>
                    {a.node && <small>Node: {a.node}</small>}
                    {a.gpu_class && <small>GPU: {a.gpu_class}</small>}
                    {a.reason && <small>{a.reason}</small>}
                    {a.resumed_from_checkpoint && (
                      <small>Resumed from {a.resumed_from_checkpoint}</small>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </DetailCard>
      )}
      {tab === "Results" && <Results id={id} status={o.status} />}
      {tab === "Settings" && (
        <DetailCard title="Job actions">
          <p>
            A job's submitted configuration is immutable. Run it again to start
            a fresh operation, or resume a failed or cancelled run when its
            checkpoint volume is still available.
          </p>
          <div className="actions">
            {active ? (
              <Button
                view="outlined-danger"
                size="l"
                onClick={() => setConfirm("cancel")}
              >
                Cancel job
              </Button>
            ) : (
              <Button
                view="outlined"
                size="l"
                onClick={() => setConfirm("again")}
              >
                Run again
              </Button>
            )}
            {resumable && (
              <Button
                view="outlined"
                size="l"
                onClick={() => setConfirm("resume")}
              >
                Resume checkpoint
              </Button>
            )}
          </div>
        </DetailCard>
      )}
      {confirm && (
        <Modal
          title={
            confirm === "cancel"
              ? "Cancel job?"
              : confirm === "resume"
                ? "Resume from checkpoint?"
                : "Run this job again?"
          }
          onClose={() => {
            if (!busy) setConfirm(null);
          }}
        >
          <p>
            {confirm === "cancel"
              ? "Stop this operation. Running containers receive a termination signal and may finish saving their checkpoint."
              : confirm === "resume"
                ? "Create a new operation using this job's existing work volume in the same region. The volume must still be available."
                : "Start a fresh operation with the same configuration and input. Compute usage will count toward your API key budget."}
          </p>
          <div className="actions">
            <Button disabled={busy} onClick={() => setConfirm(null)}>
              Keep current job
            </Button>
            <Button
              view={confirm === "cancel" ? "outlined-danger" : "action"}
              loading={busy}
              onClick={action}
            >
              {confirm === "cancel"
                ? "Cancel job"
                : confirm === "resume"
                  ? "Resume job"
                  : "Run again"}
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}
function Results({ id, status }: { id: string; status: string }) {
  const result = useAsync(() => api().getResult(id), [id, status]);
  if (result.error)
    return <ErrorBox msg={result.error} retry={result.reload} />;
  if (!result.data) return <Loading />;
  return (
    <DetailCard title="Results and artifacts">
      {result.data.artifacts?.length ? (
        <table className="tbl">
          <thead>
            <tr>
              <th>Artifact</th>
              <th>Size</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {result.data.artifacts.map((a) => (
              <tr key={a.name}>
                <td className="mono">{a.name}</td>
                <td>{fmt.bytes(a.size_bytes)}</td>
                <td>
                  <Button view="outlined" size="l" href={a.url} target="_blank">
                    Download
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty>
          {ACTIVE.has(status)
            ? "Results appear as the job produces output."
            : "No result artifacts were returned."}
        </Empty>
      )}
      {result.data.result != null && (
        <pre className="out">{JSON.stringify(result.data.result, null, 2)}</pre>
      )}
    </DetailCard>
  );
}
