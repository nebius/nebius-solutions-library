import { api } from "../api/client";
import type { FleetPool } from "../api/types";
import { Empty, ErrorBox, Loading, useAsync } from "../components/ui";

const CAPACITY: Record<string, string> = {
  spot: "Spot",
  on_demand: "On demand",
  reserved: "Reserved",
};

export function Fleet() {
  const { data, error, loading, reload } = useAsync(() => api().fleetLive(), [], { every: 30_000 });
  if (error && !data) return <ErrorBox msg={error} retry={reload} />;
  const pools = data?.pools ?? [];
  const regions = [...new Set(pools.map((p) => p.region))].sort();
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Fleet</h1>
          <p>
            The clusters and GPU pools Terraform manages, per region, and what is up right now. Pools scale
            from zero and back; a warm spare node shows as a ready node with no GPU in use.
          </p>
        </div>
      </div>
      {loading && !data && <Loading />}
      {data?.tenant && (
        <div className="card" style={{ marginBottom: 16, padding: "12px 16px" }}>
          <h2 style={{ margin: "0 0 8px" }}>Your tenant: {data.tenant.name}</h2>
          <div className="stats">
            <div className="stat"><div className="small muted">GPU quota per pool</div><div className="stat-value">{data.tenant.gpu_quota ?? "no cap"}</div></div>
            <div className="stat"><div className="small muted">Fair-share weight</div><div className="stat-value">{data.tenant.fair_share_weight ?? 1}</div></div>
            <div className="stat"><div className="small muted">Dedicated pools</div><div className="stat-value">{data.tenant.dedicated_pools?.length ? data.tenant.dedicated_pools.join(", ") : "none"}</div></div>
            {Object.entries(data.tenant.usage ?? {}).map(([region, u]) => (
              <div className="stat" key={region}>
                <div className="small muted">{region}: GPUs in use (admitted / pending)</div>
                <div className="stat-value">
                  {Object.values(u.gpus_by_pool).reduce((a, b) => a + b, 0)} ({u.admitted} / {u.pending})
                </div>
                <div className="small muted">{Object.entries(u.gpus_by_pool).map(([pool, n]) => `${pool}: ${n}`).join(", ") || "idle"}</div>
              </div>
            ))}
          </div>
        </div>
      )}
      {data?.clusters && data.clusters.length > 0 && (
        <div className="card tbl-wrap" style={{ marginBottom: 16 }}>
          <h2 style={{ margin: "12px 16px" }}>Clusters</h2>
          <table className="tbl">
            <thead>
              <tr>
                <th>Cluster</th>
                <th>Region</th>
                <th>Project</th>
                <th>Roles</th>
                <th>Kubernetes</th>
                <th>Nodes ready</th>
                <th>GPU nodes</th>
                <th>API</th>
                <th>Grafana</th>
              </tr>
            </thead>
            <tbody>
              {data.clusters.map((c) => (
                <tr key={c.id}>
                  <td>
                    <strong>{c.id}</strong>
                    {!c.reachable && <div className="small muted">unreachable from the control plane</div>}
                  </td>
                  <td>{c.region}</td>
                  <td className="small">{c.project ?? "—"}</td>
                  <td>{c.roles.join(", ")}</td>
                  <td>{c.kubernetes_version ?? "—"}</td>
                  <td>{c.nodes_ready ?? "—"}</td>
                  <td>{c.gpu_nodes_ready ?? "—"}</td>
                  <td>{c.api_url ? <a href={c.api_url + "/healthz"} target="_blank" rel="noreferrer">API</a> : "—"}</td>
                  <td>{c.grafana_url ? <a href={c.grafana_url} target="_blank" rel="noreferrer">Grafana</a> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && pools.length === 0 && <Empty>No GPU pools in this fleet.</Empty>}
      {regions.map((region) => (
        <div className="card tbl-wrap" key={region} style={{ marginBottom: 16 }}>
          <h2 style={{ margin: "12px 16px" }}>{region}</h2>
          <table className="tbl">
            <thead>
              <tr>
                <th>Pool</th>
                <th>GPU</th>
                <th>Per node</th>
                <th>Capacity</th>
                <th>USD / GPU-hour</th>
                <th>Nodes</th>
                <th>GPUs in use</th>
                <th>Extras</th>
              </tr>
            </thead>
            <tbody>
              {pools
                .filter((p) => p.region === region)
                .map((p: FleetPool) => (
                  <tr key={`${region}/${p.pool}`}>
                    <td>
                      <strong>{p.pool}</strong>
                      <div className="small muted">{p.platform} · {p.preset}</div>
                    </td>
                    <td>{p.gpu_class}</td>
                    <td>{p.gpus_per_node}</td>
                    <td>{CAPACITY[p.capacity] ?? p.capacity}</td>
                    <td>{p.usd_per_gpu_hour != null ? p.usd_per_gpu_hour.toFixed(2) : "—"}</td>
                    <td>
                      {p.nodes_ready == null ? "unknown" : `${p.nodes_ready} ready`}
                      {p.max_nodes != null ? <span className="small muted"> / max {p.max_nodes}</span> : null}
                    </td>
                    <td>
                      {p.gpus_total == null
                        ? "unknown"
                        : `${p.gpus_used ?? "?"} of ${p.gpus_total}`}
                    </td>
                    <td className="small muted">
                      {[
                        p.interconnect && p.interconnect !== "none" ? p.interconnect : null,
                        p.local_nvme ? "local NVMe" : null,
                      ]
                        .filter(Boolean)
                        .join(", ") || "—"}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
