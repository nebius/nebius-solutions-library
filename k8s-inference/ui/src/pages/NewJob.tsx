import { useEffect, useState } from "react";
import { Cube, Terminal } from "@gravity-ui/icons";
import { api } from "../api/client";
import type { ModelParam } from "../api/types";
import { useSession } from "../components/Session";
import {
  Button,
  ErrorBox,
  Field,
  Icon,
  Loading,
  Modal,
  Section,
  fmt,
  useAsync,
  useToast,
} from "../components/ui";
import { parseEnvironment } from "./DefinitionForm";
import { navigate } from "../router";

export function NewJob({ initialModel }: { initialModel?: string }) {
  const { key, fleet } = useSession();
  const toast = useToast();
  const models = useAsync(() => api().listModels(), []);
  const [source, setSource] = useState(initialModel ? "saved" : "image");
  const [modelId, setModelId] = useState(initialModel ?? "");
  const [name, setName] = useState("");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [files, setFiles] = useState<Record<string, File>>({});
  const [env, setEnv] = useState("");
  const [args, setArgs] = useState("");
  const [region, setRegion] = useState("");
  const [priority, setPriority] = useState("normal");
  const [timeout, setTimeout] = useState(0);
  const [mode, setMode] = useState<"run" | "async">("run");
  const [nodes, setNodes] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [code, setCode] = useState(false);
  const genericId = nodes > 1 ? "distributed-run" : "container-run";
  const model = models.data?.find(
    (m) => m.id === (source === "image" ? genericId : modelId),
  );
  useEffect(() => {
    if (!model) return;
    const defaults = Object.fromEntries(
      (model.parameters ?? [])
        .filter((p) => p.default != null)
        .map((p) => [
          p.name,
          typeof p.default === "object" ? JSON.stringify(p.default) : p.default,
        ]),
    );
    setParams((current) =>
      source === "image"
        ? {
            ...defaults,
            ...Object.fromEntries(
              ["image", "command", "input_prefix"]
                .filter((k) => current[k] != null && current[k] !== "")
                .map((k) => [k, current[k]]),
            ),
          }
        : defaults,
    );
    setFiles({});
    setMode(model.modes.includes("run") ? "run" : "async");
    setRegion(fleet?.fleet_manager ? "" : (model.regions[0]?.region ?? ""));
  }, [model?.id, source, fleet?.fleet_manager]);
  const setParam = (p: string, value: unknown) =>
    setParams((current) => ({ ...current, [p]: value }));
  function payload() {
    const declared = new Set((model?.parameters ?? []).map((p) => p.name));
    const input = Object.fromEntries(
      Object.entries(params).filter(([k]) => declared.has(k)),
    ) as Record<string, unknown>;
    if (source === "image" && nodes > 1) input.nodes = nodes;
    for (const [k, v] of Object.entries(input))
      if (
        model?.parameters?.find((p) => p.name === k)?.type === "text" &&
        typeof v === "string" &&
        /^\s*[\[{]/.test(v)
      ) {
        try {
          input[k] = JSON.parse(v);
        } catch {
          throw new Error(`${k}: enter valid JSON.`);
        }
      }
    if (source === "image") {
      const environment = parseEnvironment(env);
      if (Object.keys(environment).length) input.env = environment;
      const argv = args.split("\n").filter((a) => a.trim());
      if (argv.length) input.args = argv;
    }
    return {
      name: name.trim(),
      mode,
      input,
      ...(region ? { region } : {}),
      priority,
      ...(timeout > 0 ? { timeout_s: Math.round(timeout * 3600) } : {}),
    };
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const invalid: Record<string, string> = {};
    if (!name.trim()) invalid.name = "Enter a job name.";
    if (!model) invalid.model = "Choose an available definition.";
    model?.parameters?.forEach((p) => {
      if (
        p.required &&
        (params[p.name] == null || params[p.name] === "") &&
        !files[p.name]
      )
        invalid[p.name] = `${p.label ?? p.name} is required.`;
    });
    setErrors(invalid);
    if (Object.keys(invalid).length) return;
    setBusy(true);
    setError("");
    try {
      const request = payload();
      for (const [p, file] of Object.entries(files))
        request.input[p] = await api().uploadArtifact(
          file,
          region || undefined,
        );
      const result = await api().invoke(model!.id, request);
      const op = "operation" in result ? result.operation : result;
      toast("Job submitted");
      navigate(`/jobs/${op.id}`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (models.error)
    return <ErrorBox msg={models.error} retry={models.reload} />;
  if (!models.data) return <Loading />;
  const available = models.data.filter(
    (m) => m.modes.includes("run") || m.modes.includes("async"),
  );
  const generic = source === "image";
  const hidden = new Set([
    "image",
    "command",
    "args",
    "env",
    "gpus",
    "cpu",
    "memory",
    "disk_gi",
    "scratch",
    "input_prefix",
    "nodes",
    "gpus_per_node",
    "interconnect",
    "checkpoints",
  ]);
  const multi = generic && nodes > 1;
  return (
    <div className="form-layout">
      <form onSubmit={submit}>
        <div className="page-head">
          <h1>Create job</h1>
        </div>
        {error && <ErrorBox msg={error} />}
        <Section title="Name and context">
          <Field label="Tenant">
            <input className="input" value={key?.tenant ?? ""} readOnly />
          </Field>
          <Field label="Name *" error={errors.name}>
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-training-job"
              required
            />
          </Field>
        </Section>
        <Section title="Job settings">
          <div className="radio-cards">
            <button
              type="button"
              className={`radio-card ${generic ? "active" : ""}`}
              onClick={() => setSource("image")}
            >
              <Icon data={Terminal} size={22} />
              <strong>Custom container</strong>
              <small>Run your image and command.</small>
            </button>
            <button
              type="button"
              className={`radio-card ${!generic ? "active" : ""}`}
              onClick={() => setSource("saved")}
            >
              <Icon data={Cube} size={22} />
              <strong>Saved definition</strong>
              <small>Reuse container and hardware settings.</small>
            </button>
          </div>
          {!generic && (
            <Field label="Saved definition *" error={errors.model}>
              <select
                className="select"
                required
                value={modelId}
                onChange={(e) => setModelId(e.target.value)}
              >
                <option value="">Choose a definition</option>
                {available.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {generic && !model && (
            <ErrorBox msg="The generic container-run definition is not available in this fleet." />
          )}
          {generic && model && (
            <>
              <Field label="Image path *" error={errors.image}>
                <input
                  className="input"
                  required
                  value={String(params.image ?? "")}
                  onChange={(e) => setParam("image", e.target.value)}
                  placeholder="registry.example.com/team/image:tag"
                />
              </Field>
              <Field
                label="Entrypoint command *"
                error={errors.command}
                help="Executed by /bin/sh -c. Save results under /work/out."
              >
                <textarea
                  className="textarea mono"
                  rows={3}
                  required
                  value={String(params.command ?? "")}
                  onChange={(e) => setParam("command", e.target.value)}
                  placeholder="python train.py"
                />
              </Field>
              <Field
                label="Arguments"
                help="One argument per line; each line stays intact."
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
              >
                <textarea
                  className="textarea mono"
                  rows={3}
                  value={env}
                  onChange={(e) => setEnv(e.target.value)}
                />
              </Field>
            </>
          )}
          {model && model.modes.filter((m) => m !== "sync").length > 1 && (
            <Field label="Execution mode">
              <select
                className="select"
                value={mode}
                onChange={(e) => setMode(e.target.value as "run" | "async")}
              >
                {model.modes
                  .filter((m) => m !== "sync")
                  .map((m) => (
                    <option key={m}>{m}</option>
                  ))}
              </select>
            </Field>
          )}
          {model?.parameters
            ?.filter((p) => !generic || !hidden.has(p.name))
            .map((p) => (
              <ParamInput
                key={p.name}
                p={p}
                value={params[p.name]}
                error={errors[p.name]}
                onChange={(v) => setParam(p.name, v)}
                onFile={(file) =>
                  setFiles((current) => {
                    const next = { ...current };
                    if (file) next[p.name] = file;
                    else delete next[p.name];
                    return next;
                  })
                }
              />
            ))}
        </Section>
        {generic && model && (
          <Section title="Hardware">
            <div className="row">
              <Field
                label="Nodes"
                help="More than one node runs the image on whole GPU nodes at once (one pod per node, torchrun/NCCL conventions: MASTER_ADDR, NODE_RANK, WORLD_SIZE)."
              >
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={64}
                  value={nodes}
                  onChange={(e) =>
                    setNodes(Math.max(1, Math.round(Number(e.target.value) || 1)))
                  }
                />
              </Field>
              {multi ? (
                <Field
                  label="GPUs per node"
                  help="A whole node: the GPU count of the pool's preset (8 on H100, H200, B200 and B300 nodes)."
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={8}
                    value={Number(params.gpus_per_node ?? 8)}
                    onChange={(e) =>
                      setParam("gpus_per_node", Number(e.target.value))
                    }
                  />
                </Field>
              ) : (
                <Field label="GPUs">
                  <input
                    className="input"
                    type="number"
                    min={0}
                    max={8}
                    value={Number(params.gpus ?? 1)}
                    onChange={(e) => setParam("gpus", Number(e.target.value))}
                  />
                </Field>
              )}
            </div>
            {multi && (
              <Field
                label="Interconnect"
                help="Required: InfiniBand pools only, the pod gets every fabric NIC of its node. Preferred: InfiniBand when free, else any whole-node pool. None: NCCL over TCP, an explicit opt-in."
              >
                <select
                  className="select"
                  value={String(params.interconnect ?? "required")}
                  onChange={(e) => setParam("interconnect", e.target.value)}
                >
                  <option value="required">InfiniBand required</option>
                  <option value="preferred">InfiniBand preferred</option>
                  <option value="none">None (TCP)</option>
                </select>
              </Field>
            )}
            <div className="row">
              <Field label="CPUs">
                <input
                  className="input"
                  value={String(params.cpu ?? "8")}
                  onChange={(e) => setParam("cpu", e.target.value)}
                  required
                />
              </Field>
              <Field label="Memory">
                <input
                  className="input"
                  value={String(params.memory ?? "64Gi")}
                  onChange={(e) => setParam("memory", e.target.value)}
                  required
                />
              </Field>
            </div>
          </Section>
        )}
        <Section title="Scheduling">
          <Field
            label="Region"
            help={
              fleet?.fleet_manager
                ? "Automatic placement selects a compatible worker region."
                : "Choose an available region for this definition."
            }
          >
            <select
              className="select"
              value={region}
              onChange={(e) => setRegion(e.target.value)}
            >
              {fleet?.fleet_manager && (
                <option value="">
                  {mode === "run"
                    ? "Automatic · capacity and cost"
                    : "Automatic"}
                </option>
              )}
              {model?.regions.map((r) => (
                <option key={r.region} value={r.region}>
                  {r.region}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Priority">
            <select
              className="select"
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            >
              <option value="low">Low</option>
              <option value="normal">Normal</option>
              <option value="high">High</option>
            </select>
          </Field>
          <Field label="Job timeout (hours)" help="0 means no deadline.">
            <input
              className="input"
              type="number"
              min={0}
              step={0.5}
              value={timeout}
              onChange={(e) => setTimeout(Number(e.target.value))}
            />
          </Field>
        </Section>
        {generic && model && (
          <Section title="Storage and recovery">
            <Field label={multi ? "Scratch per node (GiB)" : "Work volume (GiB)"}>
              <input
                className="input"
                type="number"
                min={1}
                value={Number(params.disk_gi ?? (multi ? 500 : 100))}
                onChange={(e) => setParam("disk_gi", Number(e.target.value))}
              />
            </Field>
            {multi && (
              <Field
                label="Checkpoints"
                help="Shared: the tenant's claim on the region's shared filesystem; restarts and resume continue from it. Local: node scratch, lost when a node goes."
              >
                <select
                  className="select"
                  value={String(params.checkpoints ?? "local")}
                  onChange={(e) => setParam("checkpoints", e.target.value)}
                >
                  <option value="local">Local node scratch</option>
                  <option value="shared">Shared filesystem</option>
                </select>
              </Field>
            )}
            {!multi && (
            <Field label="Scratch storage">
              <select
                className="select"
                value={String(params.scratch ?? "network")}
                onChange={(e) => setParam("scratch", e.target.value)}
              >
                <option value="network">Persistent network volume</option>
                <option value="local-nvme">Local NVMe</option>
              </select>
            </Field>
            )}
            <div className="inline-note">
              {multi
                ? "Every node syncs the input prefix to /work/in; rank 0 uploads /work/out. Any node loss restarts all ranks from the last checkpoint under /work/checkpoint."
                : params.scratch === "local-nvme"
                  ? "Local data is lost on pod replacement. Save checkpoints to object storage."
                  : "Save checkpoints under /work/checkpoint. The volume survives interruptions and can be used to resume a stopped run."}
            </div>
            <Field
              label="Input prefix"
              help="Optional object storage location copied to /work/in."
            >
              <input
                className="input"
                value={String(params.input_prefix ?? "")}
                onChange={(e) => setParam("input_prefix", e.target.value)}
                placeholder="s3://your-bucket/input/"
              />
            </Field>
          </Section>
        )}
        <div className="form-actions">
          <Button
            view="action"
            size="xl"
            type="submit"
            loading={busy}
            disabled={!model}
          >
            Create job
          </Button>
          <Button view="outlined" size="xl" onClick={() => setCode(true)}>
            View configuration
          </Button>
          <Button view="flat" size="xl" onClick={() => navigate("/jobs")}>
            Cancel
          </Button>
        </div>
      </form>
      <aside className="card summary">
        <div className="card__head">
          <h2>Job summary</h2>
        </div>
        <div className="card__body">
          <dl className="kv">
            <dt>Name</dt>
            <dd>{name || "Not set"}</dd>
            <dt>Definition</dt>
            <dd>{model?.name ?? "Not selected"}</dd>
            <dt>Region</dt>
            <dd>{region || "Automatic"}</dd>
            <dt>Hardware</dt>
            <dd>
              {generic
                ? `${params.gpus ?? 1} GPUs · ${params.cpu ?? 8} CPUs`
                : (model?.gpu ?? "—")}
            </dd>
            <dt>Priority</dt>
            <dd>{priority}</dd>
            <dt>Timeout</dt>
            <dd>{timeout ? fmt.secs(timeout * 3600) : "No deadline"}</dd>
            <dt>Budget remaining</dt>
            <dd>
              {key?.budget != null
                ? fmt.usd(Math.max(0, key.budget - key.spend))
                : "No limit"}
            </dd>
          </dl>
          <p className="help" style={{ marginTop: 24 }}>
            Follow progress, metrics, logs, attempts, and results on the job
            page after submission.
          </p>
        </div>
      </aside>
      {code && (
        <Modal
          title="Job configuration"
          onClose={() => setCode(false)}
          width={720}
        >
          <pre className="out">
            {(() => {
              try {
                return JSON.stringify(
                  { definition: model?.id, ...payload() },
                  null,
                  2,
                );
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

function ParamInput({
  p,
  value,
  error,
  onChange,
  onFile,
}: {
  p: ModelParam;
  value: unknown;
  error?: string;
  onChange: (v: unknown) => void;
  onFile: (f: File | null) => void;
}) {
  const label =
    (p.label ?? p.name.replace(/_/g, " ")) + (p.required ? " *" : "");
  const common = { label, help: p.help, error };
  if (p.type === "select")
    return (
      <Field {...common}>
        <select
          className="select"
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        >
          {p.options?.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      </Field>
    );
  if (p.type === "boolean")
    return (
      <Field {...common}>
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
      </Field>
    );
  if (p.type === "file")
    return (
      <Field {...common}>
        <input
          className="input"
          type="file"
          required={p.required}
          onChange={(e) => {
            const file = e.target.files?.[0] ?? null;
            onFile(file);
            onChange(file?.name ?? "");
          }}
        />
      </Field>
    );
  if (p.type === "text")
    return (
      <Field {...common}>
        <textarea
          className="textarea mono"
          required={p.required}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      </Field>
    );
  return (
    <Field {...common}>
      <input
        className="input"
        required={p.required}
        type={p.type === "number" ? "number" : "text"}
        value={String(value ?? "")}
        onChange={(e) =>
          onChange(
            p.type === "number" ? Number(e.target.value) : e.target.value,
          )
        }
      />
    </Field>
  );
}
