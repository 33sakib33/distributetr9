"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import LineChart, { type Series } from "@/components/LineChart";
import StageBar, { type Stage } from "@/components/StageBar";
import { ErrorBox, Loading } from "@/components/States";
import Time from "@/components/Time";
import { api, fmtMs, type Artifact, type Job, type JobUsage, type StageEntry } from "@/lib/api";
import { fmtBytes, fmtDuration, fmtMb, ordinal, parseIso } from "@/lib/format";

const TERMINAL = ["completed", "failed", "cancelled"];

function secs(entry?: StageEntry) {
  if (!entry?.started_at || !entry.finished_at) return null;
  return (parseIso(entry.finished_at) - parseIso(entry.started_at)) / 1000;
}

/** The API always reports timings.uploading_ms as 0, so take the upload time from the stage map. */
function uploadMs(job: Job): number {
  const entry = job.stages?.uploading;
  if (!entry?.started_at) return job.timings.uploading_ms;
  const end = entry.finished_at ? parseIso(entry.finished_at) : Date.now();
  return Math.max(0, end - parseIso(entry.started_at));
}

/** Map the job status and the attempt's stage map onto the timeline shown to users. */
function buildStages(job: Job): Stage[] {
  const map = job.stages ?? {};
  const status = job.status;
  const failed = status === "failed";
  const cancelled = status === "cancelled";
  const ended = failed || cancelled;
  const hasAttempt = job.latest_attempt != null;
  const building =
    map.building_environment != null || status === "building" || job.building_environment != null;

  const stages: Stage[] = [];
  const t = job.timings;
  stages.push({
    name: "queued",
    label: "Queued",
    // A job cancelled while waiting did queue; only a failure before any attempt marks the step.
    state: hasAttempt || status === "building" ? "done" : ended ? (failed ? "failed" : "done") : "active",
    detail: job.timings ? fmtMs(job.timings.queued_ms) : undefined,
  });
  if (building) {
    const e = map.building_environment;
    stages.push({
      name: "building_environment",
      label: "Building environment",
      state: e?.finished_at ? "done" : ended ? "failed" : e || status === "building" ? "active" : "pending",
      detail: secs(e) != null ? fmtDuration(secs(e)) : job.building_environment?.state,
    });
  }
  // What the timings say was reached when the stage map has no entry (older attempts).
  const hit: Record<string, boolean> = {
    pulling_image: (t?.running_ms ?? 0) > 0,
    running: (t?.running_ms ?? 0) > 0,
    uploading: uploadMs(job) > 0,
  };
  const late = (name: string, label: string, state: string | null) => {
    const e = map[name];
    let st: Stage["state"] = e?.finished_at ? "done" : e?.started_at ? "active" : "pending";
    if (!e && state && status === state) st = "active";
    if (st === "pending" && ended && hit[name]) st = "done";
    const took = secs(e) ?? (name === "running" && st === "done" && !e ? (t?.running_ms ?? 0) / 1000 : null);
    stages.push({
      name,
      label,
      state: st,
      detail: took != null ? fmtDuration(took) : undefined,
      progress: e?.progress ?? undefined,
    });
  };
  late("pulling_image", "Pulling image", status === "starting" ? "starting" : null);
  late("running", "Running", "running");
  late("uploading", "Uploading", "uploading");
  stages.push({
    name: "done",
    label: failed ? "Failed" : cancelled ? "Cancelled" : "Done",
    state: status === "completed" ? "done" : failed ? "failed" : cancelled ? "failed" : "pending",
  });

  // A stage still open when the job ended is where it stopped: a failure marks it, a
  // cancelled run did that step until it was stopped. Nothing after it will happen.
  if (ended) {
    const open = stages.find((s, i) => s.state === "active" && i < stages.length - 1);
    if (open) open.state = failed ? "failed" : "done";
    stages.forEach((s, i) => {
      if (i < stages.length - 1 && s.state === "pending") s.state = "skipped";
    });
  }
  // Stages before the furthest one reached that never reported are skipped, not pending.
  let reached = -1;
  stages.forEach((s, i) => {
    if (i > 0 && i < stages.length - 1 && (s.state === "active" || s.state === "done" || s.state === "failed")) {
      reached = i;
    }
  });
  stages.forEach((s, i) => {
    if (i > 0 && i < reached && s.state === "pending") s.state = "skipped";
  });
  return stages;
}

