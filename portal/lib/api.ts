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
};

export type Job = {
  id: string;
  entrypoint: string;
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
};

export type Artifact = {
  id: string;
  type: string;
  object_key: string;
  size_bytes: number;
  download: string;
};

async function parse(res: Response) {
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { message: text };
  }
  if (!res.ok) {
    const err = data as { message?: string; error?: string };
    throw new Error(err.message || err.error || res.statusText);
  }
  return data;
}

export async function api(path: string, init: RequestInit = {}) {
  const res = await fetch(path, { ...init, cache: "no-store" });
  return parse(res);
}

export function fmtMs(ms: number | undefined) {
  const n = Math.max(0, Number(ms) || 0);
  if (n < 1000) return `${n} ms`;
  if (n < 60_000) return `${(n / 1000).toFixed(1)} s`;
  return `${Math.floor(n / 60000)}m ${Math.round((n % 60000) / 1000)}s`;
}

export function fmtBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
}
