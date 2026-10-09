import { useState } from "react";
import { DEFAULT_API_BASE, probeApi, settings } from "../api/client";
import { config } from "../config";
import { ErrorBox, Field, useToast } from "../components/ui";
import { Button, Input, PageHeader, FormSection } from "../components/controls";
import { useSession } from "../components/Session";
export function Settings({ onChange }: { onChange: () => void }) {
  const { key, fleet } = useSession();
  const [base, setBase] = useState(settings.apiBase);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const toast = useToast();
  async function save(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError("");
    const old = settings.apiBase;
    settings.apiBase = base.trim().replace(/\/$/, "");
    const p = await probeApi();
    if (!p.reachable) {
      settings.apiBase = old;
      setError(
        `Connection failed: ${p.detail}. The previous connection is still active.`,
      );
    } else {
      toast("Connection saved");
      onChange();
    }
    setBusy(false);
  }
  return (
    <>
      <PageHeader
        title="Settings"
        description="Connection and signed-in account."
      />
      <form className="editor-main" style={{ maxWidth: 760 }} onSubmit={save}>
        <FormSection title="API connection">
          <Field label="API URL" help={`Default: ${DEFAULT_API_BASE}`}>
            <Input
              value={base}
              onChange={setBase}
              mono
              controlProps={{ required: true }}
            />
          </Field>
          {config().publicApiUrl && (
            <Field label="Public API URL">
              <Input value={config().publicApiUrl} mono readOnly />
            </Field>
          )}
          {error && <ErrorBox msg={error} />}
          <div className="form-actions">
            <Button primary type="submit" loading={busy}>
              Save and test
            </Button>
          </div>
        </FormSection>
        <FormSection title="Account">
          <dl className="details-kv">
            <dt>Tenant</dt>
            <dd>{key?.tenant || "—"}</dd>
            <dt>API key</dt>
            <dd className="mono">{key?.key_preview || "Hidden"}</dd>
            <dt>Role</dt>
            <dd>{key?.role === "admin" ? "Administrator" : "Member"}</dd>
            <dt>Regions</dt>
            <dd>{fleet?.regions.join(", ") || "Unavailable"}</dd>
          </dl>
          <p className="help">
            The API key and connection URL are stored in this browser. Sign out
            to remove the key.
          </p>
        </FormSection>
      </form>
    </>
  );
}
