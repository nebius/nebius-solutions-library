import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { Model, ModelParam } from "../api/types";
import { Field, Loading, fmt, useAsync, useToast } from "../components/ui";
import { navigate } from "../router";

const STEPS = ["Model", "Parameters", "Scheduling", "Review"];
const REGIONS = ["eu-north1", "eu-south1"];

type Form = {
  name: string; source: "catalog" | "image"; model: string; image: string; command: string; args: string; env: string;
  params: Record<string, string | number | boolean>;
  region: string; priority: "low" | "normal" | "high"; timeoutH: number;
  mode: "run" | "async";
};

function defaultsFor(m?: Model): Record<string, string | number | boolean> {
  const o: Record<string, string | number | boolean> = {};
  m?.parameters?.forEach((p) => { if (p.default != null) o[p.name] = p.default; });
  return o;
}

export function NewJob({ initialModel }: { initialModel?: string }) {
  const toast = useToast();
  const models = useAsync(() => api().listModels(), []);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState<Record<string, File>>({});
  const [f, setF] = useState<Form>({
    name: "", source: initialModel ? "catalog" : "catalog", model: initialModel ?? "", image: "", command: "", args: "", env: "",
    params: {}, region: "eu-north1", priority: "normal", timeoutH: 2, mode: "run",
  });
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((s) => ({ ...s, [k]: v }));
  const model = useMemo(() => models.data?.find((m) => m.id === f.model), [models.data, f.model]);

  // when the model changes: parameter defaults, name suggestion, mode, a region where it is deployed
  useEffect(() => {
    if (!model) return;
    setF((s) => ({
      ...s, params: defaultsFor(model),
      name: s.name || `${model.id}-${new Date().toISOString().slice(5, 16).replace(/[-:T]/g, "")}`,
      mode: model.modes.includes("run") ? "run" : "async",
      region: model.regions.find((r) => r.status === "ready")?.region ?? model.regions[0]?.region ?? s.region,
    }));
  }, [model]);

  const errors: string[] = [];
  if (!f.name.trim()) errors.push("Name is required");
  if (f.source === "catalog" && !f.model) errors.push("Choose a model");
  if (f.source === "image" && !f.image.trim()) errors.push("Image is required");
  model?.parameters?.forEach((p) => { if (p.required && p.type !== "file" && (f.params[p.name] == null || f.params[p.name] === "")) errors.push(`${p.label ?? p.name} is required`); });
  if (model && !model.regions.some((r) => r.region === f.region)) errors.push(`${model.name} is not deployed in ${f.region}`);

  async function submit() {
    setBusy(true);
    try {
      const input: Record<string, unknown> = { ...f.params };
      // text parameters that hold JSON (e.g. OpenAI "messages") are sent as JSON, not as a string
      for (const [k, v] of Object.entries(input)) if (typeof v === "string" && /^\s*[[{]/.test(v)) { try { input[k] = JSON.parse(v); } catch { /* keep string */ } }
      for (const [k, file] of Object.entries(files)) { toast(`Uploading ${file.name}`); input[k] = await api().uploadArtifact(file); }
      if (f.source === "image") Object.assign(input, { image: f.image, command: f.command, args: f.args.split(/\s+/).filter(Boolean), env: Object.fromEntries(f.env.split("\n").filter((l) => l.includes("=")).map((l) => l.split("=", 2) as [string, string])) });
      // `input` must only hold parameters the model's WorkflowTemplate declares; the API rejects unknown keys
      const r = await api().invoke(f.source === "catalog" ? f.model : "custom", { name: f.name.trim(), mode: f.mode, input, region: f.region, priority: f.priority, timeout_s: Math.round(f.timeoutH * 3600) });
      const op = "operation" in r ? r.operation : r;
      toast(`Job ${op.id} submitted`);
      navigate(`/jobs/${op.id}`);
    } catch (e) { toast((e as Error).message, true); } finally { setBusy(false); }
  }

  if (!models.data) return <Loading />;

  return (
    <>
      <div className="page-head"><div><h1>New job</h1><p>Pick a model, set its parameters, choose region and priority. Pool, image and checkpointing come from the model&apos;s run class.</p></div></div>
      <div className="wizard">
        <nav className="steps">{STEPS.map((s, i) => <a key={s} href="#" className={i === step ? "active" : ""} onClick={(e) => { e.preventDefault(); setStep(i); }}><span className="n">{i + 1}</span>{s}</a>)}</nav>
        <div className="card">
          {step === 0 && (
            <div className="section">
              <h2>Name and source</h2>
              <Field label="Name" help="Lowercase letters, digits and dashes."><input className="input" value={f.name} onChange={(e) => set("name", e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} placeholder="gromacs-1ns" /></Field>
              <div className="radio-cards">
                <div className={`radio-card ${f.source === "catalog" ? "active" : ""}`} onClick={() => set("source", "catalog")}><strong>From the model catalog</strong><small>Image, protocol and resources come from the onboarded model.</small></div>
                <div className={`radio-card ${f.source === "image" ? "active" : ""}`} onClick={() => set("source", "image")}><strong>Bring your own image</strong><small>Any container; you set command, args and environment.</small></div>
              </div>
              {f.source === "catalog" ? (
                <Field label="Model">
                  <select className="select" value={f.model} onChange={(e) => set("model", e.target.value)}>
                    <option value="">Choose a model</option>
                    {models.data.map((m) => <option key={m.id} value={m.id}>{m.name} — {m.modes.join("/")} — {m.gpu}</option>)}
                  </select>
                </Field>
              ) : (
                <>
                  <Field label="Image" help="Registry credentials: pull secret from your tenant namespace (NGC via `ngc`)."><input className="input mono" placeholder="cr.eu-north1.nebius.cloud/<project>/my-image:tag" value={f.image} onChange={(e) => set("image", e.target.value)} /></Field>
                  <div className="row"><Field label="Command"><input className="input mono" value={f.command} onChange={(e) => set("command", e.target.value)} placeholder="python" /></Field><Field label="Arguments"><input className="input mono" value={f.args} onChange={(e) => set("args", e.target.value)} placeholder="train.py --epochs 3" /></Field></div>
                  <Field label="Environment variables" help="KEY=value per line. Secrets go through the tenant SecretStash."><textarea className="textarea" value={f.env} onChange={(e) => set("env", e.target.value)} /></Field>
                </>
              )}
              {model && (
                <Field label="Execution mode">
                  <div className="radio-cards">
                    {model.modes.filter((m) => m !== "sync").map((m) => <div key={m} className={`radio-card ${f.mode === m ? "active" : ""}`} onClick={() => set("mode", m as Form["mode"])}><strong>{m}</strong><small>{m === "run" ? "Minutes to weeks; Kueue queue, checkpoint volume, survives preemption." : "Seconds to minutes on a warm endpoint; no held connection."}</small></div>)}
                  </div>
                </Field>
              )}
            </div>
          )}
          {step === 1 && (
            <div className="section">
              <h2>Parameters</h2>
              {!model?.parameters?.length && <span className="muted">{f.source === "image" ? "Custom images take their parameters from command, args and environment." : "This model has no input parameters."}</span>}
              {model?.parameters?.map((p) => <ParamInput key={p.name} p={p} value={f.params[p.name]} onChange={(v) => setF((s) => ({ ...s, params: { ...s.params, [p.name]: v } }))} onFile={(file) => setFiles((s) => { const n = { ...s }; if (file) n[p.name] = file; else delete n[p.name]; return n; })} />)}
            </div>
          )}
          {step === 2 && (
            <div className="section">
              <h2>Scheduling</h2>
              <div className="row">
                <Field label="Region"><select className="select" value={f.region} onChange={(e) => set("region", e.target.value)}>{REGIONS.map((r) => <option key={r} value={r}>{r}{model && !model.regions.some((x) => x.region === r) ? " (model not deployed here)" : ""}</option>)}</select></Field>
                <Field label="Priority" help="Kueue workload priority; high preempts low when the pool is full."><select className="select" value={f.priority} onChange={(e) => set("priority", e.target.value as Form["priority"])}><option value="low">low</option><option value="normal">normal</option><option value="high">high</option></select></Field>
              </div>
              <Field label="Job timeout (hours)" help="Hard stop; the operation becomes FAILED. Retries after preemption and checkpoint resume are part of the model's run class."><input className="input" type="number" step="0.5" value={f.timeoutH} onChange={(e) => set("timeoutH", Number(e.target.value))} /></Field>
            </div>
          )}
          {step === 3 && (
            <div className="section">
              <h2>Review</h2>
              {errors.length > 0 && <div className="banner banner--warn"><ul style={{ margin: 0, paddingLeft: 18 }}>{errors.map((e) => <li key={e}>{e}</li>)}</ul></div>}
              <pre className="out">{JSON.stringify({ name: f.name, model: f.source === "catalog" ? f.model : { image: f.image, command: f.command, args: f.args }, mode: f.mode, input: f.params, region: f.region, priority: f.priority, timeout_s: f.timeoutH * 3600 }, null, 2)}</pre>
            </div>
          )}
          <div className="section"><div className="row" style={{ justifyContent: "space-between" }}>
            <button className="btn" disabled={step === 0} onClick={() => setStep(step - 1)}>Back</button>
            {step < STEPS.length - 1 ? <button className="btn btn--primary" onClick={() => setStep(step + 1)}>Next</button> : <button className="btn btn--primary" disabled={busy || errors.length > 0} onClick={submit}>{busy && <span className="spin" />} Create job</button>}
          </div></div>
        </div>
        <div className="card summary">
          <div className="card__head"><h2>Summary</h2></div>
          <div className="card__body">
            <dl className="kv">
              <dt>Name</dt><dd>{f.name || "-"}</dd>
              <dt>Model</dt><dd>{f.source === "catalog" ? model?.name ?? "-" : f.image || "custom image"}</dd>
              <dt>Mode</dt><dd>{f.mode}</dd>
              <dt>Region</dt><dd>{f.region}</dd>
              <dt>GPU</dt><dd>{model?.gpu ?? "-"}</dd>
              <dt>Priority</dt><dd>{f.priority}</dd>
              <dt>Timeout</dt><dd>{fmt.secs(f.timeoutH * 3600)}</dd>
            </dl>
            <div className="total"><span>Price</span><span>{model?.price ?? "-"}</span></div>
            <div className="small muted">Billed per second of GPU time while running (run class) or per call (async).</div>
          </div>
        </div>
      </div>
    </>
  );
}

function ParamInput({ p, value, onChange, onFile }: { p: ModelParam; value: string | number | boolean | undefined; onChange: (v: string | number | boolean) => void; onFile: (f: File | null) => void }) {
  const label = (p.label ?? p.name) + (p.required ? " *" : "");
  if (p.type === "select") return <Field label={label} help={p.help}><select className="select" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>{p.options?.map((o) => <option key={o} value={o}>{o}</option>)}</select></Field>;
  if (p.type === "boolean") return <Field label={label} help={p.help}><label className="row"><input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} /> enabled</label></Field>;
  if (p.type === "text") return <Field label={label} help={p.help}><textarea className="textarea" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} /></Field>;
  if (p.type === "file") return <Field label={label} help={p.help ?? "Uploaded to the tenant bucket via a presigned URL (POST /v1/artifacts/uploads)."}><input className="input" type="file" onChange={(e) => { const file = e.target.files?.[0] ?? null; onFile(file); onChange(file ? file.name : ""); }} /></Field>;
  if (p.type === "number") return <Field label={label} help={p.help}><input className="input" type="number" value={value == null ? "" : Number(value)} onChange={(e) => onChange(Number(e.target.value))} /></Field>;
  return <Field label={label} help={p.help}><input className="input" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} /></Field>;
}
