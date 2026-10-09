import { useState } from "react";
import { api } from "../api/client";
import type { Model, ModelSpec } from "../api/types";
import {
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  useAsync,
  useToast,
} from "../components/ui";
import {
  Button,
  Input,
  Area,
  Pick,
  MultiPick,
  Search,
  PageHeader,
  FormSection,
} from "../components/controls";
import { useAdmin, useSession } from "../components/Session";
import { href, navigate } from "../router";

export function endpointPath(id: string, region?: string) {
  return `/endpoints/${encodeURIComponent(id.replace(/\./g, "-"))}${region ? "?region=" + encodeURIComponent(region) : ""}`;
}
export function parseEnvironment(text: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of text.split("\n").filter((l) => l.trim())) {
    const at = line.indexOf("=");
    const key = line.slice(0, at).trim();
    if (at < 1 || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key))
      throw new Error("Use NAME=value for each environment variable.");
    if (key in env)
      throw new Error(`Environment variable ${key} is listed twice.`);
    env[key] = line.slice(at + 1).replace(/\r$/, "");
  }
  return env;
}
export function prepareSpec(
  f: ModelSpec,
  args: string,
  env: string,
  classes: string,
): ModelSpec {
  const spec: ModelSpec = {
    ...f,
    args: args.split("\n").filter((s) => s.length > 0),
    env: parseEnvironment(env),
    gpu: {
      ...f.gpu,
      classes: classes
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    },
  };
  if (!/^[a-z][a-z0-9-]{1,40}$/.test(spec.id))
    throw new Error(
      "Name must be 2–41 characters: lowercase letters, numbers and dashes, starting with a letter.",
    );
  if (!spec.image.trim()) throw new Error("Container image is required.");
  if (!Number.isInteger(spec.gpu?.count) || spec.gpu!.count! < 0)
    throw new Error("GPU count must be a whole number of zero or more.");
  if (spec.kind === "endpoint") {
    const { min = 0, max = 1, target = 4 } = spec.scaling ?? {};
    if (
      ![min, max, target].every(Number.isInteger) ||
      min < 0 ||
      max < 1 ||
      max < min ||
      target < 1
    )
      throw new Error(
        "Scaling requires whole numbers: minimum ≥ 0, maximum ≥ 1 and minimum, concurrency ≥ 1.",
      );
    if (!Number.isInteger(spec.port) || spec.port! < 1 || spec.port! > 65535)
      throw new Error("Container port must be between 1 and 65535.");
    delete spec.cpu;
    delete spec.memory;
    delete spec.disk_gi;
  } else {
    if (!Number.isInteger(spec.disk_gi) || spec.disk_gi! < 1)
      throw new Error("Work volume must be a positive whole number.");
    delete spec.port;
    delete spec.protocol;
    delete spec.scaling;
    delete spec.health_path;
    delete spec.served_model;
  }
  return spec;
}