/** "3rd in line for ampere-8g, estimated start in ~12m 00s" for a queued job. */
function queueNote(job: Job): string | null {
  if (job.status !== "queued" || job.queue_position == null) return null;
  const cls = typeof job.resources?.gpu_class === "string" && job.resources.gpu_class ? job.resources.gpu_class : "CPU jobs";
  const place = job.queue_position === 1 ? `Next in line for ${cls}` : `${ordinal(job.queue_position)} in line for ${cls}`;
  const eta = job.est_start_seconds;
  if (eta == null) return `${place}. No PC of this class is online right now, so there is no estimate.`;
  if (eta <= 0) return `${place}. A PC is free and should pick it up shortly.`;
  return `${place}, estimated start in ~${fmtDuration(eta)}.`;
}

function pts(usage: JobUsage, pick: (s: JobUsage["samples"][number]) => number | null, scale = 1) {
  return usage.samples.map((s) => {
    const v = pick(s);
    return { t: parseIso(s.at) / 1000, v: v == null ? null : v / scale };
  });
}

type Line = { seq: number; stream: string; line: string };

export default function JobDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [job, setJob] = useState<Job | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [lines, setLines] = useState<Line[]>([]);
  const [buildLog, setBuildLog] = useState("");
  const [error, setError] = useState("");
  const [reconnect, setReconnect] = useState(false);
  const [usage, setUsage] = useState<JobUsage | null>(null);
  const [usageError, setUsageError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  async function refreshJob() {
    const data = (await api(`/api/jobs/${id}`)) as Job;
    setJob(data);
    if (["building", "failed"].includes(data.status)) {
      try {
        const logs = (await api(`/api/jobs/${id}/build-logs`)) as { text?: string };
        setBuildLog(logs.text || "");
      } catch {
        setBuildLog("");
      }
    }
    if (["completed", "failed", "cancelled"].includes(data.status)) {
      const arts = (await api(`/api/jobs/${id}/artifacts`)) as { artifacts: Artifact[] };
      setArtifacts(arts.artifacts);
    }
    return data;
  }

  useEffect(() => {
    let es: EventSource | null = null;
    let stopped = false;
    async function start() {
      try {
        await refreshJob();
        es = new EventSource(`/api/jobs/${id}/logs/stream`);
        es.addEventListener("log", (ev) => {
          const chunk = JSON.parse((ev as MessageEvent).data) as Line;
          setLines((prev) => (prev.some((p) => p.seq === chunk.seq) ? prev : [...prev, chunk]));
        });
        es.addEventListener("done", async () => {
          es?.close();
          setReconnect(false);
          await refreshJob();
        });
        es.onerror = () => {
          setReconnect(true);
        };
        es.onopen = () => setReconnect(false);
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "load failed");
      }
    }
    start();
    const poll = setInterval(() => {
      refreshJob().catch(() => undefined);
    }, 5000);
    return () => {
      stopped = true;
      es?.close();
      clearInterval(poll);
    };
  }, [id]);

  const jobId = job?.id;
  const isTerminal = job ? TERMINAL.includes(job.status) : false;
  const attemptNo = job?.latest_attempt?.attempt_number ?? 0;
  useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    async function load() {
      try {
        const data = (await api(`/api/jobs/${jobId}/usage`)) as JobUsage;
        if (!stopped) {
          setUsage(data);
          setUsageError("");
        }
      } catch (err) {
        if (!stopped) setUsageError(err instanceof Error ? err.message : "usage failed to load");
      }
    }
    load();
    if (isTerminal) return () => { stopped = true; };
    const timer = setInterval(load, 5000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [jobId, isTerminal, attemptNo]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [lines.length]);

  async function cancel() {
    setError("");
    try {
      await api(`/api/jobs/${id}/cancel`, { method: "POST" });
      await refreshJob();
    } catch (err) {
      setError(err instanceof Error ? err.message : "cancel failed");
    }
  }

  async function retry() {
    setError("");
    try {
      await api(`/api/jobs/${id}/retry`, { method: "POST" });
      setLines([]);
      await refreshJob();
    } catch (err) {
      setError(err instanceof Error ? err.message : "retry failed");
    }
  }

  if (!job && error) return <ErrorBox message={error} onRetry={() => void refreshJob().catch(() => undefined)} />;
  if (!job) return <Loading label="Loading job…" />;

  const terminal = isTerminal;
  const canCancel = !terminal && job.status !== "cancelling";
  const canRetry = job.status === "failed";
  const t = job.timings;
  const waitNote = queueNote(job);
  const hasOutputs = artifacts.some((a) => a.type === "output");

  return (
    <div className="stack">
      <div className="row">
        <div>
          <Link className="muted" href="/">
            Queue
          </Link>
          <h1>
            <code>{job.entrypoint}</code>
          </h1>
          <p className="muted">
            <span className={`chip ${job.status}`}>{job.status}</span>{" "}
            {job.latest_attempt?.hostname || "no node yet"}
            {job.pending_reason ? ` · ${job.pending_reason}` : ""}
            {reconnect ? " · reconnecting logs" : ""}
          </p>
          {job.user_email || job.project_name ? (
            <p className="muted job-meta">
              {job.user_email ? (
                <>
                  Submitted by <span className="email-cell">{job.user_email}</span>
                </>
              ) : null}
              {job.user_email && job.project_name ? " · " : null}
              {job.project_name ? <>Project {job.project_name}</> : null}
              {job.created_at ? (
                <>
                  {" · "}
                  <Time iso={job.created_at} />
                </>
              ) : null}
            </p>
          ) : null}
        </div>
        <div className="btn-row">
          <Link className="btn ghost" href={`/submit?clone=${job.id}`}>
            Clone
          </Link>
          {canCancel ? (
            <button className="ghost" onClick={cancel}>
              Cancel
            </button>
          ) : null}
          {canRetry ? <button onClick={retry}>Retry</button> : null}
        </div>
      </div>
      {error ? <ErrorBox message={error} /> : null}
      <div className="card">
        <h2>Progress</h2>
        {waitNote ? (
          <p className="queue-note" role="status">
            {waitNote}
          </p>
        ) : null}
        <StageBar stages={buildStages(job)} />
      </div>
      <div className="grid2">
        <div className="card">
          <h2>Timings</h2>
          <p className="muted">Queue, start, run, and upload are separate. Building is the pip image step.</p>
          <div className="table-scroll">
          <table>
            <tbody>
              <tr>
                <th>Queued</th>
                <td>{fmtMs(t.queued_ms)}</td>
              </tr>
              <tr>
                <th>Starting</th>
                <td>{fmtMs(t.starting_ms)}</td>
              </tr>
              <tr>
                <th>Running</th>
                <td>{fmtMs(t.running_ms)}</td>
              </tr>
              <tr>
                <th>Uploading</th>
                <td>{fmtMs(uploadMs(job))}</td>
              </tr>
            </tbody>
          </table>
          </div>
        </div>
        <div className="card">
          <h2>Attempt</h2>
          <p className="muted">
            #{job.latest_attempt?.attempt_number ?? 0} · {job.latest_attempt?.observed_state || "none"}
            {job.latest_attempt?.exit_code != null ? ` · exit ${job.latest_attempt.exit_code}` : ""}
          </p>
          <p className="mono muted" style={{ fontSize: 13 }}>
            {job.id}
          </p>
        </div>
      </div>
      {renderUsage(job, usage, usageError)}
      {job.failure_explanation ? (
        <div className="card failure-card" role="alert">
          <h2>{job.failure_explanation.title}</h2>
          <p className="failure-advice">{job.failure_explanation.advice}</p>
        </div>
      ) : null}
      {job.status === "building" || buildLog ? (
        <div className="card">
          <h2>Environment build</h2>
          <p className="muted">Pip output stays here. Training logs are below after the image is ready.</p>
          <div className="log">
            {buildLog ? (
              buildLog.split("\n").map((line, i) => <div key={i}>{line}</div>)
            ) : (
              <span className="muted">Waiting for the lab PC to build the image…</span>
            )}
          </div>
        </div>
      ) : null}
      <div className="card">
        <h2>Logs</h2>
        <div className="log">
          {lines.length === 0 ? <span className="muted">Waiting for output…</span> : null}
          {lines.map((line) => (
            <div key={line.seq} className={line.stream === "stderr" ? "stderr" : ""}>
              {line.line}
            </div>
          ))}
          <div ref={bottom} />
        </div>
      </div>
      <div className="card">
        <div className="row">
          <h2>Artifacts</h2>
          {hasOutputs ? (
            <a className="btn ghost" href={`/api/jobs/${job.id}/outputs.zip`} download>
              Download all outputs
            </a>
          ) : null}
        </div>
        {artifacts.length === 0 ? (
          <p className="muted">Promoted files appear after a successful complete.</p>
        ) : (
          <div className="table-scroll wide">
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th>Key</th>
                <th>Size</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {artifacts.map((a) => (
                <tr key={a.id}>
                  <td>{a.type}</td>
                  <td className="mono wrap-any">{a.object_key}</td>
                  <td>{fmtBytes(a.size_bytes)}</td>
                  <td>
                    {a.type === "output" || a.type === "input" ? (
                      <a className="btn ghost" href={`/api${a.download}`}>
                        Download
                      </a>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </div>
  );
}

function renderUsage(job: Job, usage: JobUsage | null, usageError: string) {
  const running = !TERMINAL.includes(job.status);
  if (usageError && !usage) return <ErrorBox message={`Resource usage: ${usageError}`} />;
  if (!usage) return <Loading label="Loading resource usage…" />;
  const hasSamples = usage.samples.length > 0;
  const hasGpu = usage.samples.some((s) => s.gpu_pct != null || s.vram_mb != null);
  const requested = Number(job.resources?.min_vram_mb) || 0;
  const peak = usage.peak_vram_mb;
  const total = usage.vram_total_mb;
  const underused = peak != null && requested > 0 && peak < requested * 0.5;

  const series = (name: string, points: ReturnType<typeof pts>): Series[] => [{ name, points }];
  return (
    <div className="card">
      <h2>Resources</h2>
      {hasGpu && peak != null ? (
        <>
          <p>
            Peak VRAM <strong>{fmtMb(peak)}</strong>
            {total ? <> / {fmtMb(total)}</> : null}
            {usage.avg_gpu_pct != null ? <span className="muted"> · average GPU {Math.round(usage.avg_gpu_pct)}%</span> : null}
            {usage.peak_ram_mb != null ? <span className="muted"> · peak RAM {fmtMb(usage.peak_ram_mb)}</span> : null}
          </p>
          {underused ? (
            <p className="hint">
              This job used less than half of the {fmtMb(requested)} you requested. You could request a smaller GPU
              and start sooner.
            </p>
          ) : null}
        </>
      ) : usage.peak_ram_mb != null ? (
        <p>
          Peak RAM <strong>{fmtMb(usage.peak_ram_mb)}</strong>
        </p>
      ) : null}
      {!hasSamples ? (
        <p className="muted">
          {running ? "Usage appears once the job starts running." : "No usage was recorded for this job."}
        </p>
      ) : (
        <div className="charts">
          {hasGpu ? (
            <div className="chart-card">
              <h3>GPU utilisation</h3>
              <LineChart series={series("GPU %", pts(usage, (s) => s.gpu_pct))} yMax={100} unit="%" />
            </div>
          ) : null}
          {hasGpu ? (
            <div className="chart-card">
              <h3>VRAM</h3>
              <LineChart
                series={series("VRAM GB", pts(usage, (s) => s.vram_mb, 1024))}
                yMax={total ? total / 1024 : undefined}
                unit=" GB"
              />
            </div>
          ) : null}
          <div className="chart-card">
            <h3>CPU</h3>
            <LineChart series={series("CPU %", pts(usage, (s) => s.cpu_pct))} unit="%" />
          </div>
          <div className="chart-card">
            <h3>RAM</h3>
            <LineChart series={series("RAM GB", pts(usage, (s) => s.ram_mb, 1024))} unit=" GB" />
          </div>
        </div>
      )}
      {!hasGpu && hasSamples ? <p className="muted">This node has no GPU, so GPU charts are not shown.</p> : null}
    </div>
  );
}
