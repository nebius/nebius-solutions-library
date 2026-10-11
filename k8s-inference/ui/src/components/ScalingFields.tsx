import type { Endpoint, ScalingSpec } from "../api/types";
import { Field } from "./ui";

export const defaultScaling: ScalingSpec = {
  min: 0,
  max: 1,
  metric: "concurrency_utilization",
  target: 100,
  container_concurrency: 1,
  cooldown_s: 10,
  window_s: 30,
  idle_s: 120,
};

export function endpointScaling(endpoint: Endpoint): ScalingSpec {
  return {
    min: endpoint.min_replicas,
    max: endpoint.max_replicas,
    idle_s: endpoint.scale_to_zero_after_s,
    ...endpoint.scaling,
  };
}

export function isRequestRate(scaling: ScalingSpec) {
  return scaling.metric === "requests_per_second";
}

export function scalingMetricLabel(scaling: ScalingSpec) {
  return isRequestRate(scaling)
    ? "Requests per second"
    : "Concurrency utilization";
}

export function scalingThreshold(scaling: ScalingSpec) {
  if (isRequestRate(scaling)) return scaling.target ?? 5;
  return ((scaling.container_concurrency ?? 1) * (scaling.target ?? 100)) / 100;
}

export function scalingValidation(value: ScalingSpec): string | undefined {
  if ((value.min ?? 0) > (value.max ?? 1))
    return "Minimum replicas cannot exceed maximum replicas.";
  if (!isRequestRate(value)) {
    if ((value.target ?? 100) > 100 || (value.target ?? 100) < 1)
      return "Concurrency utilization target must be between 1% and 100%.";
    if ((value.container_concurrency ?? 1) < 1)
      return "Concurrency utilization requires a replica concurrency of at least 1.";
  }
  if ((value.window_s ?? 30) > 300)
    return "Evaluation interval must be between 6 and 300 seconds.";
}