export function Models() {
  const toast = useToast();
  const admin = useAdmin();
  const { data, error, loading, reload } = useAsync(
    () => api().listModels(),
    [],
  );
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("all");
  const [deleting, setDeleting] = useState<Model | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = (data ?? []).filter(
    (m) =>
      (kind === "all" || (kind === "job") === (m.default_mode === "run")) &&
      `${m.name} ${m.id} ${m.image ?? ""}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      await api().deleteModel(deleting.id);
      toast("Model deleted");
      setDeleting(null);
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageHeader
        title="Models"
        count={data?.length}
        description="Reusable container configurations for endpoints and jobs."
      >
        {admin && (
          <Button primary href={href("/models/new")}>
            Create model
          </Button>
        )}
      </PageHeader>
      {error && <ErrorBox msg={error} retry={reload} />}
      <div className="toolbar">
        <div className="toolbar__search">
          <Search value={search} onChange={setSearch} label="Search models" />
        </div>
        <Pick
          value={kind}
          onChange={setKind}
          aria-label="Model type"
          options={[
            { value: "all", label: "All types" },
            { value: "endpoint", label: "Endpoints" },
            { value: "job", label: "Job classes" },
          ]}
        />
        <span className="toolbar__count">{rows.length} models</span>
        <Button onClick={reload} size="m">
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
                  <th>Type</th>
                  <th>GPU</th>
                  <th>Regions</th>
                  <th>Source</th>
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
                          {search || kind !== "all"
                            ? "No matching models"
                            : "No models yet"}
                        </h2>
                        <p>
                          {search || kind !== "all"
                            ? "Try changing the search or type filter."
                            : "Create a model to reuse a container configuration."}
                        </p>
                      </Empty>
                    </td>
                  </tr>
                )}
                {rows.map((m) => (
                  <tr key={m.id}>
                    <td>
                      <strong>{m.name}</strong>
                      <div className="small muted mono">{m.id}</div>
                      {m.description && (
                        <div className="small muted table-description">
                          {m.description}
                        </div>
                      )}
                    </td>
                    <td>
                      {m.default_mode === "run" ? "Job class" : "Endpoint"}
                    </td>
                    <td>{m.gpu}</td>
                    <td>{m.regions.map((r) => r.region).join(", ") || "—"}</td>
                    <td>{m.managed_by === "api" ? "Custom" : "Built-in"}</td>
                    <td>
                      <div className="actions">
                        <Button
                          size="m"
                          href={href(
                            m.default_mode === "run"
                              ? `/jobs/new?model=${encodeURIComponent(m.id)}`
                              : endpointPath(m.id, m.regions[0]?.region),
                          )}
                        >
                          {m.default_mode === "run"
                            ? "Run job"
                            : "View endpoint"}
                        </Button>
                        {admin && m.managed_by === "api" && (
                          <>
                            <Button
                              size="m"
                              href={href(
                                `/models/${encodeURIComponent(m.id)}/edit`,
                              )}
                            >
                              Edit
                            </Button>
                            <Button
                              size="m"
                              view="flat-danger"
                              onClick={() => setDeleting(m)}
                            >
                              Delete
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
      {deleting && (
        <Modal title="Delete model" onClose={() => !busy && setDeleting(null)}>
          <p>
            Delete <strong>{deleting.name}</strong>? Its endpoints will be
            removed from every region. Running jobs are not stopped.
          </p>
          <div className="dialog-actions">
            <Button disabled={busy} onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button danger loading={busy} onClick={remove}>
              Delete model
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}

export function ModelEditor({
  id,
  kind = "endpoint",
}: {
  id?: string;
  kind?: ModelSpec["kind"];
}) {
  const admin = useAdmin();
  const model = useAsync(
    () => (id ? api().getModel(id) : Promise.resolve(null)),
    [id],
  );
  if (!admin)
    return (
      <div className="empty">
        <h2>Administrator access required</h2>
        <p>Use an administrator key to create or edit models.</p>
        <Button href={href("/models")}>Back to models</Button>
      </div>
    );
  if (id && model.error)
    return <ErrorBox msg={model.error} retry={model.reload} />;
  if (id && !model.data) return <Loading />;
  if (id && (!model.data?.spec || model.data.managed_by !== "api"))
    return (
      <ErrorBox msg="This model is managed by the deployment. Update its configuration at the source." />
    );
  return <ModelForm key={id ?? kind} initial={model.data?.spec} kind={kind} />;
}
function ModelForm({
  initial,
  kind,
}: {
  initial?: ModelSpec;
  kind: ModelSpec["kind"];
}) {
  const { fleet } = useSession();
  const toast = useToast();
  const editing = !!initial;
  const [f, setF] = useState<ModelSpec>(() => ({
    id: "",
    kind,
    image: "",
    command: "",
    cpu: "4",
    memory: "16Gi",
    disk_gi: 50,
    regions: [],
    ...initial,
    gpu: {
      count: initial
        ? (initial.gpu?.count ?? (initial.gpu?.classes?.length ? 1 : 0))
        : 1,
      ...initial?.gpu,
    },
    port: initial?.port ?? (initial ? 8080 : 8000),
    protocol: initial?.protocol ?? (initial ? "http" : "openai"),
    resources: { cpu: "4", memory: "16Gi", ...initial?.resources },
    scaling: { min: 0, max: 1, target: 4, ...initial?.scaling },
  }));
  const [args, setArgs] = useState((initial?.args ?? []).join("\n"));
  const [env, setEnv] = useState(
    Object.entries(initial?.env ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n"),
  );
  const [classes, setClasses] = useState(
    (initial?.gpu?.classes ?? []).join(", "),
  );
  const [command, setCommand] = useState(
    Array.isArray(f.command) ? JSON.stringify(f.command) : (f.command ?? ""),
  );
  const [commandType, setCommandType] = useState(
    Array.isArray(f.command) ? "argv" : "shell",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (p: Partial<ModelSpec>) => setF((s) => ({ ...s, ...p }));
  const job = f.kind === "job";
  const regions = [
    ...new Set([...(fleet?.regions ?? []), ...(f.regions ?? [])]),
  ];
  const back = job || editing ? "/models" : "/endpoints";
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    let spec: ModelSpec;
    try {
      const cmd =
        commandType === "argv" ? JSON.parse(command || "[]") : command;
      if (
        commandType === "argv" &&
        (!Array.isArray(cmd) ||
          !cmd.every((a: unknown) => typeof a === "string"))
      )
        throw new Error("Entrypoint must be a JSON array of strings.");
      spec = prepareSpec({ ...f, command: cmd }, args, env, classes);
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    setBusy(true);
    try {
      if (editing) await api().updateModel(spec.id, spec);
      else await api().createModel(spec);
      toast(
        editing
          ? "Configuration saved"
          : job
            ? "Job class created"
            : "Endpoint deployment started",
      );
      navigate(back);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageHeader
        title={
          editing
            ? `Edit ${initial.display_name || initial.id}`
            : job
              ? "Create model"
              : "Create endpoint"
        }
        description={
          job
            ? "Configure a reusable job class for your container."
            : "Deploy a container with an inference API that scales with demand."
        }
      />
      <form onSubmit={save} className="editor-grid">
        <div className="editor-main">
          <FormSection
            title="General"
            description="Choose how this model will be used."
          >
            {!editing && kind === "job" && (
              <Field label="Model type">
                <Pick
                  value={f.kind}
                  onChange={(v) => set({ kind: v as ModelSpec["kind"] })}
                  options={[
                    { value: "endpoint", label: "Endpoint" },
                    { value: "job", label: "Job class" },
                  ]}
                />
              </Field>
            )}
            <div className="form-grid">
              <Field
                label="Name"
                help="2–41 lowercase letters, numbers and dashes."
              >
                <Input
                  value={f.id}
                  onChange={(v) => set({ id: v })}
                  disabled={editing}
                  placeholder="my-model"
                  controlProps={{ required: true }}
                />
              </Field>
              <Field
                label="Display name"
                help="Optional name shown in the console."
              >
                <Input
                  value={f.display_name ?? ""}
                  onChange={(v) => set({ display_name: v })}
                />
              </Field>
            </div>
            <Field label="Description">
              <Input
                value={f.description ?? ""}
                onChange={(v) => set({ description: v })}
              />
            </Field>
            <Field
              label="Regions"
              help="Leave empty to use every compatible region."
            >
              <MultiPick
                value={f.regions ?? []}
                onUpdate={(v) => set({ regions: v })}
                options={regions}
                placeholder={
                  regions.length
                    ? "All compatible regions"
                    : "Region information unavailable"
                }
                disabled={!regions.length}
              />
            </Field>
          </FormSection>
          <FormSection
            title="Container"
            description="Image and startup configuration."
          >
            <Field
              label="Container image"
              help="Full image path with a tag or digest."
            >
              <Input
                value={f.image}
                mono
                onChange={(v) => set({ image: v })}
                placeholder="registry.example.com/team/model:tag"
                controlProps={{ required: true }}
              />
            </Field>
            <Field
              label="Registry pull secret"
              help="Optional name of an existing registry secret."
            >
              <Input
                value={f.pull_secret ?? ""}
                onChange={(v) => set({ pull_secret: v })}
              />
            </Field>
            <div className="form-grid">
              <Field label="Entrypoint format">
                <Pick
                  value={commandType}
                  onChange={(v) => {
                    if (command.trim() && v !== commandType) {
                      setError(
                        "Clear the entrypoint before changing its format.",
                      );
                      return;
                    }
                    setCommandType(v);
                  }}
                  options={[
                    { value: "shell", label: "Shell command" },
                    { value: "argv", label: "Argument vector (JSON)" },
                  ]}
                />
              </Field>
              <Field
                label="Entrypoint"
                help={
                  commandType === "argv"
                    ? 'JSON array, e.g. ["python", "serve.py"].'
                    : "Optional. Leave empty to use the image entrypoint."
                }
              >
                <Input value={command} mono onChange={setCommand} />
              </Field>
            </div>
            <Field
              label="Arguments"
              help="One argument per line. Spaces within a line are preserved."
            >
              <Area value={args} onChange={setArgs} rows={3} />
            </Field>
            <Field
              label="Environment variables"
              help="NAME=value, one per line. Use existing secret references in your deployment for sensitive values."
            >
              <Area value={env} onChange={setEnv} rows={3} />
            </Field>
          </FormSection>
          <FormSection
            title="Resources"
            description="Resources allocated to each replica or run."
          >
            <div className="form-grid">
              <Field label="GPU count">
                <Input
                  type="number"
                  value={f.gpu?.count ?? 1}
                  onChange={(v) => set({ gpu: { ...f.gpu, count: Number(v) } })}
                  controlProps={{ min: 0, step: 1, required: true }}
                />
              </Field>
              <Field label="GPU classes" help="Empty uses the fleet defaults.">
                {fleet?.gpu_classes?.length ? (
                  <MultiPick
                    value={classes
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean)}
                    onUpdate={(v) => setClasses(v.join(", "))}
                    options={[
                      ...new Set([
                        ...fleet.gpu_classes,
                        ...classes
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      ]),
                    ]}
                    placeholder="Automatic"
                  />
                ) : (
                  <Input
                    value={classes}
                    onChange={setClasses}
                    placeholder="Comma-separated fleet classes"
                  />
                )}
              </Field>
            </div>
            <div className="form-grid">
              <Field label="CPU">
                <Input
                  value={job ? (f.cpu ?? "4") : (f.resources?.cpu ?? "4")}
                  onChange={(v) =>
                    set(
                      job
                        ? { cpu: v }
                        : { resources: { ...f.resources, cpu: v } },
                    )
                  }
                />
              </Field>
              <Field label="Memory">
                <Input
                  value={
                    job ? (f.memory ?? "16Gi") : (f.resources?.memory ?? "16Gi")
                  }
                  onChange={(v) =>
                    set(
                      job
                        ? { memory: v }
                        : { resources: { ...f.resources, memory: v } },
                    )
                  }
                  placeholder="16Gi"
                />
              </Field>
            </div>
            {job && (
              <Field
                label="Work volume (GiB)"
                help="Persistent storage for this run and its checkpoints."
              >
                <Input
                  type="number"
                  value={f.disk_gi ?? 50}
                  onChange={(v) => set({ disk_gi: Number(v) })}
                  controlProps={{ min: 1, step: 1, required: true }}
                />
              </Field>
            )}
          </FormSection>
          {!job && (
            <>
              <FormSection
                title="Networking"
                description="How callers reach the container."
              >
                <div className="form-grid">
                  <Field label="Protocol">
                    <Pick
                      value={f.protocol ?? "openai"}
                      onChange={(v) =>
                        set({ protocol: v as ModelSpec["protocol"] })
                      }
                      options={[
                        { value: "openai", label: "OpenAI compatible" },
                        { value: "http", label: "HTTP" },
                        { value: "websocket", label: "WebSocket" },
                        { value: "grpc", label: "gRPC" },
                      ]}
                    />
                  </Field>
                  <Field label="Container port">
                    <Input
                      type="number"
                      value={f.port ?? 8000}
                      onChange={(v) => set({ port: Number(v) })}
                      controlProps={{ min: 1, max: 65535, required: true }}
                    />
                  </Field>
                </div>
                <div className="form-grid">
                  <Field label="Health check path">
                    <Input
                      value={f.health_path ?? ""}
                      mono
                      onChange={(v) => set({ health_path: v })}
                      placeholder="/health"
                    />
                  </Field>
                  <Field
                    label="Served model name"
                    help="Model name expected by an OpenAI compatible server."
                  >
                    <Input
                      value={f.served_model ?? ""}
                      onChange={(v) => set({ served_model: v })}
                    />
                  </Field>
                </div>
              </FormSection>
              <FormSection
                title="Autoscaling"
                description="Set the replica limits and requests per replica."
              >
                <div className="form-grid form-grid--three">
                  {(
                    [
                      ["min", "Minimum replicas", 0],
                      ["max", "Maximum replicas", 1],
                      ["target", "Target concurrency", 4],
                    ] as const
                  ).map(([k, label, def]) => (
                    <Field key={k} label={label}>
                      <Input
                        type="number"
                        value={f.scaling?.[k] ?? def}
                        onChange={(v) =>
                          set({ scaling: { ...f.scaling, [k]: Number(v) } })
                        }
                        controlProps={{
                          min: k === "min" ? 0 : 1,
                          step: 1,
                          required: true,
                        }}
                      />
                    </Field>
                  ))}
                </div>
                <p className="help">
                  A minimum of zero allows the endpoint to scale down when idle.
                </p>
              </FormSection>
            </>
          )}
          {error && <ErrorBox msg={error} />}
          <div className="form-actions">
            <Button href={href(back)} disabled={busy}>
              Cancel
            </Button>
            <Button primary type="submit" loading={busy}>
              {editing
                ? "Save changes"
                : job
                  ? "Create model"
                  : "Create endpoint"}
            </Button>
          </div>
        </div>
        <aside className="editor-summary">
          <h2>Configuration summary</h2>
          <dl className="kv">
            <dt>Type</dt>
            <dd>{job ? "Job class" : "Endpoint"}</dd>
            <dt>Name</dt>
            <dd>{f.id || "—"}</dd>
            <dt>GPU</dt>
            <dd>
              {f.gpu?.count ?? 1} per {job ? "run" : "replica"}
            </dd>
            <dt>Regions</dt>
            <dd>{f.regions?.join(", ") || "Automatic"}</dd>
            {!job && (
              <>
                <dt>Replicas</dt>
                <dd>
                  {f.scaling?.min ?? 0}–{f.scaling?.max ?? 1}
                </dd>
              </>
            )}
          </dl>
          <p>Changes apply to every selected region.</p>
        </aside>
      </form>
    </>
  );
}
