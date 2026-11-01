import { fmtDuration } from "./format";

export type Me = {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
};

export type Project = { id: string; name: string };

export type Environment = {
  id: string;
  name: string;
  framework: string;
  python_version: string;
  cuda_version: string;
  image_digest: string;
};

export type Attempt = {
  id: string;
  attempt_number: number;
  node_id: string;
  hostname?: string;
  compatibility_class?: string;
  observed_state: string;
  exit_code: number | null;
  failure_category: string | null;
  last_log_seq: number;
  peak_vram_mb?: number | null;
  peak_ram_mb?: number | null;
  avg_gpu_pct?: number | null;
};

export type StageProgress = { done: number; total: number; unit?: string };
export type StageEntry = {
  started_at: string | null;
  finished_at: string | null;
  progress: StageProgress | null;
};
/** Keys: pulling_image, building_environment, running, uploading. */
export type StageMap = Record<string, StageEntry>;

export type FailureExplanation = { title: string; advice: string };

export type Job = {
  id: string;
  project_id?: string;
  entrypoint: string;
  args?: unknown[];
  environment_id?: string;
  status: string;
  pending_reason: string | null;
  created_at: string | null;
  completed_at: string | null;
  resources: Record<string, unknown>;
  timings: {
    queued_ms: number;
    starting_ms: number;
    running_ms: number;
    uploading_ms: number;
    building_ms?: number;
  };
  latest_attempt: Attempt | null;
  /** GET /jobs/{id} only: who submitted the job and the project it belongs to. */
  user_email?: string;
  project_name?: string;
  stages?: StageMap;
  /** Set only while a custom environment is waiting for or in a build. */
  building_environment?: { state: string; started_at: string | null } | null;
  /** GET /jobs/{id} only: place among queued jobs of the class, null unless queued. */
  queue_position?: number | null;
  est_start_seconds?: number | null;
  /** GET /jobs/{id} only: set for failed jobs when a rule matched. */
  failure_explanation?: FailureExplanation | null;
};

/** POST /jobs/inspect */
export type InspectResult = {
  entrypoints: string[];
  suggested_entrypoint: string | null;
  requirements: string | null;
};

export type GpuAvailability = {
  gpu_class: string;
  /** Human name of the card ("NVIDIA GeForce RTX 4070"); null for the cpu class. */
  gpu_model?: string | null;
  vram_mb: number;
  nodes_total: number;
  nodes_online: number;
  nodes_free: number;
  queued: number;
  est_wait_seconds: number | null;
};

export type NotificationSettings = {
  notify_email: boolean;
  notify_webhook_url: string | null;
  email_available: boolean;
};

/** One row of GET /admin/projects. */
export type AdminProject = {
  id: string;
  name: string;
  created_by_email: string;
  members: number;
  max_concurrent_jobs: number;
};

export type UsageSample = {
  at: string;
  gpu_pct: number | null;
  vram_mb: number | null;
  cpu_pct: number | null;
  ram_mb: number | null;
};

export type JobUsage = {
  attempt_id: string | null;
  samples: UsageSample[];
  peak_vram_mb: number | null;
  peak_ram_mb: number | null;
  avg_gpu_pct: number | null;
  vram_total_mb: number | null;
};

export type NodeRow = {
  id: string;
  hostname: string;
  gpu_model: string;
  compatibility_class: string;
  vram_allocatable_mb: number;
  eligibility: string;
  eligibility_reason: string | null;
  role?: string;
  last_heartbeat_at: string | null;
  gpu_desktop_busy: boolean;
  disk_free_bytes: number;
  agent_version: string;
  update_ring: number;
  last_restart?: LastRestart | null;
  room?: string | null;
  room_source?: "agent" | "admin" | null;
  labels?: string[];
  decommissioned_at?: string | null;
  /** From the newest health sample; null when there is none newer than 5 minutes. */
  gpu_pct?: number | null;
  gpu_temp_c?: number | null;
  busy?: boolean;
  current_attempt?: { attempt_id: string; job_id: string; state: string } | null;
};

