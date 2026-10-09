// Loaded only by VITE_UI_PREVIEW builds. All data and edits stay in this browser tab.
import samples from "./preview-data.json";
import type {
  ApiKey,
  Endpoint,
  Model,
  ModelSpec,
  Operation,
  MetricPanel,
} from "./types";

const models = samples.models as unknown as Model[];
const endpoints = samples.endpoints as unknown as Endpoint[];
const operations = samples.operations as unknown as Operation[];
const keys = samples.keys as unknown as ApiKey[];
const now = Date.now() / 1000;
// Keep the sample timestamps useful whenever the preview is started.
const offset = now - new Date(operations[0].created_at).getTime() / 1000 - 3600;
const shift = (value?: string) =>
  value
    ? new Date(new Date(value).getTime() + offset * 1000).toISOString()
    : undefined;
for (const endpoint of endpoints)
  endpoint.created_at = shift(endpoint.created_at);
for (const operation of operations) {
  operation.created_at = shift(operation.created_at)!;
  operation.started_at = shift(operation.started_at);
  operation.ended_at = shift(operation.ended_at);
  for (const attempt of operation.attempts ?? []) {
    attempt.started_at = shift(attempt.started_at);
    attempt.ended_at = shift(attempt.ended_at);
  }
}
for (const key of keys) {
  key.created_at = shift(key.created_at)!;
  key.expires_at = shift(key.expires_at);
}

