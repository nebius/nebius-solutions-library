// Shapes follow services/api/openapi.yaml (customer API) plus the UI-facing list endpoints.

export type Mode = "sync" | "async" | "run";
export type Region = "eu-north1" | "eu-south1" | string;

export interface ModelRegionStatus {
  region: Region;
  status: "ready" | "scaled-to-zero" | "deploying" | "unavailable";
  replicas_ready?: number;
}

export interface Model {
  id: string;                 // e.g. "gromacs", "nemotron-stt", "qwen"
  name: string;
  description?: string;
  modes: Mode[];
  default_mode: Mode;
  gpu: string;                // "1x H100", "1x RTX PRO 6000", "none"
  price: string;              // "$0.05 / call" or "$2.40 / GPU-h"
  price_per_call?: number;
  regions: ModelRegionStatus[];
  image?: string;
  protocol?: string;          // "openai-chat", "http-json", "argo-workflow"
  cold_start_s?: number;
  parameters?: ModelParam[];  // input parameters shown in the wizard
}

export interface ModelParam {
  name: string;
  type: "string" | "number" | "boolean" | "select" | "text" | "file";
  label?: string;
  default?: string | number | boolean;
  options?: string[];
  required?: boolean;
  help?: string;
}

export type OperationStatus =
  | "QUEUED" | "ADMITTED" | "RUNNING" | "PREEMPTED" | "SUCCEEDED" | "FAILED" | "CANCELLED";

export interface Attempt {
  index: number;
  started_at?: string;
  ended_at?: string;
  status: OperationStatus | string;
  node?: string;
  reason?: string;            // e.g. "preempted: spot reclaimed"
  resumed_from_checkpoint?: string;
}

export interface Operation {
  id: string;
  name?: string;
  model: string;
  mode: Mode;
  status: OperationStatus;
  region: Region;
  priority?: "low" | "normal" | "high" | string;
  queue_position?: number;
  created_at: string;
  started_at?: string;
  ended_at?: string;
  timeout_s?: number;
  attempts?: Attempt[];
  logs_url?: string;
  error?: string;
  input?: Record<string, unknown>;
  cost?: number;
}

export interface OperationResult {
  operation_id: string;
  status: OperationStatus;
  result?: unknown;
  artifacts?: { name: string; url: string; size_bytes?: number }[];
}

export interface Endpoint {
  id: string;
  name: string;
  model: string;
  region: Region;
  url: string;
  status: "ready" | "scaled-to-zero" | "deploying" | "error";
  replicas_ready: number;
  min_replicas: number;
  max_replicas: number;
  scale_to_zero_after_s: number;
  target_concurrency?: number;
  in_flight?: number;
  last_cold_start_s?: number;
  placement?: "RESERVED" | "SPOT" | "ANY";
  gpu?: string;
  created_at?: string;
  protocol?: string;          // "openai-chat" | "http-json"
  managed_by?: "git" | "api"; // git = Argo CD owns it (PATCH reverted, DELETE 403)
}

export interface ApiKey {
  id: string;
  key?: string;               // only returned at creation
  key_preview: string;        // "sk-...abcd"
  alias: string;
  budget: number;             // USD
  spend: number;              // USD
  models: string[];           // allow-list
  created_at: string;
  expires_at?: string;
  status: "active" | "exhausted" | "expired";
  role?: "user" | "admin";
  tenant?: string;
}

export interface InvokeRequest {
  name?: string;
  mode?: Mode;
  input?: Record<string, unknown>;
  region?: Region;
  priority?: string;
  timeout_s?: number;
}
