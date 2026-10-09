import { useState } from "react";
import { api } from "../api/client";
import type { Model, ModelSpec } from "../api/types";
import { Badge, Empty, ErrorBox, Field, Loading, Modal, useAsync, useToast } from "../components/ui";
import { href } from "../router";

function actionFor(m: Model) {
  return m.default_mode === "run" ? { label: "Run job", to: `/jobs/new?model=${m.id}` } : { label: "Open endpoint", to: `/endpoints/${m.id.replace(/\./g, "-")}` };
}

// The form: a container and a few knobs, the same fields as the Nebius Serverless AI endpoint form
// (image, command, port, environment, GPU, scale) plus the fleet's regions. POST/PUT /v1/models.
const EMPTY: ModelSpec = { id: "", kind: "endpoint", image: "", command: "", args: [], env: {}, port: 8000, protocol: "openai",
  health_path: "", served_model: "", gpu: { count: 1, classes: [] }, scaling: { min: 0, max: 1, target: 4 }, regions: [],
  cpu: "4", memory: "16Gi", disk_gi: 50 };

function lines(v: string): string[] { return v.split("\n").map((s) => s.trim()).filter(Boolean); }
function envOf(v: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const l of lines(v)) { const i = l.indexOf("="); if (i > 0) out[l.slice(0, i).trim()] = l.slice(i + 1).trim(); }
  return out;
}

