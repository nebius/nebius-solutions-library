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
            The GPU pools Terraform manages, per region, and what is up right now. Pools scale from zero
            and back; a warm spare node shows as a ready node with no GPU in use.
          </p>
        </div>
      </div>
      {loading && !data && <Loading />}
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
