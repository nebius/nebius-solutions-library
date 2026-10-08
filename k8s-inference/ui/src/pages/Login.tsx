import { useState } from "react";
import { probeApi, settings } from "../api/client";

export function Login({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState("");
  const [base, setBase] = useState(settings.apiBase);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setMsg(null);
    settings.apiBase = base.trim().replace(/\/$/, "");
    settings.key = key.trim();
    const p = await probeApi();
    if (!p.reachable) { settings.key = ""; setMsg(`API not reachable or key rejected (${p.detail}). Check the key and the API URL.`); setBusy(false); return; }
    setBusy(false);
    onDone();
  }

  return (
    <div className="login">
      <div className="card"><div className="card__body">
        <div className="login__brand"><span className="brand__mark">N</span><div><h1 style={{ fontSize: 18 }}>Nebius Serverless 2.0</h1><div className="small muted">Scientific AI Platform console</div></div></div>
        <p>Sign in with your tenant API key (a LiteLLM virtual key issued at onboarding). It stays in this browser and is sent as <code>Authorization: Bearer</code> to the customer API.</p>
        <form className="form" onSubmit={submit}>
          <div className="field"><label>API key</label><input className="input mono" placeholder="sk-..." value={key} onChange={(e) => setKey(e.target.value)} autoFocus /></div>
          <div className="field"><label>API URL</label><input className="input mono" value={base} onChange={(e) => setBase(e.target.value)} /></div>
          {msg && <div className="banner banner--warn">{msg}</div>}
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn btn--primary" disabled={busy || !key} type="submit">{busy ? <span className="spin" /> : null} Sign in</button>
          </div>
        </form>
      </div></div>
    </div>
  );
}
