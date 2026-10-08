import { useEffect, useState } from "react";
import { api } from "../api/client";
import { grafanaUrl } from "../config";
import type { Endpoint } from "../api/types";
import { Badge, Empty, ErrorBox, Field, Loading, Modal, Stat, fmt, useAsync, useToast } from "../components/ui";
import { href, navigate } from "../router";

export function Endpoints() {
  const { data, error, loading, reload } = useAsync(() => api().listEndpoints(), [], { every: 8000 });
  return (
    <>
      <div className="page-head">
        <div><h1>Endpoints</h1><p>Sync and async models served by KServe on Knative behind LiteLLM. Scale to zero when idle; billed per second per ready replica.</p></div>
        <div className="actions"><button className="btn" onClick={reload}>Refresh</button><span className="small muted">Deployed from the catalog in the repo (Argo CD)</span></div>
      </div>
      {error && <ErrorBox msg={error} retry={reload} />}
      {loading && !data && <Loading />}
      {data && (
        <div className="card tbl-wrap">
          <table className="tbl">
            <thead><tr><th>Name</th><th>Model</th><th>Status</th><th>Replicas</th><th>Scale to zero</th><th>In flight</th><th>GPU</th><th>Placement</th><th>Region</th><th>Managed by</th><th>Last cold start</th></tr></thead>
            <tbody>
              {data.length === 0 && <tr><td colSpan={11}><Empty>No endpoints.</Empty></td></tr>}
              {data.map((e) => (
                <tr key={e.id} className="row--link" onClick={() => navigate(`/endpoints/${e.id}`)}>
                  <td><a className="strong" href={href(`/endpoints/${e.id}`)}>{e.name}</a><div className="small muted mono">{e.id}</div></td>
                  <td>{e.model}</td>
                  <td><Badge status={e.status} /></td>
                  <td>{e.replicas_ready} <span className="muted">/ {e.min_replicas}–{e.max_replicas}</span></td>
                  <td>{fmt.secs(e.scale_to_zero_after_s)}</td>
                  <td>{e.in_flight ?? 0}</td>
                  <td>{e.gpu ?? "-"}</td>
                  <td>{e.placement?.toLowerCase() ?? "-"}</td>
                  <td>{e.region}</td>
                  <td>{e.managed_by ? <span className={`badge badge--plain ${e.managed_by === "api" ? "badge--violet" : ""}`}>{e.managed_by}</span> : "-"}</td>
                  <td>{e.last_cold_start_s != null ? `${e.last_cold_start_s} s` : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function sampleBody(e: Endpoint) {
  if (e.protocol === "openai-chat") return { messages: [{ role: "user", content: "Say hello in one sentence." }], max_tokens: 64 };
  if (e.model.includes("stt")) return { audio_url: "s3://tenant/samples/hello.wav", language: "en" };
  return { input: {} };
}

export function EndpointDetail({ id }: { id: string }) {
  const toast = useToast();
  const { data: e, error, loading, reload } = useAsync(() => api().getEndpoint(id), [id], { every: 6000 });
  const [body, setBody] = useState("");
  const [out, setOut] = useState<{ status: number; ms: number; body: unknown } | null>(null);
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState(false);
  const [scale, setScale] = useState({ min: 0, max: 1, idle: 300, conc: 4 });
  useEffect(() => { if (e && !body) setBody(JSON.stringify(sampleBody(e), null, 2)); if (e) setScale({ min: e.min_replicas, max: e.max_replicas, idle: e.scale_to_zero_after_s, conc: e.target_concurrency ?? 4 }); }, [e]); // eslint-disable-line

  async function tryIt() {
    if (!e) return;
    setBusy(true); setOut(null);
    try {
      let parsed: unknown; try { parsed = JSON.parse(body); } catch { throw new Error("Body is not valid JSON"); }
      const t0 = performance.now();
      const r = await api().invoke(e.model, { mode: "sync", input: parsed as Record<string, unknown>, region: e.region, timeout_s: 600 });
      setOut({ status: 200, ms: Math.round(performance.now() - t0), body: "result" in r ? r.result : r });
    } catch (err) { setOut({ status: 0, ms: 0, body: (err as Error).message }); } finally { setBusy(false); }
  }
  async function saveScale() {
    try { await api().updateEndpoint(id, { min_replicas: scale.min, max_replicas: scale.max, scale_to_zero_after_s: scale.idle, target_concurrency: scale.conc }); toast("Scaling updated"); setEdit(false); reload(); } catch (err) { toast((err as Error).message, true); }
  }
  const curl = e ? `curl -sS ${e.url} \\\n  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \\\n  -d '{"mode":"sync","input":${body.replace(/\n\s*/g, " ")}}'` : "";

  if (error) return <ErrorBox msg={error} retry={reload} />;
  if (loading && !e) return <Loading />;
  if (!e) return null;
  return (
    <>
      <div className="page-head">
        <div><div className="row"><h1>{e.name}</h1><Badge status={e.status} /></div><p className="mono small">{e.id} · {e.model} · {e.region} · {e.gpu}</p></div>
        <div className="actions">
          {grafanaUrl(e.region) && <a className="btn" href={`${grafanaUrl(e.region)}/d/knative-serving?var-service=${e.name}`} target="_blank" rel="noreferrer">Metrics</a>}
          {grafanaUrl(e.region) && <a className="btn" href={`${grafanaUrl(e.region)}/explore?left=${encodeURIComponent(JSON.stringify({ datasource: "Loki", queries: [{ expr: `{app="${e.name}-predictor"}` }] }))}`} target="_blank" rel="noreferrer">Logs</a>}
          <button className="btn" onClick={() => setEdit(true)}>Edit scaling</button>
        </div>
      </div>
      {e.managed_by === "git" && <div className="banner banner--info" style={{ marginBottom: 16 }}><span>Managed from the repo (Argo CD): scaling changes made here are reverted on the next sync. Change <code>catalog/models/*.yaml</code> instead.</span></div>}
      <div className="stats" style={{ marginBottom: 16 }}>
        <Stat label="Ready replicas" value={`${e.replicas_ready} / ${e.max_replicas}`} />
        <Stat label="Min replicas" value={e.min_replicas} />
        <Stat label="Scale to zero after" value={fmt.secs(e.scale_to_zero_after_s)} />
        <Stat label="Target concurrency" value={e.target_concurrency ?? "-"} />
        <Stat label="In flight" value={e.in_flight ?? 0} />
        <Stat label="Last cold start" value={e.last_cold_start_s != null ? `${e.last_cold_start_s} s` : "-"} />
        <Stat label="Placement" value={e.placement?.toLowerCase() ?? "-"} />
        <Stat label="Created" value={fmt.ago(e.created_at)} />
      </div>
      <div className="grid grid--2">
        <div className="card">
          <div className="card__head"><h2>Try it</h2><span className="small muted">POST {new URL(e.url).pathname}</span></div>
          <div className="card__body form">
            <Field label="URL"><input className="input mono" readOnly value={e.url} /></Field>
            <Field label="Request body (JSON)"><textarea className="textarea" rows={7} value={body} onChange={(ev) => setBody(ev.target.value)} /></Field>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small muted">Sent with your key as Bearer. A scaled-to-zero endpoint takes a cold start (~{e.last_cold_start_s ?? 60} s).</span>
              <button className="btn btn--primary" disabled={busy} onClick={tryIt}>{busy && <span className="spin" />} Send request</button>
            </div>
            {out && <><div className={`banner ${out.status >= 200 && out.status < 300 ? "banner--success" : "banner--danger"}`}><span>HTTP {out.status || "error"}</span><span>{out.ms} ms</span></div><pre className="out">{typeof out.body === "string" ? out.body : JSON.stringify(out.body, null, 2)}</pre></>}
            <details><summary className="small muted">curl</summary><pre className="out">{curl}</pre></details>
          </div>
        </div>
        <div className="grid" style={{ alignContent: "start" }}>
          <div className="card">
            <div className="card__head"><h2>Configuration</h2></div>
            <div className="card__body"><dl className="kv">
              <dt>Model</dt><dd>{e.model}</dd><dt>Protocol</dt><dd className="mono">{e.protocol ?? "http-json"}</dd><dt>GPU</dt><dd>{e.gpu ?? "-"}</dd>
              <dt>Scaling</dt><dd>{e.min_replicas}–{e.max_replicas} replicas, concurrency {e.target_concurrency ?? "-"}, idle {fmt.secs(e.scale_to_zero_after_s)}</dd>
              <dt>Placement</dt><dd>{e.placement ?? "-"}</dd><dt>Managed by</dt><dd>{e.managed_by ?? "-"}</dd><dt>Region</dt><dd>{e.region}</dd><dt>URL</dt><dd className="mono small">{e.url}</dd>
            </dl></div>
          </div>
        </div>
      </div>
      {edit && (
        <Modal title="Edit scaling" onClose={() => setEdit(false)}>
          <div className="form">
            <div className="row"><Field label="Min replicas"><input className="input" type="number" min={0} value={scale.min} onChange={(ev) => setScale({ ...scale, min: Number(ev.target.value) })} /></Field><Field label="Max replicas"><input className="input" type="number" min={1} value={scale.max} onChange={(ev) => setScale({ ...scale, max: Number(ev.target.value) })} /></Field></div>
            <div className="row"><Field label="Scale to zero after (s)" help="0 = never"><input className="input" type="number" min={0} value={scale.idle} onChange={(ev) => setScale({ ...scale, idle: Number(ev.target.value) })} /></Field><Field label="Target concurrency"><input className="input" type="number" min={1} value={scale.conc} onChange={(ev) => setScale({ ...scale, conc: Number(ev.target.value) })} /></Field></div>
            <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn" onClick={() => setEdit(false)}>Cancel</button><button className="btn btn--primary" onClick={saveScale}>Save</button></div>
          </div>
        </Modal>
      )}
    </>
  );
}