export function ScalingFields({
  value,
  onChange,
  timeout = 600,
  onTimeoutChange,
  disabled = false,
  advancedOpen = false,
  error,
}: {
  value: ScalingSpec;
  onChange: (value: ScalingSpec) => void;
  timeout?: number;
  onTimeoutChange: (value: number) => void;
  disabled?: boolean;
  advancedOpen?: boolean;
  error?: string;
}) {
  const metric = value.metric ?? "concurrency_utilization";
  const rate = isRequestRate(value);
  const set = (patch: Partial<ScalingSpec>) => onChange({ ...value, ...patch });
  const issue = error || scalingValidation(value);
  function number(
    label: string,
    key: keyof ScalingSpec,
    fallback: number,
    min: number,
    max?: number,
    help?: string,
    inactive = false,
  ) {
    return (
      <Field label={label} help={help}>
        <input
          className="input"
          type="number"
          min={min}
          max={max}
          step={1}
          required
          disabled={disabled || inactive}
          value={Number(value[key] ?? fallback)}
          onChange={(e) => set({ [key]: Number(e.target.value) })}
        />
      </Field>
    );
  }
  function changeMetric(next: ScalingSpec["metric"]) {
    onChange({
      ...value,
      metric: next,
      target: next === "requests_per_second" ? 5 : 100,
      container_concurrency:
        next === "requests_per_second"
          ? 0
          : Math.max(1, value.container_concurrency || 1),
      window_s: Math.min(300, value.window_s ?? 30),
    });
  }
  return (
    <div className="scaling-fields form">
      <div className="row">
        {number(
          "Min replicas",
          "min",
          0,
          0,
          undefined,
          "Pod floor per regional deployment. Set 0 to scale to zero.",
        )}
        {number(
          "Max replicas",
          "max",
          1,
          1,
          undefined,
          "Pod ceiling per regional deployment. These bounds apply separately in each region.",
        )}
      </div>
      <Field
        label="Scaling metric"
        help="Choose how demand is measured across replicas. Changing metric resets its target."
      >
        <select
          className="select"
          disabled={disabled}
          value={metric}
          onChange={(e) =>
            changeMetric(e.target.value as ScalingSpec["metric"])
          }
        >
          <option value="concurrency_utilization">
            Concurrency utilization
          </option>
          <option value="requests_per_second">Requests per second</option>
          <option value="cpu_utilization" disabled>
            CPU utilization — unavailable
          </option>
          <option value="memory_utilization" disabled>
            Memory utilization — unavailable
          </option>
        </select>
      </Field>
      <div className="row">
        {number(
          rate ? "Scaling target (requests/s)" : "Scaling target (%)",
          "target",
          rate ? 5 : 100,
          1,
          rate ? undefined : 100,
          rate
            ? "Average requests per second per replica. A target of 5 means 5 requests/s."
            : "Percentage of replica concurrency, averaged across replicas. 200 concurrent slots × 80% = 160 requests per replica.",
        )}
        {number(
          "Replica concurrency",
          "container_concurrency",
          rate ? 0 : 1,
          rate ? 0 : 1,
          1000,
          rate
            ? "Not enforced by requests-per-second scaling. Concurrency is unlimited in this mode."
            : "Maximum simultaneous requests per replica. Excess requests wait for available capacity.",
          rate,
        )}
      </div>
      <div className="inline-note" aria-live="polite">
        Average demand target:{" "}
        <strong>
          {Number(scalingThreshold(value).toFixed(2))}{" "}
          {rate ? "requests/s" : "concurrent requests"} per replica
        </strong>
        .{!rate && <> {100 - (value.target ?? 100)}% concurrency headroom.</>}{" "}
        Replica bounds still apply.
      </div>
      {issue && (
        <p className="field-error" role="alert">
          {issue}
        </p>
      )}
      {rate && (
        <p className="help">
          This metric does not enforce a concurrency limit. Most GPU workloads
          should use concurrency utilization.
        </p>
      )}
      <details className="scaling-advanced" open={advancedOpen || undefined}>
        <summary>Advanced scaling</summary>
        <div className="form">
          <div className="row">
            {number(
              "Cooldown period (seconds)",
              "cooldown_s",
              10,
              0,
              3600,
              "Reduced demand must persist for this period before scaling down. Scale-up can proceed immediately.",
            )}
            {number(
              "Evaluation interval (seconds)",
              "window_s",
              30,
              6,
              300,
              "Metrics averaging window before scaling decisions, 6–300 seconds, default 30. Burst handling can react sooner.",
            )}
          </div>
          <Field
            label="Response grace period (seconds)"
            help="One duration for HTTP request lifetime and the SIGTERM-to-SIGKILL shutdown allowance. Supported here: 1–600 seconds. The app must handle cancellation and SIGTERM to stop or finish its work."
          >
            <input
              className="input"
              type="number"
              min={1}
              max={600}
              step={1}
              required
              disabled={disabled}
              value={timeout}
              onChange={(e) => onTimeoutChange(Number(e.target.value))}
            />
          </Field>
          {number(
            "Idle replica retention (seconds)",
            "idle_s",
            120,
            0,
            3600,
            "Keep the last pod after the decision to scale to zero. Separate from cooldown and the averaging window.",
          )}
          {number(
            "Scaling buffer (extra ready replicas)",
            "buffer",
            0,
            0,
            8,
            "Ready replicas kept above the measured demand while the app serves, so a burst finds a replica at once. Off while the app is at zero (the warm spare node covers that); lowered after the cooldown.",
          )}
          <div className="scaling-unavailable">
            <div className="scaling-unavailable__head">
              <h3>Additional deployment controls</h3>
              <span className="badge">Unavailable</span>
            </div>
            <Field
              label="Shared warm GPU buffer"
              help="Spare GPU capacity shared across compatible apps. Two L40S apps with buffer 1 can share one spare L40S slot. Requires a pool capacity controller; separate from extra app replicas."
            >
              <input
                className="input"
                disabled
                readOnly
                value="Not configured"
              />
            </Field>
            <Field
              label="Load balancing"
              help="Per-app routing selection needs implementation. Min-connections selects fewest in-flight requests; random-choice-2 samples two replicas and chooses the less busy one."
            >
              <select
                className="select"
                disabled
                value="platform"
                onChange={() => {}}
              >
                <option value="platform">Managed by platform</option>
                <option value="round-robin">Round-robin</option>
                <option value="first-available">First-available</option>
                <option value="min-connections">Min-connections</option>
                <option value="random-choice-2">Random-choice-2</option>
              </select>
            </Field>
            <p className="help">
              CPU utilization targets a percentage of allocated CPU; memory
              utilization targets a percentage of allocated RAM, excluding GPU
              memory. Both require at least one replica and autoscaler
              integration.
            </p>
          </div>
        </div>
      </details>
    </div>
  );
}
