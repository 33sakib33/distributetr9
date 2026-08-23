"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, fmtMs, type Artifact, type Job } from "@/lib/api";

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

  if (!job && error) return <p className="err">{error}</p>;
  if (!job) return <p className="muted">Loading…</p>;

  const terminal = ["completed", "failed", "cancelled"].includes(job.status);
  const canCancel = !terminal && job.status !== "cancelling";
  const canRetry = job.status === "failed";
  const t = job.timings;

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
        </div>
        <div className="row" style={{ gap: 8 }}>
          {canCancel ? (
            <button className="ghost" onClick={cancel}>
              Cancel
            </button>
          ) : null}
          {canRetry ? <button onClick={retry}>Retry</button> : null}
        </div>
      </div>
      {error ? <p className="err">{error}</p> : null}
      <div className="grid2">
        <div className="card">
          <h2>Timings</h2>
          <p className="muted">Queue, start, run, and upload are separate. Building is the pip image step.</p>
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
                <td>{fmtMs(t.uploading_ms)}</td>
              </tr>
            </tbody>
          </table>
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
        <h2>Artifacts</h2>
        {artifacts.length === 0 ? (
          <p className="muted">Promoted files appear after a successful complete.</p>
        ) : (
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
                  <td className="mono">{a.object_key}</td>
                  <td>{a.size_bytes}</td>
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
        )}
      </div>
    </div>
  );
}
