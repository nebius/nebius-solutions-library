import { useState } from "react";
import { api } from "../api/client";
import type { Model } from "../api/types";
import { useSession } from "../components/Session";
import {
  Badge,
  Button,
  ErrorBox,
  Loading,
  Modal,
  useAsync,
  useToast,
} from "../components/ui";
import { PageHeader, ResourceName, RowMenu } from "../components/Resource";
import { href, navigate } from "../router";

export function Models() {
  const { admin } = useSession();
  const toast = useToast();
  const models = useAsync(() => api().listModels(), []);
  const [search, setSearch] = useState("");
  const [remove, setRemove] = useState<Model | null>(null);
  const [busy, setBusy] = useState(false);
  async function destroy() {
    if (!remove) return;
    setBusy(true);
    try {
      await api().deleteModel(remove.id);
      toast("Definition deleted");
      setRemove(null);
      models.reload();
    } catch (err) {
      toast((err as Error).message, true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageHeader
        title="Saved definitions"
        count={models.data?.length}
        create={
          admin ? { label: "Create definition", to: "/models/new" } : undefined
        }
      />
      <p className="muted" style={{ marginBottom: 24 }}>
        Reusable containers and settings for your endpoints and jobs.
      </p>
      <div className="toolbar">
        <input
          className="input search"
          aria-label="Search definitions"
          placeholder="Search definitions"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      {models.error && <ErrorBox msg={models.error} retry={models.reload} />}{" "}
      {models.loading && !models.data && <Loading />}
      {models.data && (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Name and ID</th>
                <th>Type</th>
                <th>Hardware</th>
                <th>Regions</th>
                <th></th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {models.data
                .filter((m) =>
                  `${m.id} ${m.name}`
                    .toLowerCase()
                    .includes(search.toLowerCase()),
                )
                .map((m) => (
                  <tr key={m.id}>
                    <td className="name-cell">
                      <ResourceName
                        name={m.name}
                        id={m.id}
                        to={
                          m.modes.includes("run")
                            ? `/jobs/new?model=${m.id}`
                            : `/endpoints?model=${m.id}`
                        }
                      />
                    </td>
                    <td>
                      <Badge status="active">
                        {m.default_mode === "run" ? "Job" : "Endpoint"}
                      </Badge>
                    </td>
                    <td>{m.gpu}</td>
                    <td>
                      {m.regions.map((r) => (
                        <span className="chip" key={r.region}>
                          {r.region}
                        </span>
                      ))}
                    </td>
                    <td>
                      {m.modes.includes("run") ? (
                        <Button
                          view="outlined"
                          size="l"
                          href={href(`/jobs/new?model=${m.id}`)}
                        >
                          Create job
                        </Button>
                      ) : (
                        <Button
                          view="outlined"
                          size="l"
                          href={href(`/endpoints?model=${m.id}`)}
                        >
                          View endpoints
                        </Button>
                      )}
                    </td>
                    <td>
                      <RowMenu
                        actions={[
                          ...(admin && m.managed_by === "api"
                            ? [
                                {
                                  label: "Edit definition",
                                  action: () => navigate(`/models/${m.id}`),
                                },
                                {
                                  label: "Delete definition",
                                  action: () => setRemove(m),
                                },
                              ]
                            : []),
                          {
                            label: "Copy definition ID",
                            action: () =>
                              navigator.clipboard
                                .writeText(m.id)
                                .then(() => toast("Copied")),
                          },
                        ]}
                      />
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
      {remove && (
        <Modal
          title="Delete definition?"
          onClose={() => {
            if (!busy) setRemove(null);
          }}
        >
          <p>
            Delete <strong>{remove.name}</strong>? Its endpoints are removed
            from every region.
          </p>
          <div className="actions">
            <Button disabled={busy} onClick={() => setRemove(null)}>
              Keep definition
            </Button>
            <Button view="outlined-danger" loading={busy} onClick={destroy}>
              Delete definition
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}