export type LastRestart = {
  reason: string;
  exit_code: number | null;
  detail: string | null;
  at: string | null;
};

export type NodeHealthSnapshot = {
  gpu_temp_c?: number | null;
  gpu_pct?: number | null;
  vram_used_mb?: number | null;
  driver_version?: string | null;
  uptime_s?: number | null;
  docker_ok?: boolean | null;
};

export type NodeHealthPoint = {
  at: string;
  gpu_temp_c: number | null;
  gpu_pct: number | null;
  disk_free_bytes: number | null;
  busy: boolean;
};

export type NodeAttemptRow = {
  attempt_id: string;
  job_id: string;
  attempt_number: number;
  state: string;
  exit_code: number | null;
  failure_category: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type NodeEventRow = {
  at: string;
  kind: string;
  payload: Record<string, unknown> | null;
};

export type NodeDetail = NodeRow & {
  last_restart: LastRestart | null;
  cpu_cores?: number | null;
  ram_mb?: number | null;
  vram_total_mb: number | null;
  compute_capability: string | null;
  driver_version: string | null;
  /** The last heartbeat body; the sampled values sit under `health`. */
  last_health: (NodeHealthSnapshot & { health?: NodeHealthSnapshot | null }) | null;
  health_history: NodeHealthPoint[];
  recent_attempts: NodeAttemptRow[];
  events: NodeEventRow[];
};

export type NodeLogs = {
  requested_at: string | null;
  uploaded_at: string | null;
  text: string | null;
};

export type UsageRow = {
  key: string;
  label: string;
  gpu_hours: number;
  jobs: number;
  succeeded: number;
  failed: number;
};

export type UsageReport = {
  rows: UsageRow[];
  /** Present on /admin/usage only. */
  lab?: { online_hours: number; busy_hours: number; utilisation: number };
};

export type NodeList = {
  nodes: NodeRow[];
  rooms?: string[];
  agent_target_version: string | null;
  agent_rollout_ring: number;
};

export type AlertStatus = "open" | "acknowledged" | "muted" | "resolved";

export type AlertRow = {
  id: string;
  key: string;
  kind: string;
  severity: string;
  message: string;
  status: AlertStatus;
  node_id: string | null;
  first_seen: string;
  last_seen: string;
  resolved_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  muted_until: string | null;
};

export type AlertGroup = {
  kind: string;
  /** Open (not resolved) alerts in the group. */
  count: number;
  /** Open alerts nobody has acknowledged or muted. */
  unhandled: number;
  severity: string;
  alerts: AlertRow[];
};

export type AlertList = { groups: AlertGroup[] };

/** One row of GET /admin/jobs: the job plus its owner and project. */
export type AdminJob = Job & {
  user_email: string;
  project_name: string;
};

export type AdminJobList = { items: AdminJob[]; next_cursor: string | null };

export type BulkCancelResult = {
  cancelled: string[];
  skipped: { job_id: string; reason: string }[];
};

export type Page<T> = { items: T[]; next_cursor: string | null };

/** One closed weekly window (GET /admin/schedules). weekday 0 is Monday. */
export type ScheduleWindow = {
  id: string;
  room: string | null;
  node_id: string | null;
  hostname: string | null;
  weekday: number;
  start_minute: number;
  end_minute: number;
  start: string;
  end: string;
  note: string | null;
};

export type ScheduleList = Page<ScheduleWindow> & { timezone: string };

export type ProjectMember = {
  user_id: string;
  email: string;
  display_name: string;
  role: "owner" | "member";
  blocked: boolean;
  /** True until the person signs in for the first time. */
  invited?: boolean;
};

/** POST /projects/{id}/members */
export type AddedMember = ProjectMember;

export type ProjectMembers = {
  project: { id: string; name: string; max_concurrent_jobs: number };
  your_role: "owner" | "member";
  can_edit: boolean;
  items: ProjectMember[];
};

export type AdminUser = {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  blocked: boolean;
  disabled_at: string | null;
  created_at: string | null;
  max_concurrent_jobs: number;
  /** True until the person signs in for the first time. */
  invited?: boolean;
};

export type QuotaItem = {
  subject_type: "user" | "project";
  subject_id: string;
  label: string | null;
  max_concurrent_jobs: number;
  default: boolean;
};

export type CatalogEnvironment = {
  id: string;
  name: string;
  framework: string;
  python_version: string;
  cuda_version: string;
  gpu_archs: string[];
  image_digest: string;
  upstream_image: string | null;
  cuda_preflight_ok: boolean | null;
  retired: boolean;
  retired_at: string | null;
  created_at: string | null;
  jobs_last_30d: number;
};

export type Rollout = {
  target_version: string | null;
  bundle_version: string | null;
  rollout_ring: number;
  min_version: string | null;
  /** The stored override per key; null means the environment default applies. */
  overrides: {
    agent_target_version?: string | null;
    agent_rollout_ring?: number | null;
    agent_min_version?: string | null;
  };
  defaults: { target_version: string | null; rollout_ring: number; min_version: string | null };
  nodes_by_version: Record<string, number>;
};

export type AuditRow = {
  id: string;
  at: string;
  actor_user_id: string | null;
  actor_email: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  details: Record<string, unknown>;
};

export type AuditList = Page<AuditRow> & { actions: string[] };

export type Artifact = {
  id: string;
  type: string;
  object_key: string;
  size_bytes: number;
  download: string;
};

/** FastAPI validation errors come as `detail: [{msg}]`; show the first message plainly. */
function detailText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string };
    if (typeof first?.msg === "string") return first.msg.replace(/^Value error, /, "");
  }
  return "";
}

