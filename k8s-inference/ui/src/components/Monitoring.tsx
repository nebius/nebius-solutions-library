import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ArrowsRotateRight, ArrowDownToLine } from "@gravity-ui/icons";
import { api } from "../api/client";
import type { MetricPanel } from "../api/types";
import { Button, Empty, ErrorBox, Icon, Loading, fmt, useAsync } from "./ui";

const RANGES = [
  ["15m", "Last 15 minutes"],
  ["1h", "Last hour"],
  ["6h", "Last 6 hours"],
  ["24h", "Last 24 hours"],
  ["7d", "Last 7 days"],
];
const number = (value: number, unit: string) =>
  unit === "bytes"
    ? fmt.bytes(value)
    : `${value >= 100 ? Math.round(value).toLocaleString() : value.toFixed(value < 1 ? 2 : 1)}${unit === "%" ? "%" : ""}`;
const clock = (t: number) =>
  new Date(t * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });

function Chart({
  panel,
  compact = false,
}: {
  panel: MetricPanel;
  compact?: boolean;
}) {
  const series = panel.series[0]?.points ?? [];
  const data = series.map(([time, value]) => ({ time, value }));
  const longRange =
    series.length > 1 && series[series.length - 1][0] - series[0][0] >= 86400;
  const last = [...series].reverse().find(([, value]) => value != null)?.[1];
  const unit = panel.unit === "%" || panel.unit === "bytes" ? "" : panel.unit;
  const id = "metric-" + panel.id;
  return (
    <section className="card chart-card" aria-label={panel.title}>
      <div className="chart-heading">
        <h3>{panel.title}</h3>
        <span className="small muted">
          {panel.unit === "bytes" ? "Memory" : panel.unit}
        </span>
      </div>
      <div className="metric-value">
        {last == null ? "—" : number(last, panel.unit)} <small>{unit}</small>
      </div>
      {panel.unavailable || !series.some(([, v]) => v != null) ? (
        <div className="chart-empty">
          <span>
            {panel.unavailable
              ? "Temporarily unavailable"
              : "No data in this time range"}
          </span>
          {!compact && (
            <small>
              {panel.id.startsWith("gpu")
                ? "GPU series appear when a GPU workload is running."
                : "Metrics appear after the resource starts reporting."}
            </small>
          )}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={compact ? 135 : 180}>
          <AreaChart
            data={data}
            margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
          >
            <defs>
              <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#5547ea" stopOpacity={0.18} />
                <stop offset="100%" stopColor="#5547ea" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke="#edf0f5" />
            <XAxis
              dataKey="time"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(time) =>
                longRange
                  ? new Date(time * 1000).toLocaleDateString([], {
                      month: "short",
                      day: "numeric",
                    })
                  : clock(time)
              }
              tick={{ fill: "#69778c", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              minTickGap={50}
            />
            <YAxis
              tickFormatter={(v) => number(v, panel.unit)}
              tick={{ fill: "#69778c", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              width={52}
              domain={[0, "auto"]}
            />
            <Tooltip
              labelFormatter={(t) =>
                new Date(Number(t) * 1000).toLocaleString()
              }
              formatter={(v) => [
                v == null ? "—" : `${number(Number(v), panel.unit)} ${unit}`,
                panel.title,
              ]}
              contentStyle={{
                borderRadius: 8,
                borderColor: "#e0e4eb",
                fontSize: 12,
              }}
            />
            <Area
              dataKey="value"
              stroke="#5547ea"
              strokeWidth={2}
              fill={`url(#${id})`}
              isAnimationActive={false}
              connectNulls={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

export default function Monitoring({
  kind,
  id,
  region,
  view = "metrics",
  compact = false,
  end,
}: {
  kind: "endpoints" | "operations";
  id: string;
  region?: string;
  view?: "metrics" | "logs";
  compact?: boolean;
  end?: number;
}) {
  const [range, setRange] = useState(end ? "24h" : "1h");
  const [live, setLive] = useState(!end);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search), 350);
    return () => clearTimeout(timer);
  }, [search]);
  return view === "metrics" ? (
    <Metrics
      {...{ kind, id, region, range, setRange, live, setLive, compact, end }}
    />
  ) : (
    <Logs
      {...{
        kind,
        id,
        region,
        range,
        setRange,
        live,
        setLive,
        search,
        setSearch,
        query,
        end,
      }}
    />
  );
}

type Controls = {
  range: string;
  setRange: (v: string) => void;
  live: boolean;
  setLive: (v: boolean) => void;
  reload: () => void;
  loading: boolean;
  end?: number;
};
function Controls({
  range,
  setRange,
  live,
  setLive,
  reload,
  loading,
  end,
}: Controls) {
  return (
    <>
      <select
        className="select"
        aria-label="Monitoring time range"
        value={range}
        onChange={(e) => setRange(e.target.value)}
      >
        {RANGES.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
      {!end && (
        <label className="row small">
          <input
            type="checkbox"
            checked={live}
            onChange={(e) => setLive(e.target.checked)}
          />
          Auto-refresh
        </label>
      )}
      <Button view="outlined" size="l" loading={loading} onClick={reload}>
        <Icon data={ArrowsRotateRight} size={16} /> Refresh
      </Button>
    </>
  );
}
type Props = Omit<Controls, "reload" | "loading"> & {
  kind: "endpoints" | "operations";
  id: string;
  region?: string;
  compact?: boolean;
};
function Metrics({ kind, id, region, compact, ...controls }: Props) {
  const metrics = useAsync(
    () => api().metrics(kind, id, controls.range, region, controls.end),
    [kind, id, region, controls.range, controls.end],
    { every: controls.live ? 30000 : undefined },
  );
  const panels = compact
    ? metrics.data?.panels
        .filter((p) =>
          (kind === "endpoints"
            ? ["requests", "latency"]
            : ["cpu", "gpu"]
          ).includes(p.id),
        )
        .slice(0, 2)
    : metrics.data?.panels;
  return (
    <div>
      <div className="monitor-toolbar">
        {compact && <h2 style={{ marginRight: "auto" }}>Monitoring</h2>}
        <Controls
          {...controls}
          reload={metrics.reload}
          loading={metrics.loading}
        />
        <span className="refresh-label">
          {metrics.data && `Updated ${clock(metrics.data.end)}`}
        </span>
      </div>
      {metrics.error && <ErrorBox msg={metrics.error} retry={metrics.reload} />}{" "}
      {!metrics.data && !metrics.error && <Loading />}
      {metrics.data && (
        <div className="chart-grid">
          {panels?.map((p) => (
            <Chart key={p.id} panel={p} compact={compact} />
          ))}
        </div>
      )}
      <p className="monitor-note">
        {controls.end
          ? `Time range ends at ${new Date(controls.end * 1000).toLocaleString()}.`
          : "Live metrics · refreshed every 30 seconds when enabled."}{" "}
        {region && `Region: ${region}.`} Unreported metrics are shown as
        unavailable.
      </p>
    </div>
  );
}
function Logs({
  kind,
  id,
  region,
  search,
  setSearch,
  query,
  ...controls
}: Props & { search: string; setSearch: (v: string) => void; query: string }) {
  const logs = useAsync(
    () => api().logs(kind, id, controls.range, query, region, controls.end),
    [kind, id, region, controls.range, query, controls.end],
    { every: controls.live ? 10000 : undefined },
  );
  const lines = useMemo(
    () => [...(logs.data?.lines ?? [])].reverse(),
    [logs.data],
  );
  function download() {
    const text = lines
      .map(
        (l) =>
          `${new Date(Number(l.timestamp) / 1e6).toISOString()} ${l.pod}/${l.container} ${l.line}`,
      )
      .join("\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${id}.log`;
    anchor.click();
    URL.revokeObjectURL(url);
  }
  return (
    <div>
      <div className="monitor-toolbar">
        <input
          className="input"
          aria-label="Search log messages"
          placeholder="Search log messages"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <Controls {...controls} reload={logs.reload} loading={logs.loading} />
        <Button
          view="outlined"
          size="l"
          disabled={!lines.length}
          onClick={download}
        >
          <Icon data={ArrowDownToLine} size={16} /> Download
        </Button>
      </div>
      {logs.error && <ErrorBox msg={logs.error} retry={logs.reload} />}{" "}
      {!logs.data && !logs.error && <Loading />}
      {logs.data && (
        <>
          <div className="log-meta">
            <span>{lines.length} log entries</span>
            <span>{logs.data.region}</span>
            {logs.data.truncated && (
              <span>
                Showing the latest entries. Narrow the time range or search to
                see more.
              </span>
            )}
          </div>
          {lines.length ? (
            <div
              className="log-view"
              role="log"
              aria-label="Resource logs"
              aria-live="off"
            >
              {lines.map((l, i) => (
                <div className="log-line" key={l.timestamp + "-" + i}>
                  <time
                    title={new Date(Number(l.timestamp) / 1e6).toISOString()}
                  >
                    {new Date(Number(l.timestamp) / 1e6).toLocaleTimeString()}
                  </time>
                  <span
                    className="log-source truncate"
                    title={`${l.pod} / ${l.container}`}
                  >
                    {l.container || l.pod}
                  </span>
                  <code>{l.line}</code>
                </div>
              ))}
            </div>
          ) : (
            <Empty>
              {query
                ? "No log messages match this search."
                : "No logs in this time range. Try a wider range after the resource starts."}
            </Empty>
          )}
        </>
      )}
      <p className="monitor-note">
        {controls.end
          ? "Showing logs around the end of this job."
          : "Logs refresh every 10 seconds when enabled."}{" "}
        Search matches literal text.
      </p>
    </div>
  );
}
