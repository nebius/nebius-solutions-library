// Customer API client. Bearer = LiteLLM virtual key pasted at login.
// Paths follow services/api/openapi.yaml (the API serves the same spec at /openapi.json).
import type {
  ApiKey, Endpoint, InvokeRequest, Model, ModelSpec, Operation, OperationResult,
} from "./types";

const LS = { key: "s2.key", base: "s2.apiBase" };
// Default: same-origin "/api" (nginx in the UI image proxies it to the API service, so no CORS);
// override with VITE_API_BASE at build time or in Settings.
export const DEFAULT_API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";
export function apiHost(): string { const b = settings.apiBase; return b.startsWith("http") ? new URL(b).host : `${location.host}${b}`; }

export const settings = {
  get key(): string { return localStorage.getItem(LS.key) ?? ""; },
  set key(v: string) { v ? localStorage.setItem(LS.key, v) : localStorage.removeItem(LS.key); },
  get apiBase(): string { return localStorage.getItem(LS.base) ?? DEFAULT_API_BASE; },
  set apiBase(v: string) { v && v !== DEFAULT_API_BASE ? localStorage.setItem(LS.base, v) : localStorage.removeItem(LS.base); },
};

// One authenticated GET at login / in Settings to tell "wrong URL" from "wrong key".
export async function probeApi(): Promise<{ reachable: boolean; detail: string }> {
  try {
    const r = await fetch(`${settings.apiBase}/v1/models`, { headers: authHeaders(), signal: AbortSignal.timeout(6000) });
    return { reachable: r.ok, detail: `${r.status} ${r.statusText}` };
  } catch (e) {
    return { reachable: false, detail: (e as Error).message };
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) { super(message); }
}

function authHeaders(extra: Record<string, string> = {}) {
  const h: Record<string, string> = { ...extra };
  if (settings.key) h.Authorization = `Bearer ${settings.key}`;
  return h;
}
function idem() { return crypto.randomUUID(); }

async function http<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = authHeaders({ ...(init.headers as Record<string, string> | undefined) });
  if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (init.method && init.method !== "GET") headers["Idempotency-Key"] = idem();
  const r = await fetch(`${settings.apiBase}${path}`, { ...init, headers });
  const text = await r.text();
  let body: unknown = text;
  try { body = text ? JSON.parse(text) : undefined; } catch { /* keep text */ }
  if (!r.ok) {
    const msg = (body as { error?: { message?: string }; detail?: string; message?: string })?.error?.message
      ?? (body as { detail?: string })?.detail ?? (body as { message?: string })?.message ?? `${r.status} ${r.statusText}`;
    throw new ApiError(r.status, r.status === 403 ? `Not allowed: ${msg} (needs an admin key of the tenant; git-managed endpoints are changed in the repo)` : String(msg), body);
  }
  return body as T;
}
// Accept both bare arrays and {items|models|operations|...: []} envelopes.
function unwrap<T>(b: unknown, ...keys: string[]): T[] {
  if (Array.isArray(b)) return b as T[];
  const o = b as Record<string, unknown>;
  for (const k of [...keys, "items", "data"]) if (Array.isArray(o?.[k])) return o[k] as T[];
  return [];
}

const real = {
  listModels: async () => unwrap<Model>(await http("/v1/models"), "models"),
  getModel: (id: string) => http<Model>(`/v1/models/${encodeURIComponent(id)}`),
  // models defined from a container (admin key): the "New model" form, services/api/models.py
  createModel: (spec: ModelSpec) => http<{ id: string; model: Model }>(`/v1/models`, { method: "POST", body: JSON.stringify(spec) }),
  updateModel: (id: string, spec: ModelSpec) => http<{ id: string; model: Model }>(`/v1/models/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(spec) }),
  deleteModel: (id: string) => http<void>(`/v1/models/${encodeURIComponent(id)}`, { method: "DELETE" }),
  listOperations: async () => unwrap<Operation>(await http("/v1/operations?limit=100"), "operations"),
  getOperation: (id: string) => http<Operation>(`/v1/operations/${encodeURIComponent(id)}`),
  getResult: async (id: string) => {
    try { return await http<OperationResult>(`/v1/operations/${encodeURIComponent(id)}/result`); }
    catch (e) { if (e instanceof ApiError && e.status === 409 && e.body && typeof e.body === "object") return e.body as OperationResult; throw e; }
  },
  cancel: (id: string) => http<Operation>(`/v1/operations/${encodeURIComponent(id)}:cancel`, { method: "POST" }),
  invoke: (model: string, req: InvokeRequest) => {
    // only the documented fields; for run-class models `input` holds the WorkflowTemplate parameters
    const { name, mode, input, region, priority, timeout_s } = req;
    const body: InvokeRequest = { name, mode, region, priority, timeout_s, input };
    return http<Operation | { result: unknown; operation: Operation }>(`/v1/models/${encodeURIComponent(model)}:invoke`, { method: "POST", body: JSON.stringify(body) });
  },
  uploadArtifact: async (file: File): Promise<string> => {
    const u = await http<{ uri: string; url: string; method: string; headers?: Record<string, string> }>(`/v1/artifacts/uploads`, { method: "POST", body: JSON.stringify({ filename: file.name, content_type: file.type || "application/octet-stream" }) });
    const r = await fetch(u.url, { method: u.method || "PUT", headers: { "Content-Type": file.type || "application/octet-stream", ...(u.headers ?? {}) }, body: file });
    if (!r.ok) throw new ApiError(r.status, `upload failed: ${r.status} ${r.statusText}`);
    return u.uri;
  },
  listEndpoints: async () => unwrap<Endpoint>(await http("/v1/endpoints"), "endpoints"),
  getEndpoint: (id: string) => http<Endpoint>(`/v1/endpoints/${encodeURIComponent(id)}`),
  updateEndpoint: (id: string, patch: Partial<Endpoint>) => {
    const { min_replicas, max_replicas, scale_to_zero_after_s, target_concurrency } = patch;
    return http<Endpoint>(`/v1/endpoints/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ min_replicas, max_replicas, scale_to_zero_after_s, target_concurrency }) });
  },
  // endpoints are created and deleted as models (createModel/deleteModel); this patches scaling of an existing one
  listKeys: async () => unwrap<ApiKey>(await http("/v1/keys"), "keys"),
  createKey: (input: { alias: string; budget: number; models: string[]; expires_days?: number }) => http<ApiKey>(`/v1/keys`, { method: "POST", body: JSON.stringify(input) }),
  deleteKey: (alias: string) => http<void>(`/v1/keys/${encodeURIComponent(alias)}`, { method: "DELETE" }),
  keyInfo: () => http<ApiKey>(`/v1/keys/me`),
};

export type Api = typeof real;
export function api(): Api { return real; }