function resource<T>(items: T[], predicate: (item: T) => boolean): T {
  const item = items.find(predicate);
  if (!item)
    throw new Error(
      "This sample resource was removed. Reload to reset the preview.",
    );
  return item;
}
function metrics(path: string, query: URLSearchParams) {
  const durations: Record<string, number> = {
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
  };
  const end = Number(query.get("end")) || Date.now() / 1000;
  const duration = durations[query.get("range") ?? "1h"] ?? 3600;
  const isEndpoint = path.includes("/endpoints/");
  const definitions: [string, string, string, number][] = isEndpoint
    ? [
        ["requests", "Request rate", "req/s", 24],
        ["latency", "Response latency · p95", "ms", 145],
        ["errors", "Server errors", "req/s", 0.02],
        ["replicas", "Ready replicas", "replicas", 2],
        ["concurrency", "Concurrent requests", "requests", 12],
      ]
    : [];
  definitions.push(
    ["cpu", "CPU usage", "cores", 4.4],
    ["memory", "Memory usage", "bytes", 22 * 1024 ** 3],
    ["gpu", "GPU utilization", "%", 72],
    ["gpu_memory", "GPU memory", "bytes", 38 * 1024 ** 3],
  );
  const id = path.split("/")[3];
  const endpoint = endpoints.find(
    (e) =>
      e.id === id && (!query.get("region") || e.region === query.get("region")),
  );
  const idle = endpoint?.status === "scaled-to-zero";
  const panels: MetricPanel[] = definitions.map(([id, title, unit, value]) => ({
    id,
    title,
    unit,
    unavailable: false,
    series:
      endpoint?.gpu === "CPU" && id.startsWith("gpu")
        ? []
        : [
            {
              name: title,
              points: Array.from(
                { length: 61 },
                (_, index) =>
                  [
                    end - duration + (index * duration) / 60,
                    idle
                      ? 0
                      : id === "replicas"
                        ? index < 15
                          ? 1
                          : 2
                        : Number(
                            (
                              value *
                              (0.83 +
                                0.18 * Math.sin(index / 4) +
                                0.07 * Math.cos(index / 2))
                            ).toFixed(2),
                          ),
                  ] as [number, number],
              ),
            },
          ],
  }));
  return {
    region: query.get("region") ?? "eu-north1",
    start: end - duration,
    end,
    step: duration / 60,
    panels,
  };
}
function logs(path: string, query: URLSearchParams) {
  const end = Number(query.get("end")) || Date.now() / 1000;
  const job = path.includes("/operations/");
  const id = path.split("/")[3];
  const lines = Array.from({ length: 36 }, (_, index) => ({
    timestamp: String(Math.round((end - index * 12) * 1e9)),
    pod: job ? `${id}-worker` : `${id}-predictor-00012-worker`,
    container: "main",
    line: job
      ? `INFO  step=${1200 - index * 10} loss=${(0.34 + index * 0.006).toFixed(3)} ${index % 8 === 0 ? "checkpoint saved to /work/checkpoint" : "training batch completed"}`
      : `INFO  request completed · duration=${125 + index * 3}ms · tokens=${64 + index}`,
  })).filter((line) => line.line.includes(query.get("search") ?? ""));
  return {
    region: query.get("region") ?? "eu-north1",
    start: end - 3600,
    end,
    lines,
    truncated: false,
  };
}
function saveModel(spec: ModelSpec) {
  const isJob = spec.kind === "job";
  const model: Model = {
    id: spec.id,
    name: spec.display_name || spec.id,
    description: spec.description,
    modes: isJob ? ["run"] : ["sync", "async"],
    default_mode: isJob ? "run" : "sync",
    gpu: spec.gpu?.count
      ? `${spec.gpu.count}x ${spec.gpu.classes?.[0]?.toUpperCase()}`
      : "CPU",
    price: "",
    regions: (spec.regions?.length ? spec.regions : samples.fleet.regions).map(
      (region) => ({ region, status: "deploying" }),
    ),
    image: spec.image,
    protocol: spec.protocol,
    managed_by: "api",
    spec,
  };
  const index = models.findIndex((m) => m.id === model.id);
  if (index < 0) models.push(model);
  else models[index] = model;
  if (!isJob)
    for (const region of model.regions) {
      const endpoint: Endpoint = {
        id: spec.id,
        name: model.name,
        model: spec.id,
        region: region.region,
        url: `https://api.example.com/v1/models/${spec.id}:invoke`,
        status: "deploying",
        replicas_ready: 0,
        min_replicas: spec.scaling?.min ?? 0,
        max_replicas: spec.scaling?.max ?? 1,
        scale_to_zero_after_s: spec.scaling?.idle_s ?? 120,
        target_concurrency:
          spec.scaling?.metric === "concurrency_utilization"
            ? (spec.scaling.container_concurrency ?? 1)
            : (spec.scaling?.target ?? 4),
        scaling: spec.scaling,
        timeout_s: spec.timeout_s ?? 600,
        gpu: model.gpu,
        image: spec.image,
        cpu: spec.resources?.cpu,
        memory: spec.resources?.memory,
        created_at: new Date().toISOString(),
        managed_by: "api",
        protocol: spec.protocol,
      };
      const existing = endpoints.findIndex(
        (e) => e.id === endpoint.id && e.region === endpoint.region,
      );
      if (existing < 0) endpoints.push(endpoint);
      else endpoints[existing] = endpoint;
    }
  return { id: model.id, model };
}

