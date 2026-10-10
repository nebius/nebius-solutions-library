import { useState } from "react";
import { ArrowDown, ArrowUp, Cube, Plus, Terminal } from "@gravity-ui/icons";
import { api } from "../api/client";
import type { ModelSpec } from "../api/types";
import {
  ScalingFields,
  defaultScaling,
  scalingValidation,
} from "../components/ScalingFields";
import { useSession } from "../components/Session";
import {
  Button,
  ErrorBox,
  Field,
  Icon,
  Loading,
  Modal,
  Section,
  useAsync,
  useToast,
} from "../components/ui";
import { navigate } from "../router";

export function parseEnvironment(text: string) {
  const env: Record<string, string> = {};
  for (const line of text.split("\n").filter((line) => line.trim())) {
    const split = line.indexOf("=");
    const key = line.slice(0, split).trim();
    if (split < 1 || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key))
      throw new Error("Use NAME=value for each environment variable.");
    if (key in env)
      throw new Error(`Environment variable ${key} is listed twice.`);
    env[key] = line.slice(split + 1);
  }
  return env;
}

export function DefinitionForm({
  kind = "endpoint",
  modelId,
}: {
  kind?: ModelSpec["kind"];
  modelId?: string;
}) {
  const model = useAsync(
    () => (modelId ? api().getModel(modelId) : Promise.resolve(null)),
    [modelId],
  );
  if (modelId && model.error)
    return <ErrorBox msg={model.error} retry={model.reload} />;
  if (modelId && model.loading && !model.data) return <Loading />;
  if (modelId && (!model.data?.spec || model.data.managed_by !== "api"))
    return (
      <ErrorBox msg="This definition is managed by the fleet configuration." />
    );
  return (
    <DefinitionEditor
      key={modelId ?? kind}
      initial={model.data?.spec}
      kind={kind}
      onDone={() =>
        navigate(kind === "endpoint" && !modelId ? "/endpoints" : "/models")
      }
      onCancel={() =>
        navigate(kind === "endpoint" && !modelId ? "/endpoints" : "/models")
      }
    />
  );
}

