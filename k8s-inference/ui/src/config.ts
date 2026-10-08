// Runtime configuration of the console: served by nginx at /config.json from the pod's environment
// (templates/default.conf.template), so one image works for every fleet. Nothing fleet-specific is
// compiled into the bundle. Missing or unreachable config (e.g. `npm run dev`) leaves every link empty.
export type UiConfig = {
  publicApiUrl: string;                 // the customer API as reachable from outside, shown in Settings
  grafanaUrls: Record<string, string>;  // region -> Grafana URL (plus "control" on a dedicated control plane)
};

let current: UiConfig = { publicApiUrl: "", grafanaUrls: {} };

// "eu-north1=https://grafana...,control=https://grafana..." (the API's GRAFANA_URLS format) -> map
function parseUrls(s: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (typeof s !== "string") return out;
  for (const part of s.split(",")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

export async function loadConfig(): Promise<UiConfig> {
  try {
    const r = await fetch("/config.json", { cache: "no-cache" });
    if (r.ok) {
      const j = (await r.json()) as { publicApiUrl?: string; grafanaUrls?: string };
      current = { publicApiUrl: j.publicApiUrl ?? "", grafanaUrls: parseUrls(j.grafanaUrls) };
    }
  } catch {
    /* keep the empty defaults */
  }
  return current;
}

export function config(): UiConfig { return current; }

// Grafana of a region, else the control plane's, else the first one; "" when none is configured.
export function grafanaUrl(region?: string): string {
  const g = current.grafanaUrls;
  if (region && g[region]) return g[region];
  return g.control ?? Object.values(g)[0] ?? "";
}