export async function previewRequest(
  rawPath: string,
  init: RequestInit = {},
): Promise<unknown> {
  const url = new URL(rawPath, "http://preview.invalid");
  const path = decodeURIComponent(url.pathname);
  const method = init.method ?? "GET";
  const body = init.body ? JSON.parse(String(init.body)) : {};
  if (path.endsWith("/metrics")) return metrics(path, url.searchParams);
  if (path.endsWith("/logs")) return logs(path, url.searchParams);
  if (path === "/v1/fleet") return samples.fleet;
  if (path === "/v1/keys/me") return keys[0];
  if (path === "/v1/keys") {
    if (method === "GET") return keys;
    const key: ApiKey = {
      ...keys[0],
      id: body.alias,
      alias: body.alias,
      budget: body.budget,
      spend: 0,
      role: "user",
      key: "preview-key-only",
    };
    keys.push(key);
    return key;
  }
  if (path.startsWith("/v1/keys/") && method === "DELETE") {
    const index = keys.findIndex((key) => key.alias === path.split("/")[3]);
    if (index >= 0) keys.splice(index, 1);
    return;
  }
  if (path.endsWith(":invoke")) {
    if (body.mode === "sync")
      return {
        result: {
          choices: [
            {
              message: {
                role: "assistant",
                content:
                  "Hello! This response is sample data in the local UI preview.",
              },
            },
          ],
        },
      };
    const operation: Operation = {
      id: `job-preview-${crypto.randomUUID().slice(0, 8)}`,
      name: body.name,
      model: path.split("/")[3].split(":")[0],
      mode: body.mode ?? "run",
      status: "QUEUED",
      region: body.region || "eu-north1",
      created_at: new Date().toISOString(),
      priority: body.priority,
      timeout_s: body.timeout_s,
      input: body.input,
      attempts: [],
      cost: 0,
    };
    operations.unshift(operation);
    return operation;
  }
  if (path === "/v1/models") return method === "GET" ? models : saveModel(body);
  if (path.startsWith("/v1/models/")) {
    const id = path.split("/")[3];
    if (method === "PUT") return saveModel(body);
    if (method === "DELETE") {
      for (let index = models.length - 1; index >= 0; index--)
        if (models[index].id === id) models.splice(index, 1);
      for (let index = endpoints.length - 1; index >= 0; index--)
        if (endpoints[index].model === id) endpoints.splice(index, 1);
      return;
    }
    return resource(models, (m) => m.id === id);
  }
  if (path === "/v1/endpoints") return endpoints;
  if (path.startsWith("/v1/endpoints/")) {
    const id = path.split("/")[3];
    const endpoint = resource(
      endpoints,
      (e) =>
        e.id === id &&
        (!url.searchParams.get("region") ||
          e.region === url.searchParams.get("region")),
    );
    if (method === "PATCH") {
      const model = models.find((item) => item.id === endpoint.model);
      const scaling = {
        ...model?.spec?.scaling,
        ...endpoint.scaling,
        ...body.scaling,
      };
      if (model?.spec) {
        model.spec.scaling = scaling;
        if (body.timeout_s !== undefined) model.spec.timeout_s = body.timeout_s;
      }
      for (const item of endpoints.filter((e) => e.model === endpoint.model)) {
        Object.assign(item, body, {
          scaling,
          min_replicas: scaling.min ?? item.min_replicas,
          max_replicas: scaling.max ?? item.max_replicas,
          target_concurrency:
            scaling.metric === "concurrency_utilization"
              ? (scaling.container_concurrency ?? 1)
              : (scaling.target ?? item.target_concurrency),
          scale_to_zero_after_s: scaling.idle_s ?? item.scale_to_zero_after_s,
        });
      }
    }
    return endpoint;
  }
  if (path === "/v1/operations") return operations;
  if (path.endsWith("/result"))
    return {
      operation_id: path.split("/")[3],
      status: "SUCCEEDED",
      artifacts: [
        {
          name: "evaluation.json",
          size_bytes: 20480,
          url: "data:application/json,%7B%22sample%22%3Atrue%2C%22accuracy%22%3A0.94%7D",
        },
        {
          name: "checkpoint/model.safetensors",
          size_bytes: 2147483648,
          url: "data:text/plain,This%20is%20a%20sample%20artifact%20for%20UI%20review.",
        },
      ],
    };
  if (path.startsWith("/v1/operations/")) {
    const [id, action] = path.split("/")[3].split(":");
    const operation = resource(operations, (o) => o.id === id);
    if (action === "cancel") {
      operation.status = "CANCELLED";
      operation.ended_at = new Date().toISOString();
    }
    if (action === "resume") {
      const resumed: Operation = {
        ...operation,
        id: `job-preview-${crypto.randomUUID().slice(0, 8)}`,
        status: "QUEUED",
        ended_at: undefined,
        started_at: undefined,
        created_at: new Date().toISOString(),
      };
      operations.unshift(resumed);
      return resumed;
    }
    return operation;
  }
  throw new Error("This action is unavailable in the local UI preview.");
}