export function DefinitionEditor({
  initial,
  kind = "endpoint",
  onDone,
  onCancel,
}: {
  initial?: ModelSpec;
  kind?: ModelSpec["kind"];
  onDone: () => void;
  onCancel: () => void;
}) {
  const {
    admin,
    key,
    fleet,
    error: sessionError,
    reload: reloadSession,
  } = useSession();
  const toast = useToast();
  const [f, setF] = useState<ModelSpec>(
    initial
      ? {
          ...initial,
          gpu: initial.gpu ?? { count: 0, classes: [] },
          port: initial.port ?? 8000,
          protocol: initial.protocol ?? "http",
        }
      : {
          id: "",
          kind,
          image: "",
          command: "",
          args: [],
          env: {},
          gpu: { count: 0, classes: [] },
          resources: { cpu: "4", memory: "16Gi" },
          cpu: "4",
          memory: "16Gi",
          disk_gi: 50,
          port: 8000,
          protocol: "http",
          scaling: { ...defaultScaling },
          regions: [],
        },
  );
  const [example, setExample] = useState(
    initial?.example ? JSON.stringify(initial.example, null, 2) : "",
  );
  const [exampleError, setExampleError] = useState("");
  const [env, setEnv] = useState(
    Object.entries(f.env ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n"),
  );
  const [args, setArgs] = useState((f.args ?? []).join("\n"));
  const [images, setImages] = useState(
    Object.keys(f.images ?? {}).length ? JSON.stringify(f.images, null, 2) : "",
  );
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [code, setCode] = useState(false);
  const [source, setSource] = useState("custom");
  const [template, setTemplate] = useState("");
  const definitions = useAsync(
    () => (initial ? Promise.resolve([]) : api().listModels()),
    [],
  );
  const templates = (definitions.data ?? []).filter(
    (model) => model.spec?.kind === f.kind,
  );
  function useTemplate(id: string) {
    setTemplate(id);
    const saved = templates.find((model) => model.id === id)?.spec;
    if (!saved) return;
    setF({ ...saved, id: f.id, display_name: f.display_name });
    setEnv(
      Object.entries(saved.env ?? {})
        .map(([k, v]) => `${k}=${v}`)
        .join("\n"),
    );
    setArgs((saved.args ?? []).join("\n"));
    setImages(
      Object.keys(saved.images ?? {}).length
        ? JSON.stringify(saved.images, null, 2)
        : "",
    );
  }
  const set = (patch: Partial<ModelSpec>) =>
    setF((value) => ({ ...value, ...patch }));
  const isJob = f.kind === "job";
  const title = initial
    ? `Edit ${isJob ? "definition" : "endpoint"}`
    : isJob
      ? "Create saved definition"
      : "Create endpoint";
  function spec() {
    const out = {
      ...f,
      env: parseEnvironment(env),
      args: args.split("\n").filter((a) => a.trim()),
      regions: f.regions?.length ? f.regions : undefined,
    };
    if (example.trim()) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(example);
      } catch {
        setExampleError("Not valid JSON");
        throw new Error("Example request must be valid JSON.");
      }
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        setExampleError("Must be a JSON object");
        throw new Error("Example request must be a JSON object.");
      }
      setExampleError("");
      out.example = parsed as Record<string, unknown>;
    } else delete out.example;
    if (images.trim()) {
      const parsed = JSON.parse(images);
      if (
        !parsed ||
        Array.isArray(parsed) ||
        typeof parsed !== "object" ||
        Object.values(parsed).some((v) => typeof v !== "string")
      )
        throw new Error(
          "GPU images must be a JSON object of class names and image paths.",
        );
      out.images = parsed;
    } else delete out.images;
    if (isJob) {
      delete out.resources;
      delete out.port;
      delete out.protocol;
      delete out.scaling;
    } else {
      out.resources = {
        cpu: f.resources?.cpu ?? f.cpu ?? "4",
        memory: f.resources?.memory ?? f.memory ?? "16Gi",
      };
      delete out.cpu;
      delete out.memory;
      delete out.disk_gi;
    }
    return out;
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const invalid: Record<string, string> = {};
    if (source === "saved" && !template)
      invalid.template = "Choose a saved definition.";
    if (
      !/^[a-z0-9][a-z0-9.-]{0,61}[a-z0-9]$/.test(f.id) &&
      !/^[a-z0-9]$/.test(f.id)
    )
      invalid.id = "Use up to 63 lowercase letters, digits, dots or dashes.";
    if (!f.image.trim()) invalid.image = "Enter a container image path.";
    if (!isJob && scalingValidation(f.scaling ?? {}))
      invalid.scaling = scalingValidation(f.scaling ?? {})!;
    if ((f.gpu?.count ?? 0) > 0 && !f.gpu?.classes?.length)
      invalid.gpu = "Choose at least one GPU class.";
    let body: ModelSpec;
    try {
      body = spec();
    } catch (err) {
      invalid.env = (err as Error).message;
      body = f;
    }
    setErrors(invalid);
    if (Object.keys(invalid).length) return;
    setBusy(true);
    setError("");
    try {
      if (initial) await api().updateModel(f.id, body);
      else await api().createModel(body);
      toast(
        initial
          ? "Definition updated"
          : isJob
            ? "Definition created"
            : "Endpoint created",
      );
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function moveClass(index: number, delta: number) {
    const classes = [...(f.gpu?.classes ?? [])];
    [classes[index], classes[index + delta]] = [
      classes[index + delta],
      classes[index],
    ];
    set({ gpu: { ...f.gpu, classes } });
  }
  if (sessionError)
    return <ErrorBox msg={sessionError} retry={reloadSession} />;
  if (!key) return <Loading />;
  if (!admin)
    return (
      <ErrorBox msg="An administrator API key is required to create or edit definitions." />
    );
  return (
    <div className="form-layout">
      <form onSubmit={submit}>
        <div className="page-head">
          <h1>{title}</h1>
        </div>
        {error && <ErrorBox msg={error} />}
        <Section title="Name and context">
          <Field label="Tenant">
            <input className="input" readOnly value={key.tenant ?? ""} />
          </Field>
          <Field
            label="Name *"
            error={errors.id}
            help="This name identifies the endpoint or reusable job definition."
          >
            <input
              className="input"
              value={f.id}
              disabled={Boolean(initial)}
              onChange={(e) => set({ id: e.target.value })}
              placeholder="my-inference-endpoint"
              required
            />
          </Field>
        </Section>
        <Section title={isJob ? "Container settings" : "Endpoint settings"}>
          {!initial && (
            <div className="radio-cards">
              <button
                type="button"
                className={`radio-card ${source === "custom" ? "active" : ""}`}
                aria-pressed={source === "custom"}
                onClick={() => setSource("custom")}
              >
                <Icon data={Terminal} size={22} />
                <strong>Custom container</strong>
                <small>Configure your image and runtime.</small>
              </button>
              <button
                type="button"
                className={`radio-card ${source === "saved" ? "active" : ""}`}
                aria-pressed={source === "saved"}
                onClick={() => setSource("saved")}
              >
                <Icon data={Cube} size={22} />
                <strong>Saved definition</strong>
                <small>Start with an existing configuration.</small>
              </button>
            </div>
          )}
          {source === "saved" && (
            <Field
              label="Saved definition *"
              error={errors.template}
              help="The settings below become a separate definition that you can customize."
            >
              <select
                className="select"
                value={template}
                onChange={(e) => useTemplate(e.target.value)}
                required
              >
                <option value="">
                  {definitions.loading
                    ? "Loading definitions…"
                    : "Choose a definition"}
                </option>
                {templates.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name ?? model.id}
                  </option>
                ))}
              </select>
              {definitions.error && (
                <ErrorBox msg={definitions.error} retry={definitions.reload} />
              )}
              {!definitions.loading &&
                !definitions.error &&
                !templates.length && (
                  <p className="help">
                    No saved {isJob ? "job" : "endpoint"} definitions are
                    available yet.
                  </p>
                )}
            </Field>
          )}
          <Field label="Image path *" error={errors.image}>
            <input
              className="input"
              value={f.image}
              onChange={(e) => set({ image: e.target.value })}
              placeholder="registry.example.com/team/image:tag"
              required
            />
          </Field>
          <Field
            label="Entrypoint command"
            help="A shell command, or leave empty to use the image entrypoint."
          >
            <textarea
              className="textarea mono"
              rows={3}
              value={
                Array.isArray(f.command)
                  ? f.command.join(" ")
                  : (f.command ?? "")
              }
              onChange={(e) => set({ command: e.target.value })}
            />
          </Field>
          <Field
            label="Arguments"
            help="One argument per line. Each line is preserved as one argument."
          >
            <textarea
              className="textarea mono"
              rows={3}
              value={args}
              onChange={(e) => setArgs(e.target.value)}
            />
          </Field>
          <Field
            label="Environment variables"
            help="NAME=value, one variable per line."
            error={errors.env}
          >
            <textarea
              className="textarea mono"
              rows={3}
              value={env}
              onChange={(e) => setEnv(e.target.value)}
              placeholder="MODEL_NAME=example-org/model"
            />
          </Field>
          {!isJob && (
            <div className="row">
              <Field label="Container port *">
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={65535}
                  value={f.port ?? 8000}
                  onChange={(e) => set({ port: Number(e.target.value) })}
                  required
                />
              </Field>
              <Field label="Protocol">
                <select
                  className="select"
                  value={f.protocol}
                  onChange={(e) =>
                    set({ protocol: e.target.value as ModelSpec["protocol"] })
                  }
                >
                  <option value="http">HTTP</option>
                  <option value="openai">OpenAI compatible</option>
                  <option value="websocket">WebSocket</option>
                  <option value="grpc">gRPC</option>
                </select>
              </Field>
            </div>
          )}
          {f.protocol === "openai" && !isJob && (
            <Field
              label="Served model name"
              help="The model name your container expects in inference requests."
            >
              <input
                className="input"
                value={f.served_model ?? ""}
                onChange={(e) => set({ served_model: e.target.value })}
              />
            </Field>
          )}
        </Section>
        <Section title="Hardware">
          <div className="row">
            <Field label="Compute type">
              <select
                className="select"
                value={(f.gpu?.count ?? 0) ? "gpu" : "cpu"}
                onChange={(e) =>
                  set({
                    gpu:
                      e.target.value === "cpu"
                        ? { count: 0, classes: [] }
                        : {
                            count: 1,
                            classes: fleet?.gpu_classes?.slice(0, 1) ?? [],
                          },
                  })
                }
              >
                <option value="cpu">Without GPUs</option>
                <option value="gpu" disabled={!fleet?.gpu_classes?.length}>
                  With GPUs
                </option>
              </select>
            </Field>
            {Boolean(f.gpu?.count) && (
              <Field
                label={
                  (f.nodes ?? 1) > 1 ? "GPUs per node" : "GPUs per replica"
                }
              >
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={8}
                  value={f.gpu?.count}
                  onChange={(e) =>
                    set({ gpu: { ...f.gpu, count: Number(e.target.value) } })
                  }
                />
              </Field>
            )}
            {!isJob && Boolean(f.gpu?.count) && (
              <Field
                label="Nodes"
                help="More than one: the model runs on this many whole nodes at once (tensor x pipeline parallel); started and stopped explicitly, no autoscaling"
              >
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={64}
                  value={f.nodes ?? 1}
                  onChange={(e) => set({ nodes: Number(e.target.value) })}
                />
              </Field>
            )}
            {!isJob && (f.nodes ?? 1) > 1 && (
              <Field
                label="Interconnect"
                help="none: pipeline parallel over Ethernet (pools scale from zero); required: InfiniBand pools only (keep min_nodes there)"
              >
                <select
                  className="input"
                  value={f.interconnect ?? "none"}
                  onChange={(e) =>
                    set({
                      interconnect: e.target
                        .value as ModelSpec["interconnect"],
                    })
                  }
                >
                  <option value="none">none (Ethernet)</option>
                  <option value="preferred">preferred</option>
                  <option value="required">required (InfiniBand)</option>
                </select>
              </Field>
            )}
          </div>
          <div className="row">
            <Field label="CPUs">
              <input
                className="input"
                value={
                  isJob ? (f.cpu ?? "4") : (f.resources?.cpu ?? f.cpu ?? "4")
                }
                onChange={(e) =>
                  set(
                    isJob
                      ? { cpu: e.target.value }
                      : { resources: { ...f.resources, cpu: e.target.value } },
                  )
                }
                required
              />
            </Field>
            <Field label="Memory">
              <input
                className="input"
                value={
                  isJob
                    ? (f.memory ?? "16Gi")
                    : (f.resources?.memory ?? f.memory ?? "16Gi")
                }
                onChange={(e) =>
                  set(
                    isJob
                      ? { memory: e.target.value }
                      : {
                          resources: { ...f.resources, memory: e.target.value },
                        },
                  )
                }
                required
              />
            </Field>
          </div>
          {Boolean(f.gpu?.count) && (
            <>
              <Field
                label="GPU classes"
                error={errors.gpu}
                help={
                  isJob
                    ? "The first class is preferred. Fallback placement also considers capacity and cost."
                    : "Select compatible classes. Endpoint deployments follow the preference order below."
                }
              >
                <div className="preference-tags">
                  {[
                    ...new Set([
                      ...(fleet?.gpu_classes ?? []),
                      ...(f.gpu?.classes ?? []),
                    ]),
                  ].map((c) => (
                    <label key={c} className="chip">
                      <input
                        type="checkbox"
                        checked={f.gpu?.classes?.includes(c) ?? false}
                        onChange={(e) =>
                          set({
                            gpu: {
                              ...f.gpu,
                              classes: e.target.checked
                                ? [...(f.gpu?.classes ?? []), c]
                                : f.gpu?.classes?.filter((v) => v !== c),
                            },
                          })
                        }
                      />{" "}
                      {c.toUpperCase()}
                    </label>
                  ))}
                </div>
              </Field>
              <div className="order" aria-label="GPU fallback order">
                {f.gpu?.classes?.map((c, i) => (
                  <div className="order-row" key={c}>
                    <span className="order-index">{i + 1}</span>
                    <span className="order-name">
                      {c.toUpperCase()}{" "}
                      <span className="muted small">
                        {i === 0 ? "Preferred" : "Fallback"}
                      </span>
                    </span>
                    <Button
                      view="flat"
                      disabled={i === 0}
                      aria-label={`Move ${c} up`}
                      onClick={() => moveClass(i, -1)}
                    >
                      <Icon data={ArrowUp} />
                    </Button>
                    <Button
                      view="flat"
                      disabled={i === (f.gpu?.classes?.length ?? 0) - 1}
                      aria-label={`Move ${c} down`}
                      onClick={() => moveClass(i, 1)}
                    >
                      <Icon data={ArrowDown} />
                    </Button>
                  </div>
                ))}
              </div>
            </>
          )}
        </Section>
        <Section
          title="Regions"
          help="Choose where this definition is available. Leave all unchecked to use compatible fleet regions."
        >
          <div className="choice-list">
            {[
              ...new Set([...(fleet?.regions ?? []), ...(f.regions ?? [])]),
            ].map((r) => (
              <label className="choice" key={r}>
                <input
                  type="checkbox"
                  checked={f.regions?.includes(r) ?? false}
                  onChange={(e) =>
                    set({
                      regions: e.target.checked
                        ? [...(f.regions ?? []), r]
                        : f.regions?.filter((v) => v !== r),
                    })
                  }
                />
                <span>
                  <strong>{r}</strong>
                  <small>Deploy in this region</small>
                </span>
              </label>
            ))}
          </div>
          {!fleet && (
            <span className="help">
              Region choices are unavailable until fleet configuration loads.
            </span>
          )}
        </Section>
        {!isJob && (
          <Section
            title="Scaling"
            help="Scale to zero when idle, or keep a minimum number of replicas warm."
          >
            <ScalingFields
              value={f.scaling ?? {}}
              error={errors.scaling}
              onChange={(scaling) => set({ scaling })}
              timeout={f.timeout_s ?? 600}
              onTimeoutChange={(timeout_s) => set({ timeout_s })}
            />
          </Section>
        )}
        {isJob && (
          <Section title="Storage and recovery">
            <Field label="Work volume (GiB)">
              <input
                className="input"
                type="number"
                min={1}
                value={f.disk_gi ?? 50}
                onChange={(e) => set({ disk_gi: Number(e.target.value) })}
              />
            </Field>
            <Field label="Scratch storage">
              <select
                className="select"
                value={f.scratch ?? "network"}
                onChange={(e) =>
                  set({ scratch: e.target.value as ModelSpec["scratch"] })
                }
              >
                <option value="network">Persistent network volume</option>
                <option value="local-nvme">Local NVMe</option>
              </select>
            </Field>
            <div className="inline-note">
              {f.scratch === "local-nvme"
                ? "Local data is lost when the pod is replaced. Save checkpoints to object storage."
                : "The work volume survives interruptions. Save checkpoints under /work/checkpoint and results under /work/out."}
            </div>
          </Section>
        )}
        <details className="form-section">
          <summary>Advanced settings</summary>
          <div className="form" style={{ marginTop: 20 }}>
            <Field label="Display name">
              <input
                className="input"
                value={f.display_name ?? ""}
                onChange={(e) => set({ display_name: e.target.value })}
                placeholder="Optional friendly name"
              />
            </Field>
            <Field label="Private registry pull secret">
              <input
                className="input"
                value={f.pull_secret ?? ""}
                onChange={(e) => set({ pull_secret: e.target.value })}
                placeholder="Existing registry secret name"
              />
            </Field>
            {!isJob && (
              <>
                <Field label="Readiness path">
                  <input
                    className="input"
                    value={f.health_path ?? ""}
                    onChange={(e) => set({ health_path: e.target.value })}
                    placeholder="/health"
                  />
                </Field>
                <Field
                  label="Example request (JSON)"
                  help="A valid request body for this endpoint; pre-filled in the Test request tab"
                  error={exampleError}
                >
                  <textarea
                    className="input"
                    rows={4}
                    value={example}
                    onChange={(e) => setExample(e.target.value)}
                    placeholder={'{"messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 64}'}
                  />
                </Field>
                {(f.nodes ?? 1) > 1 && (
                  <Field
                    label="Worker command"
                    help="Runs on every node but the leader (the leader runs Command and serves the port); LWS_LEADER_ADDRESS, NODE_RANK, NNODES, GPUS_PER_NODE, MASTER_ADDR are set"
                  >
                    <textarea
                      className="input"
                      rows={2}
                      value={
                        typeof f.worker_command === "string"
                          ? f.worker_command
                          : (f.worker_command ?? []).join(" ")
                      }
                      onChange={(e) =>
                        set({ worker_command: e.target.value || undefined })
                      }
                    />
                  </Field>
                )}
                <Field label="Shared weights path">
                  <input
                    className="input"
                    value={f.weights?.path ?? ""}
                    onChange={(e) =>
                      set({
                        weights: e.target.value
                          ? { ...f.weights, path: e.target.value }
                          : undefined,
                      })
                    }
                  />
                </Field>
                <Field label="Shared memory (GiB)">
                  <input
                    className="input"
                    type="number"
                    min={0}
                    value={f.shm_gib ?? 1}
                    onChange={(e) => set({ shm_gib: Number(e.target.value) })}
                  />
                </Field>
              </>
            )}
            <Field
              label="Images by GPU class"
              help='Optional JSON mapping, for example {"h100":"registry/image:h100","l40s":"registry/image:l40s"}.'
            >
              <textarea
                className="textarea mono"
                value={images}
                onChange={(e) => setImages(e.target.value)}
                rows={4}
              />
            </Field>
            <Field label="Description">
              <textarea
                className="textarea"
                value={f.description ?? ""}
                onChange={(e) => set({ description: e.target.value })}
              />
            </Field>
          </div>
        </details>
        <div className="form-actions">
          <Button view="action" size="xl" type="submit" loading={busy}>
            {initial
              ? "Save changes"
              : isJob
                ? "Create definition"
                : "Create endpoint"}
          </Button>
          <Button view="outlined" size="xl" onClick={() => setCode(true)}>
            View configuration
          </Button>
          <Button view="flat" size="xl" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </form>
      <aside className="card summary">
        <div className="card__head">
          <h2>Configuration summary</h2>
        </div>
        <div className="card__body">
          <dl className="kv">
            <dt>Name</dt>
            <dd>{f.id || "Not set"}</dd>
            <dt>Compute</dt>
            <dd>
              {f.gpu?.count
                ? `${f.gpu.count} GPU · ${f.gpu.classes?.join(" → ")}`
                : "CPU"}
            </dd>
            <dt>Regions</dt>
            <dd>{f.regions?.join(", ") || "All compatible regions"}</dd>
            {!isJob && (
              <>
                <dt>Replicas</dt>
                <dd>
                  {f.scaling?.min ?? 0}–{f.scaling?.max ?? 1}
                </dd>
                <dt>Idle behavior</dt>
                <dd>
                  {f.scaling?.min ? "Keep replicas warm" : "Scale to zero"}
                </dd>
              </>
            )}
          </dl>
          <p className="help" style={{ marginTop: 24 }}>
            Only running resources consume compute. Your API key budget
            continues to apply.
          </p>
        </div>
      </aside>
      {code && (
        <Modal title="Configuration" onClose={() => setCode(false)} width={720}>
          <pre className="out">
            {(() => {
              try {
                return JSON.stringify(spec(), null, 2);
              } catch (e) {
                return (e as Error).message;
              }
            })()}
          </pre>
        </Modal>
      )}
    </div>
  );
}
