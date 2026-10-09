// Shapes follow services/api/openapi.yaml (customer API) plus the UI-facing list endpoints.

export type Mode = "sync" | "async" | "run";
export type Region = "eu-north1" | "eu-south1" | string;

export interface ScalingSpec {
  min?: number;
  max?: number;
  metric?:
    | "concurrency_utilization"
    | "requests_per_second"
    | "concurrency"
    | "rps";
  target?: number;
  utilization_percent?: number;
  buffer?: number; // ready replicas kept above demand while serving (0-8)
  container_concurrency?: number;
  cooldown_s?: number;
  window_s?: number;
  idle_s?: number;
}

export interface ModelRegionStatus {
  region: Region;
  status: "ready" | "scaled-to-zero" | "deploying" | "unavailable";
  replicas_ready?: number | null;
}

export interface Model {
  id: string; // e.g. "container-run", "llm-example"
  name: string;
  description?: string;
  modes: Mode[];
  default_mode: Mode;
  gpu: string; // "1x H100", "1x RTX PRO 6000", "none"
  price: string; // "$0.05 / call" or "$2.40 / GPU-h"
  price_per_call?: number;
  regions: ModelRegionStatus[];
  image?: string;
  protocol?: string; // "openai-chat", "http-json", "kubernetes-job"
  cold_start_s?: number;
  parameters?: ModelParam[]; // input parameters shown in the wizard
  managed_by?: "api" | "terraform" | "catalog"; // api: defined through POST /v1/models (editable here)
  spec?: ModelSpec; // the definition behind an api-managed model, for the edit form
}

// A model as the "New model" form and terraform.tfvars `models` define it (services/api/models.py).
// Reserved for later: `images` (per GPU class), `routing`.
export interface ModelSpec {
  id: string;
  kind: "endpoint" | "job";
  image: string;
  images?: Record<string, string>;
  command?: string | string[];
  args?: string[];
  env?: Record<string, string>;
  port?: number;
  protocol?: "http" | "openai" | "websocket" | "grpc";
  path?: string;
  health_path?: string;
  served_model?: string;
  gpu?: { count?: number; classes?: string[] };
  resources?: { cpu?: string; memory?: string };
  scaling?: ScalingSpec;
  timeout_s?: number;
  shm_gib?: number;
  pull_secret?: string;
  weights?: {
    path?: string;
    mount_path?: string;
    env?: Record<string, string>;
  };
  regions?: string[];
  display_name?: string;
  description?: string;
  cpu?: string;
  memory?: string;
  disk_gi?: number;
  grace_seconds?: number;
  scratch?: "network" | "local-nvme";
  parameters?: ModelParam[];
  routing?: Record<string, unknown>;
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
  | "QUEUED"
  | "ADMITTED"
  | "RUNNING"
  | "PREEMPTED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export interface Attempt {
  index: number;
  started_at?: string;
  ended_at?: string;
  status: OperationStatus | string;
  node?: string;
  gpu_class?: string; // the GPU class the attempt ran on (per-class images)
  reason?: string; // e.g. "preempted: spot reclaimed"
  resumed_from_checkpoint?: string;
}

export interface Operation {
  id: string;
  name?: string;
  model: string;
  mode: Mode;
  status: OperationStatus;
  region: Region;
  gpu_class?: string; // run classes with per-GPU-class images: the class chosen at submission
  nodes?: number; // multi-node runs (JobSet): one pod per node
  interconnect?: string; // multi-node runs: required | preferred | none
  image?: string; // the image that class got
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
  status: "ready" | "scaled-to-zero" | "deploying" | "error" | "unavailable";
  replicas_ready: number | null;
  min_replicas: number;
  max_replicas: number;
  scale_to_zero_after_s: number;
  target_concurrency?: number;
  scaling?: ScalingSpec;
  timeout_s?: number;
  in_flight?: number;
  last_cold_start_s?: number;
  placement?: "RESERVED" | "SPOT" | "ANY";
  gpu?: string;
  created_at?: string;
  protocol?: string; // "openai-chat" | "http-json"
  managed_by?: "git" | "api"; // git = fleet configuration owns it; API mutations are refused
  image?: string;
  cpu?: string;
  memory?: string;
}

export interface FleetInfo {
  ok?: boolean;
  regions: string[];
  gpu_classes?: string[];
  fleet_manager: boolean;
  region: string;
}
export interface MetricPanel {
  id: string;
  title: string;
  unit: string;
  unavailable?: boolean;
  series: { name: string; points: [number, number | null][] }[];
}
export interface MetricsResponse {
  region: string;
  start: number;
  end: number;
  step: number;
  panels: MetricPanel[];
}
export interface LogLine {
  timestamp: string;
  line: string;
  pod: string;
  container: string;
}
export interface LogsResponse {
  region: string;
  start: number;
  end: number;
  lines: LogLine[];
  truncated: boolean;
}

export interface ApiKey {
  id: string;
  key?: string; // only returned at creation
  key_preview: string; // "sk-...abcd"
  alias: string;
  budget: number | null; // USD; null means unlimited
  spend: number; // USD
  models: string[]; // allow-list
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
