import { api } from "../api/client";
import type { Model } from "../api/types";
import { Badge, Empty, ErrorBox, Loading, useAsync } from "../components/ui";
import { href } from "../router";

function actionFor(m: Model) {
  return m.default_mode === "run" ? { label: "Run job", to: `/jobs/new?model=${m.id}` } : { label: "Open endpoint", to: `/endpoints/${m.id.replace(/\./g, "-")}` };
}

export function Models() {
  const { data, error, loading, reload } = useAsync(() => api().listModels(), []);
  return (
    <>
      <div className="page-head">
        <div><h1>Models</h1><p>Onboarded models from the catalog. Run-class models start as jobs; sync and async models are deployed as endpoints. Bring-your-own-image goes through the same wizard.</p></div>
        <div className="actions"><a className="btn" href={href("/jobs/new")}>Bring your own image</a></div>
      </div>
      {error && <ErrorBox msg={error} retry={reload} />}
      {loading && !data && <Loading />}
      {data && data.length === 0 && <Empty>No models in the catalog yet.</Empty>}
      {data && (
        <div className="grid grid--cards">
          {data.map((m) => {
            const a = actionFor(m);
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
                  {m.cold_start_s != null && <><dt>Cold start</dt><dd>{m.cold_start_s} s (measured)</dd></>}
                  <dt>Regions</dt><dd>{m.regions.map((r) => <span key={r.region} style={{ marginRight: 8 }}><Badge status={r.status}>{r.region}</Badge></span>)}</dd>
                </dl>
                <div className="model-card__foot">
                  <span className="price">{m.price}</span>
                  <div className="actions">
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
