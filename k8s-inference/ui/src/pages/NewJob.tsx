import { useEffect, useState } from "react";
import { Checkbox } from "@gravity-ui/uikit";
import { api } from "../api/client";
import type { Model, ModelParam, InvokeRequest } from "../api/types";
import {
  Empty,
  ErrorBox,
  Field,
  Loading,
  fmt,
  useAsync,
  useToast,
} from "../components/ui";
import {
  Button,
  Input,
  Area,
  Pick,
  PageHeader,
  FormSection,
} from "../components/controls";
import { useSession } from "../components/Session";
import { href, navigate } from "../router";

type ParamValue = string | number | boolean;
export function defaultsFor(m: Model): Record<string, ParamValue> {
  return Object.fromEntries(
    (m.parameters ?? [])
      .filter((p) => p.default != null)
      .map((p) => [p.name, p.default!]),
  );
}
export function buildJobRequest(
  model: Model,
  values: Record<string, ParamValue>,
  options: {
    name: string;
    region: string;
    priority: string;
    timeoutH: number;
    mode: "run" | "async";
  },
  uploads: Record<string, string> = {},
): InvokeRequest {
  if (!options.name.trim()) throw new Error("Job name is required.");
  if (!Number.isFinite(options.timeoutH) || options.timeoutH <= 0)
    throw new Error("Timeout must be greater than zero.");
  if (options.region && !model.regions.some((r) => r.region === options.region))
    throw new Error("Select a region where this model is deployed.");
  if (!model.modes.includes(options.mode))
    throw new Error("This model does not support the selected execution mode.");
  const input: Record<string, unknown> = {};
  for (const p of model.parameters ?? []) {
    const value = uploads[p.name] ?? values[p.name];
    if (p.required && (value == null || value === ""))
      throw new Error(`${p.label ?? p.name} is required.`);
    if (value == null || value === "") continue;
    if (p.type === "number") {
      if (!Number.isFinite(Number(value)))
        throw new Error(`${p.label ?? p.name} must be a number.`);
      input[p.name] = Number(value);
    } else if (
      p.type === "text" &&
      typeof value === "string" &&
      /^\s*[[{]/.test(value)
    ) {
      try {
        input[p.name] = JSON.parse(value);
      } catch {
        throw new Error(`${p.label ?? p.name} must contain valid JSON.`);
      }
    } else input[p.name] = value;
  }
  return {
    name: options.name.trim(),
    mode: options.mode,
    input,
    region: options.region || undefined,
    priority: options.priority,
    timeout_s: Math.round(options.timeoutH * 3600),
  };
}
export function NewJob({ initialModel }: { initialModel?: string }) {
  const toast = useToast();
  const { fleet } = useSession();
  const models = useAsync(() => api().listModels(), []);
  const [selected, setSelected] = useState(initialModel ?? "");
  const [name, setName] = useState("");
  const [params, setParams] = useState<Record<string, ParamValue>>({});
  const [files, setFiles] = useState<Record<string, File>>({});
  const [region, setRegion] = useState("");
  const [mode, setMode] = useState<"run" | "async">("run");
  const [priority, setPriority] = useState("normal");
  const [timeoutH, setTimeoutH] = useState(2);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const runnable = (models.data ?? []).filter((m) =>
    m.modes.some((v) => v === "run" || v === "async"),
  );
  const model = runnable.find((m) => m.id === selected);
  useEffect(() => {
    if (!model) return;
    setParams(defaultsFor(model));
    setFiles({});
    setError("");
    setMode(model.modes.includes("run") ? "run" : "async");
    setRegion(
      fleet?.fleet_manager && model.modes.includes("run")
        ? ""
        : (model.regions[0]?.region ?? ""),
    );
  }, [model?.id, fleet?.fleet_manager]);
  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setError("");
    if (!model) {
      setError("Choose a model or the container-run job class.");
      return;
    }
    const options = { name, region, mode, priority, timeoutH };
    try {
      buildJobRequest(model, params, options);
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    setBusy(true);
    try {
      const uploads: Record<string, string> = {};
      for (const [key, file] of Object.entries(files)) {
        toast(`Uploading ${file.name}`);
        uploads[key] = await api().uploadArtifact(file, region || undefined);
      }
      const request = buildJobRequest(model, params, options, uploads);
      const r = await api().invoke(model.id, request);
      const op = "operation" in r ? r.operation : r;
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
  return (
    <>
      <PageHeader
        title="Create job"
        description="Run a model or your own container as a background job."
      />
      {!runnable.length ? (
        <Empty>
          <h2>No job models available</h2>
          <p>Create a job class or ask an administrator to enable one.</p>
          <Button href={href("/models")}>View models</Button>
        </Empty>
      ) : (
        <form className="editor-grid" onSubmit={submit}>
          <div className="editor-main">
            <FormSection
              title="General"
              description="Name the run and choose its configuration."
            >
              <Field label="Job name">
                <Input
                  value={name}
                  onChange={setName}
                  placeholder="my-job"
                  controlProps={{ required: true }}
                />
              </Field>
              <Field
                label="Model"
                help="Choose container-run to run your own image."
              >
                <Pick
                  value={selected}
                  onChange={setSelected}
                  placeholder="Select a model"
                  filterable
                  options={runnable.map((m) => ({
                    value: m.id,
                    label:
                      m.id === "container-run"
                        ? "Your own container · container-run"
                        : m.name,
                  }))}
                />
              </Field>
              {model?.description && (
                <p className="help">{model.description}</p>
              )}
              {model &&
                model.modes.includes("run") &&
                model.modes.includes("async") && (
                  <Field label="Execution mode">
                    <Pick
                      value={mode}
                      onChange={(v) => {
                        setMode(v as typeof mode);
                        if (v === "async" && !region)
                          setRegion(model.regions[0]?.region ?? "");
                      }}
                      options={[
                        { value: "run", label: "Container job" },
                        { value: "async", label: "Asynchronous inference" },
                      ]}
                    />
                  </Field>
                )}
            </FormSection>
            {model && (
              <FormSection
                title={
                  model.id === "container-run"
                    ? "Container and resources"
                    : "Input parameters"
                }
                description={
                  model.id === "container-run"
                    ? "Set the image, shell command and resources for this run."
                    : "Inputs accepted by this model."
                }
              >
                {model.parameters?.length ? (
                  model.parameters.map((p) => (
                    <ParamInput
                      key={`${model.id}:${p.name}`}
                      p={p}
                      value={params[p.name]}
                      onChange={(v) =>
                        setParams((s) => ({ ...s, [p.name]: v }))
                      }
                      onFile={(file) =>
                        setFiles((s) => {
                          const out = { ...s };
                          if (file) out[p.name] = file;
                          else delete out[p.name];
                          return out;
                        })
                      }
                    />
                  ))
                ) : (
                  <p className="muted">This model has no input parameters.</p>
                )}
              </FormSection>
            )}
            <FormSection
              title="Scheduling"
              description="Choose where and how long the job can run."
            >
              <div className="form-grid">
                <Field label="Region">
                  <Pick
                    value={region}
                    onChange={setRegion}
                    placeholder="Select a model first"
                    disabled={!model}
                    options={[
                      ...(fleet?.fleet_manager && mode === "run"
                        ? [{ value: "", label: "Automatic placement" }]
                        : []),
                      ...(model?.regions ?? []).map((r) => ({
                        value: r.region,
                        label: r.region,
                      })),
                    ]}
                  />
                </Field>
                <Field label="Priority">
                  <Pick
                    value={priority}
                    onChange={setPriority}
                    options={["low", "normal", "high"]}
                  />
                </Field>
              </div>
              <Field
                label="Timeout (hours)"
                help="The job stops if it exceeds this duration."
              >
                <Input
                  type="number"
                  value={timeoutH}
                  onChange={(v) => setTimeoutH(Number(v))}
                  controlProps={{ min: 0.01, step: "any", required: true }}
                />
              </Field>
            </FormSection>
            {error && <ErrorBox msg={error} />}
            <div className="form-actions">
              <Button href={href("/jobs")} disabled={busy}>
                Cancel
              </Button>
              <Button primary type="submit" loading={busy}>
                Create job
              </Button>
            </div>
          </div>
          <aside className="editor-summary">
            <h2>Job summary</h2>
            <dl className="kv">
              <dt>Name</dt>
              <dd>{name || "—"}</dd>
              <dt>Model</dt>
              <dd>{model?.name ?? "—"}</dd>
              <dt>Mode</dt>
              <dd>{mode === "run" ? "Container job" : "Async inference"}</dd>
              <dt>Region</dt>
              <dd>{region || (model ? "Automatic" : "—")}</dd>
              <dt>GPU</dt>
              <dd>{model?.gpu ?? "—"}</dd>
              <dt>Priority</dt>
              <dd>{priority}</dd>
              <dt>Timeout</dt>
              <dd>{fmt.secs(timeoutH * 3600)}</dd>
            </dl>
            {model && (
              <div className="summary-note">
                <span className="help">Pricing</span>
                <strong>{model.price}</strong>
              </div>
            )}
            <p>Track progress, attempts and results on the job page.</p>
          </aside>
        </form>
      )}
    </>
  );
}
function ParamInput({
  p,
  value,
  onChange,
  onFile,
}: {
  p: ModelParam;
  value: ParamValue | undefined;
  onChange: (v: ParamValue) => void;
  onFile: (f: File | null) => void;
}) {
  const label = (p.label ?? p.name) + (p.required ? " *" : "");
  if (p.type === "select")
    return (
      <Field label={label} help={p.help}>
        <Pick
          value={String(value ?? "")}
          onChange={onChange}
          options={p.options ?? []}
          placeholder="Select an option"
        />
      </Field>
    );
  if (p.type === "boolean")
    return (
      <Checkbox
        checked={Boolean(value)}
        onUpdate={onChange}
        content={p.label ?? p.name}
      />
    );
  if (p.type === "text")
    return (
      <Field label={label} help={p.help}>
        <Area value={String(value ?? "")} onChange={onChange} rows={4} />
      </Field>
    );
  if (p.type === "file")
    return (
      <Field
        label={label}
        help={p.help ?? "Uploaded securely before the job is submitted."}
      >
        <input
          className="input"
          type="file"
          onChange={(e) => {
            const file = e.target.files?.[0] ?? null;
            onFile(file);
            onChange(file?.name ?? "");
          }}
        />
      </Field>
    );
  return (
    <Field label={label} help={p.help}>
      <Input
        value={String(value ?? "")}
        type={p.type === "number" ? "number" : "text"}
        onChange={onChange}
        controlProps={{
          required: p.required,
          step: p.type === "number" ? "any" : undefined,
        }}
      />
    </Field>
  );
}