export function ModelForm({ initial, onDone, onClose }: { initial?: ModelSpec; onDone: () => void; onClose: () => void }) {
  const toast = useToast();
  const editing = !!initial;
  const [f, setF] = useState<ModelSpec>(initial ?? EMPTY);
  const [argsText, setArgsText] = useState((initial?.args ?? []).join("\n"));
  const [envText, setEnvText] = useState(Object.entries(initial?.env ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"));
  const [classesText, setClassesText] = useState((initial?.gpu?.classes ?? []).join(", "));
  const [regionsText, setRegionsText] = useState((initial?.regions ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const set = (p: Partial<ModelSpec>) => setF({ ...f, ...p });
  const isJob = f.kind === "job";

  async function submit() {
    const classes = classesText.split(",").map((s) => s.trim()).filter(Boolean);
    const regions = regionsText.split(",").map((s) => s.trim()).filter(Boolean);
    const spec: ModelSpec = { ...f, args: lines(argsText), env: envOf(envText), gpu: { count: Number(f.gpu?.count ?? 1), classes }, regions: regions.length ? regions : undefined };
    if (!spec.health_path) delete spec.health_path;
    if (!spec.served_model) delete spec.served_model;
    if (!spec.command) delete spec.command;
    if (!isJob) { delete spec.cpu; delete spec.memory; delete spec.disk_gi; } else { delete spec.port; delete spec.protocol; delete spec.scaling; delete spec.health_path; delete spec.served_model; }
    setBusy(true);
    try {
      if (editing) await api().updateModel(spec.id, spec); else await api().createModel(spec);
      toast(editing ? "Model updated" : (isJob ? "Job class created" : "Endpoint created; it scales up on the first call"));
      onDone();
    } catch (e) { toast((e as Error).message, true); } finally { setBusy(false); }
  }

  return (
    <Modal title={editing ? `Edit model ${f.id}` : "New model from a container"} onClose={onClose} width={720}>
      <div className="form-grid">
        <Field label="Kind" help="Endpoint: always reachable, scales to zero. Job class: runs on request with a work volume and checkpoints.">
          <select className="input" value={f.kind} disabled={editing} onChange={(e) => set({ kind: e.target.value as ModelSpec["kind"] })}><option value="endpoint">Endpoint</option><option value="job">Job class</option></select>
        </Field>
        <Field label="Name" help="Lowercase letters, digits and dashes. The model id in the API and in LiteLLM."><input className="input" value={f.id} disabled={editing} onChange={(e) => set({ id: e.target.value })} placeholder="my-llm" /></Field>
        <Field label="Image path" help="Any registry, e.g. vllm/vllm-openai:v0.11.0 or nvcr.io/nim/...; pulled through the fleet's image cache."><input className="input mono" value={f.image} onChange={(e) => set({ image: e.target.value })} /></Field>
        <Field label="Entrypoint command" help="Optional. A shell line (run by /bin/sh -c). Leave empty to use the image's entrypoint."><input className="input mono" value={typeof f.command === "string" ? f.command : (f.command ?? []).join(" ")} onChange={(e) => set({ command: e.target.value })} /></Field>
        <Field label="Arguments" help="One per line, e.g. --model and <org>/<model> on two lines."><textarea className="input mono" rows={3} value={argsText} onChange={(e) => setArgsText(e.target.value)} /></Field>
        <Field label="Environment variables" help="KEY=value, one per line."><textarea className="input mono" rows={3} value={envText} onChange={(e) => setEnvText(e.target.value)} /></Field>
        {!isJob && <>
          <Field label="Container port"><input className="input" type="number" value={f.port ?? 8000} onChange={(e) => set({ port: Number(e.target.value) })} /></Field>
          <Field label="Protocol" help="openai: /v1/chat/completions and LiteLLM model group; http: plain requests; websocket; grpc.">
            <select className="input" value={f.protocol} onChange={(e) => set({ protocol: e.target.value as ModelSpec["protocol"] })}>{["openai", "http", "websocket", "grpc"].map((p) => <option key={p}>{p}</option>)}</select>
          </Field>
          <Field label="Health path" help="Optional readiness probe path, e.g. /health."><input className="input mono" value={f.health_path ?? ""} onChange={(e) => set({ health_path: e.target.value })} /></Field>
          <Field label="Served model name" help="openai: the model name the server expects in requests (filled in for callers that omit it)."><input className="input mono" value={f.served_model ?? ""} onChange={(e) => set({ served_model: e.target.value })} /></Field>
          <Field label="Replicas" help="Minimum 0 = scale to zero; maximum caps the scale-out; target = concurrent requests per replica.">
            <div className="row"><input className="input" type="number" value={f.scaling?.min ?? 0} onChange={(e) => set({ scaling: { ...f.scaling, min: Number(e.target.value) } })} /><input className="input" type="number" value={f.scaling?.max ?? 1} onChange={(e) => set({ scaling: { ...f.scaling, max: Number(e.target.value) } })} /><input className="input" type="number" value={f.scaling?.target ?? 4} onChange={(e) => set({ scaling: { ...f.scaling, target: Number(e.target.value) } })} /></div>
          </Field>
        </>}
        {isJob && <>
          <Field label="CPU / memory per run"><div className="row"><input className="input" value={f.cpu ?? "4"} onChange={(e) => set({ cpu: e.target.value })} /><input className="input" value={f.memory ?? "16Gi"} onChange={(e) => set({ memory: e.target.value })} /></div></Field>
          <Field label="Work volume (GiB)" help="Per run; survives spot interruptions (checkpoints under /work/checkpoint)."><input className="input" type="number" value={f.disk_gi ?? 50} onChange={(e) => set({ disk_gi: Number(e.target.value) })} /></Field>
        </>}
        <Field label="GPUs" help="Count per replica (0 = CPU only) and the GPU classes it may run on, preferred first (h100, h200, b300, l40s, rtx-pro-6000).">
          <div className="row"><input className="input" type="number" value={f.gpu?.count ?? 1} onChange={(e) => set({ gpu: { ...f.gpu, count: Number(e.target.value) } })} /><input className="input mono" value={classesText} onChange={(e) => setClassesText(e.target.value)} placeholder="h100, l40s" /></div>
        </Field>
        <Field label="Regions" help="Comma-separated; empty = every region with a pool of a listed class."><input className="input mono" value={regionsText} onChange={(e) => setRegionsText(e.target.value)} placeholder="eu-north1, eu-west2" /></Field>
      </div>
      <div className="actions" style={{ marginTop: 16 }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn--primary" disabled={busy || !f.id || !f.image} onClick={submit}>{busy ? "Saving" : (editing ? "Save" : "Create")}</button>
      </div>
    </Modal>
  );
}

export function Models() {
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(() => api().listModels(), []);
  const [form, setForm] = useState<null | { initial?: ModelSpec }>(null);
  async function remove(m: Model) {
    if (!confirm(`Delete ${m.id}? Its endpoints are removed from every region.`)) return;
    try { await api().deleteModel(m.id); toast("Model deleted"); reload(); } catch (e) { toast((e as Error).message, true); }
  }
  return (
    <>
      <div className="page-head">
        <div><h1>Models</h1><p>A model is a container plus a few knobs: an endpoint (always reachable, scales to zero) or a job class (runs on request with checkpoints). Define one here or with <span className="mono">POST /v1/models</span>; it is kept in the fleet database and deployed in every region it names. Creating, editing and deleting needs an admin key.</p></div>
        <div className="actions"><a className="btn" href={href("/jobs/new")}>Run a one-off container</a><button className="btn btn--primary" onClick={() => setForm({})}>New model</button></div>
      </div>
      {form && <ModelForm initial={form.initial} onClose={() => setForm(null)} onDone={() => { setForm(null); reload(); }} />}
      {error && <ErrorBox msg={error} retry={reload} />}
      {loading && !data && <Loading />}
      {data && data.length === 0 && <Empty>No models yet. Click "New model" to deploy a container.</Empty>}
      {data && (
        <div className="grid grid--cards">
          {data.map((m) => {
            const a = actionFor(m);
            const editable = m.managed_by === "api";
            return (
              <div className="card model-card" key={m.id}>
                <div className="model-card__top">
                  <div><h3>{m.name}</h3><div className="small muted mono">{m.id}</div></div>
                  <span className="badge badge--violet badge--plain">{m.default_mode}</span>
                </div>
                <p>{m.description}</p>
                <dl className="kv">
                  <dt>Modes</dt><dd>{m.modes.map((x) => <span className="chip" key={x}>{x}</span>)}</dd>
                  <dt>GPU</dt><dd>{m.gpu}</dd>
                  {m.protocol && <><dt>Protocol</dt><dd className="mono">{m.protocol}</dd></>}
                  {m.image && <><dt>Image</dt><dd className="mono small">{m.image}</dd></>}
                  {m.cold_start_s != null && <><dt>Cold start</dt><dd>{m.cold_start_s} s (measured)</dd></>}
                  <dt>Regions</dt><dd>{m.regions.map((r) => <span key={r.region} style={{ marginRight: 8 }}><Badge status={r.status}>{r.region}</Badge></span>)}</dd>
                  <dt>Managed by</dt><dd>{m.managed_by === "api" ? "this console / the API" : "the solution (built-in class)"}</dd>
                </dl>
                <div className="model-card__foot">
                  <span className="price">{m.price}</span>
                  <div className="actions">
                    {editable && m.spec && <button className="btn btn--sm" onClick={() => setForm({ initial: m.spec })}>Edit</button>}
                    {editable && <button className="btn btn--sm btn--danger" onClick={() => remove(m)}>Delete</button>}
                    {m.modes.includes("run") && m.default_mode !== "run" && <a className="btn btn--sm" href={href(`/jobs/new?model=${m.id}`)}>Run job</a>}
                    <a className="btn btn--primary btn--sm" href={href(a.to)}>{a.label}</a>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
