import { useState } from "react";
import { probeApi, settings } from "../api/client";
import { Button, Field } from "../components/ui";

export function Login({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState("");
  const [base, setBase] = useState(settings.apiBase);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    settings.apiBase = base.trim().replace(/\/$/, "");
    settings.key = key.trim();
    const p = await probeApi();
    if (!p.reachable) {
      settings.key = "";
      setMsg(
        `API not reachable or key rejected (${p.detail}). Check the key and the API URL.`,
      );
      setBusy(false);
      return;
    }
    setBusy(false);
    onDone();
  }

  return (
    <div className="login">
      <div className="card">
        <div className="card__body">
          <div className="login__brand">
            <h1>Nebius Serverless</h1>
          </div>
          <p>
            Sign in with your tenant API key to manage endpoints, run jobs, and
            follow their progress.
          </p>
          <form className="form" onSubmit={submit}>
            <Field label="API key">
              <input
                className="input mono"
                type="password"
                placeholder="sk-..."
                value={key}
                onChange={(e) => setKey(e.target.value)}
                autoFocus
                required
              />
            </Field>
            <Field label="API URL">
              <input
                className="input mono"
                value={base}
                onChange={(e) => setBase(e.target.value)}
              />
            </Field>
            {msg && <div className="banner banner--warn">{msg}</div>}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <Button
                view="action"
                size="l"
                disabled={!key}
                loading={busy}
                type="submit"
              >
                Sign in
              </Button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
