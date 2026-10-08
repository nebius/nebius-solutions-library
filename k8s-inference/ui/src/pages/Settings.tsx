import { useState } from "react";
import { DEFAULT_API_BASE, probeApi, settings } from "../api/client";
import { config } from "../config";
import { Field, useToast } from "../components/ui";

export function Settings({ onChange }: { onChange: () => void }) {
  const [base, setBase] = useState(settings.apiBase);
  const [probe, setProbe] = useState<string | null>(null);
  const toast = useToast();
  async function save() {
    settings.apiBase = base.trim().replace(/\/$/, "");
    const p = await probeApi(); setProbe(`${p.reachable ? "reachable" : "unreachable"}: ${p.detail}`);
    toast("Settings saved"); onChange();
  }
  return (
    <>
      <div className="page-head"><div><h1>Settings</h1><p>Where this console talks to. The key is stored only in this browser.</p></div></div>
      <div className="card" style={{ maxWidth: 640 }}><div className="card__body form">
        <Field label="Customer API URL" help={`Default ${DEFAULT_API_BASE} (same-origin proxy to the customer API; the public URL is ${config().publicApiUrl || "not configured"}).`}><input className="input mono" value={base} onChange={(e) => setBase(e.target.value)} /></Field>
        <Field label="API key"><input className="input mono" value={settings.key ? "..." + settings.key.slice(-8) : "none"} readOnly /></Field>
        {probe && <div className="banner banner--info">API {probe}</div>}
        <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn btn--primary" onClick={save}>Save and test</button></div>
      </div></div>
    </>
  );
}