/** An API failure: the message to show, plus the id the server logged the request under. */
export class ApiError extends Error {
  status: number;
  requestId: string | null;
  /** The machine-readable `error` code of the response, e.g. "email_domain". */
  code: string | null;
  constructor(message: string, status: number, requestId: string | null, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
    this.code = code;
  }
}

/** The error code carried by a thrown API error, if any. */
export function errorCodeOf(err: unknown): string | null {
  return err instanceof ApiError ? err.code : null;
}

let lastFailure: { message: string; requestId: string } | null = null;

/**
 * Request id of the API failure whose message is (part of) `message`. Lets
 * ErrorBox show the id even where a page only kept the message string.
 */
export function requestIdFor(message: string): string | null {
  return lastFailure && message.includes(lastFailure.message) ? lastFailure.requestId : null;
}

/** The request id carried by a thrown API error, if any. */
export function requestIdOf(err: unknown): string | null {
  return err instanceof ApiError ? err.requestId : null;
}

async function parse(res: Response) {
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { message: text };
  }
  if (!res.ok) {
    const err = (data ?? {}) as { message?: string; error?: string; detail?: unknown };
    const message = err.message || detailText(err.detail) || err.error || res.statusText;
    const requestId = res.headers.get("x-request-id");
    if (requestId && message) lastFailure = { message, requestId };
    throw new ApiError(message, res.status, requestId, typeof err.error === "string" ? err.error : null);
  }
  return data;
}

export async function api(path: string, init: RequestInit = {}) {
  const res = await fetch(path, { ...init, cache: "no-store" });
  return parse(res);
}

/** Fetch options for a JSON request body. */
export function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
}

/** Milliseconds as "340 ms", "12.5 s", then rolled up: "3m 05s", "11h 28m", "2d 4h". */
export function fmtMs(ms: number | undefined) {
  const n = Math.max(0, Number(ms) || 0);
  if (n < 1000) return `${n} ms`;
  if (n < 60_000) return `${(n / 1000).toFixed(1)} s`;
  return fmtDuration(n / 1000);
}

export { fmtBytes } from "./format";
