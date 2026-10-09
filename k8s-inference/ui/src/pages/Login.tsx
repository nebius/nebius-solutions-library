import { useState } from "react";
import { Checkbox } from "@gravity-ui/uikit";
import { probeApi, settings } from "../api/client";
import { Button, Input } from "../components/controls";
import { ErrorBox, Field } from "../components/ui";
export function Login({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState("");
  const [base, setBase] = useState(settings.apiBase);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    settings.apiBase = base.trim().replace(/\/$/, "");
    settings.key = key.trim();
    const p = await probeApi();
    if (!p.reachable) {
      settings.key = "";
      setError(`Unable to sign in: ${p.detail}. Check the API URL and key.`);
      setBusy(false);
      return;
    }
    setBusy(false);
    onDone();
  }
  return (
    <div className="login">
      <aside className="login__aside">
        <img src="/nebius.svg" width="156" height="24" alt="Nebius" />
        <div>
          <h2>Serverless AI</h2>
          <p>
            Deploy inference endpoints. Run container jobs. Manage your models
            in one place.
          </p>
        </div>
        <small>Standalone inference fleet console</small>
      </aside>
      <main className="login__main">
        <div className="login__form">
          <h1>Sign in</h1>
          <p>Use an API key issued for your inference fleet.</p>
          <form className="form" onSubmit={submit}>
            <Field label="API key">
              <Input
                value={key}
                onChange={setKey}
                type={show ? "text" : "password"}
                mono
                autoFocus
                autoComplete="off"
                placeholder="sk-…"
                controlProps={{ required: true, spellCheck: false }}
              />
            </Field>
            <Checkbox
              checked={show}
              onUpdate={setShow}
              content="Show API key"
            />
            <Field
              label="API URL"
              help="Use /api for the API served with this application."
            >
              <Input
                value={base}
                onChange={setBase}
                mono
                controlProps={{ required: true, spellCheck: false }}
              />
            </Field>
            {error && <ErrorBox msg={error} />}
            <Button primary type="submit" loading={busy} disabled={!key.trim()}>
              Sign in
            </Button>
            <p className="help">
              Your key is stored in this browser and sent to the API URL above
              with each request.
            </p>
          </form>
        </div>
      </main>
    </div>
  );
}
