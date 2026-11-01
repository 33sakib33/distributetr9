"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import DataTable, { type Column } from "@/components/DataTable";
import LineChart from "@/components/LineChart";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, type LastRestart, type NodeAttemptRow, type NodeDetail, type NodeEventRow, type NodeLogs } from "@/lib/api";
import Time from "@/components/Time";
import { fmtBytes, fmtDuration, fmtMb, parseIso } from "@/lib/format";

const POLL_MS = 3000;
const POLL_MAX_MS = 120_000;

function restartText(r: LastRestart | null | undefined) {
  if (!r) return "unknown";
  switch (r.reason) {
    case "update":
      return "updated to a new version";
    case "crash":
      return `crashed${r.detail ? `: ${r.detail}` : ""}`;
    case "reboot":
      return "PC rebooted";
    case "first_start":
      return "first start";
    default:
      return "restarted manually";
  }
}

function eventText(e: NodeEventRow) {
  if (e.kind === "restart") return `Agent restarted: ${restartText(e.payload as unknown as LastRestart)}`;
  return e.kind.replace(/_/g, " ");
}

export default function NodeDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [node, setNode] = useState<NodeDetail | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setNode((await api(`/api/nodes/${id}`)) as NodeDetail);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load node");
    }
  }, [id]);

  useEffect(() => {
    void load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  if (!node && error) return <ErrorBox message={error} onRetry={() => void load()} />;
  if (!node) return <Loading label="Loading node…" />;

  const h = node.last_health?.health ?? node.last_health ?? {};
  const history = node.health_history;
  const toPts = (pick: (p: (typeof history)[number]) => number | null, scale = 1) =>
    history.map((p) => ({ t: parseIso(p.at) / 1000, v: pick(p) == null ? null : (pick(p) as number) / scale }));
  const hasTemp = history.some((p) => p.gpu_temp_c != null);
  const hasGpu = history.some((p) => p.gpu_pct != null);

  const columns: Column<NodeAttemptRow>[] = [
    {
      key: "job",
      header: "Job",
      render: (a) => (
        <Link href={`/jobs/${a.job_id}`} className="mono">
          {a.job_id.slice(0, 8)}
        </Link>
      ),
    },
    { key: "attempt", header: "Attempt", render: (a) => `#${a.attempt_number}` },
    {
      key: "state",
      header: "State",
      render: (a) => (
        <>
          <span className={`chip ${a.state}`}>{a.state}</span>
          {a.exit_code != null ? <span className="muted"> exit {a.exit_code}</span> : null}
          {a.failure_category ? <div className="muted">{a.failure_category}</div> : null}
        </>
      ),
    },
    { key: "started", header: "Started", render: (a) => <Time iso={a.started_at ?? a.created_at} /> },
    {
      key: "took",
      header: "Duration",
      align: "right",
      render: (a) =>
        a.started_at && a.completed_at
          ? fmtDuration((parseIso(a.completed_at) - parseIso(a.started_at)) / 1000)
          : "—",
    },
  ];

  return (
    <div className="stack">
      <div>
        <Link className="muted" href="/admin/nodes">
          Nodes
        </Link>
        <h1>{node.hostname}</h1>
        <p className="muted">
          <span className={`chip ${node.eligibility}`}>{node.eligibility}</span> {node.gpu_model} ·{" "}
          <span className="mono">{node.compatibility_class}</span> · agent {node.agent_version} · last heartbeat{" "}
          <Time iso={node.last_heartbeat_at} />
          {node.eligibility_reason ? ` · ${node.eligibility_reason}` : ""}
        </p>
      </div>
      {error ? <p className="err" role="alert">{error}</p> : null}

      <div className="card">
        <h2>Health</h2>
        <div className="stats">
          <Stat label="GPU temperature" value={h.gpu_temp_c != null ? `${Math.round(h.gpu_temp_c)} °C` : "—"} />
          <Stat label="GPU utilisation" value={h.gpu_pct != null ? `${Math.round(h.gpu_pct)}%` : "—"} />
          <Stat
            label="VRAM"
            value={
              h.vram_used_mb != null
                ? `${fmtMb(h.vram_used_mb)}${node.vram_total_mb ? ` / ${fmtMb(node.vram_total_mb)}` : ""}`
                : fmtMb(node.vram_total_mb)
            }
          />
          <Stat label="Disk free" value={fmtBytes(node.disk_free_bytes)} />
          <Stat label="Uptime" value={h.uptime_s != null ? fmtDuration(h.uptime_s) : "—"} />
          <Stat
            label="Docker"
            value={h.docker_ok == null ? "—" : h.docker_ok ? "OK" : "Not responding"}
          />
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          {node.gpu_desktop_busy ? "Someone is using the GPU desktop. " : ""}
          Driver {h.driver_version || node.driver_version || "unknown"} · compute capability{" "}
          {node.compute_capability || "unknown"}
        </p>
      </div>

      <div className="card">
        <h2>Last 24 hours</h2>
        {history.length === 0 ? (
          <Empty title="No health samples yet" hint="Samples arrive about once a minute while the agent runs." />
        ) : (
          <div className="charts">
            {hasTemp ? (
              <div className="chart-card">
                <h3>GPU temperature</h3>
                <LineChart series={[{ name: "°C", points: toPts((p) => p.gpu_temp_c) }]} yMax={100} unit="°" />
              </div>
            ) : null}
            {hasGpu ? (
              <div className="chart-card">
                <h3>GPU utilisation</h3>
                <LineChart series={[{ name: "GPU %", points: toPts((p) => p.gpu_pct) }]} yMax={100} unit="%" />
              </div>
            ) : null}
            <div className="chart-card">
              <h3>Disk free</h3>
              <LineChart
                series={[{ name: "GiB free", points: toPts((p) => p.disk_free_bytes, 1024 ** 3) }]}
                unit=" GiB"
              />
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Last restart</h2>
        {node.last_restart ? (
          <p style={{ marginBottom: 0 }}>
            Agent {restartText(node.last_restart)}
            {node.last_restart.exit_code != null ? <span className="muted"> (exit code {node.last_restart.exit_code})</span> : null}
            {node.last_restart.at ? (
              <span className="muted">
                {" · "}
                <Time iso={node.last_restart.at} />
              </span>
            ) : null}
          </p>
        ) : (
          <p className="muted" style={{ marginBottom: 0 }}>
            No restart recorded.
          </p>
        )}
      </div>

      <div className="card">
        <h2>Recent attempts</h2>
        <DataTable
          columns={columns}
          rows={node.recent_attempts}
          rowKey={(a) => a.attempt_id}
          caption="Recent job attempts on this node"
          empty={<Empty title="No jobs have run here yet" />}
        />
      </div>

      <div className="card">
        <h2>Events</h2>
        {node.events.length === 0 ? (
          <Empty title="No events recorded" />
        ) : (
          <ul className="list-plain">
            {node.events.map((e, i) => (
              <li key={`${e.at}-${i}`}>
                <span className="muted tnum">
                  <Time iso={e.at} />
                </span>
                <span>{eventText(e)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <AgentLogs nodeId={node.id} />
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

function AgentLogs({ nodeId }: { nodeId: string }) {
  const [logs, setLogs] = useState<NodeLogs | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stop = useCallback(() => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    setWaiting(false);
  }, []);
  useEffect(() => stop, [stop]);

  async function fetchLogs() {
    setError("");
    setCopied(false);
    stop();
    try {
      await api(`/api/nodes/${nodeId}/logs/request`, { method: "POST" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not request logs");
      return;
    }
    const requestedAt = Date.now();
    setWaiting(true);
    const poll = async () => {
      try {
        const data = (await api(`/api/nodes/${nodeId}/logs`)) as NodeLogs;
        const fresh = data.uploaded_at && (!data.requested_at || parseIso(data.uploaded_at) >= parseIso(data.requested_at));
        if (fresh && data.text != null) {
          setLogs(data);
          stop();
          return;
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "could not read logs");
        stop();
        return;
      }
      if (Date.now() - requestedAt > POLL_MAX_MS) {
        stop();
        setError("The lab PC did not send its log within 2 minutes. It may be offline; try again later.");
      }
    };
    timer.current = setInterval(poll, POLL_MS);
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(logs?.text ?? "");
      setCopied(true);
    } catch {
      setError("Copy failed; select the text and copy it by hand.");
    }
  }

  return (
    <div className="card">
      <div className="row">
        <h2>Agent log</h2>
        <button type="button" className="ghost" onClick={fetchLogs} disabled={waiting}>
          {waiting ? "Waiting for the PC…" : "Fetch logs"}
        </button>
      </div>
      {waiting ? <Loading label="Asked the PC for its log. It answers on its next heartbeat." /> : null}
      {error ? <ErrorBox message={error} /> : null}
      {logs?.text != null ? (
        <>
          <div className="row" style={{ margin: "8px 0" }}>
            <span className="muted">
              <Time iso={logs.uploaded_at} prefix="Uploaded " />
            </span>
            <button type="button" className="ghost small" onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <pre className="pre-box mono" tabIndex={0} aria-label="Agent log">
            {logs.text || "(empty)"}
          </pre>
        </>
      ) : !waiting && !error ? (
        <p className="muted">Fetch the last 500 lines of the agent&apos;s own log from the PC.</p>
      ) : null}
    </div>
  );
}
