"use client";

import { useEffect, useState } from "react";
import { api, fmtBytes, type AlertRow, type NodeList, type NodeRow } from "@/lib/api";

export default function AdminPage() {
  const [nodes, setNodes] = useState<NodeRow[]>([]);
  const [target, setTarget] = useState<string | null>(null);
  const [rolloutRing, setRolloutRing] = useState(2);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [error, setError] = useState("");
  const [reason, setReason] = useState("admin quarantine");

  async function refresh() {
    try {
      const data = (await api("/api/nodes")) as NodeList;
      setNodes(data.nodes);
      setTarget(data.agent_target_version);
      setRolloutRing(data.agent_rollout_ring);
      const current = (await api("/api/admin/alerts")) as { alerts: AlertRow[] };
      setAlerts(current.alerts);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "admin required");
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  async function drain(id: string) {
    await api(`/api/nodes/${id}/drain`, { method: "POST" });
    await refresh();
  }

  async function release(id: string) {
    await api(`/api/nodes/${id}/release`, { method: "POST" });
    await refresh();
  }

  async function setRing(id: string, ring: number) {
    await api(`/api/nodes/${id}/ring`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ring }),
    });
    await refresh();
  }

  async function quarantine(id: string) {
    await api(`/api/nodes/${id}/quarantine`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason }),
    });
    await refresh();
  }

  return (
    <div className="stack">
      <div>
        <h1>Nodes</h1>
        <p className="muted">Admin only. Drain finishes the current job then skips claims. Quarantine fences in-flight work.</p>
        <p className="muted">
          Agent target {target || "unknown"} · rolling out to ring {rolloutRing} and below (0 canary, 1 early, 2 everyone)
        </p>
      </div>
      {error ? <p className="err">{error}</p> : null}
      {alerts.length ? (
        <div className="card">
          <strong>Alerts</strong>
          <ul>
            {alerts.map((a) => (
              <li key={a.key}>{a.message}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <label style={{ maxWidth: 360 }}>
        Quarantine reason
        <input value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="card" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Host</th>
              <th>GPU</th>
              <th>Class</th>
              <th>State</th>
              <th>Disk</th>
              <th>Ring</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((n) => (
              <tr key={n.id}>
                <td>
                  {n.hostname}
                  <div className="muted" style={{ fontSize: 12 }}>
                    {n.agent_version}
                    {target && n.agent_version !== target ? " (update pending)" : ""} · {n.role || "train"} ·{" "}
                    {n.last_heartbeat_at || "no heartbeat"}
                  </div>
                </td>
                <td>
                  {n.gpu_model}
                  <div className="muted" style={{ fontSize: 12 }}>
                    {n.vram_allocatable_mb} MB
                  </div>
                </td>
                <td className="mono">{n.compatibility_class}</td>
                <td>
                  <span className={`chip ${n.eligibility}`}>{n.eligibility}</span>
                  {n.gpu_desktop_busy ? <div className="muted">GPU busy</div> : null}
                  {n.eligibility_reason ? <div className="muted">{n.eligibility_reason}</div> : null}
                </td>
                <td className="muted">{fmtBytes(n.disk_free_bytes)}</td>
                <td>
                  <select value={n.update_ring} onChange={(e) => setRing(n.id, Number(e.target.value))}>
                    <option value={0}>0 canary</option>
                    <option value={1}>1 early</option>
                    <option value={2}>2 everyone</option>
                  </select>
                </td>
                <td>
                  <button className="ghost" onClick={() => drain(n.id)}>
                    Drain
                  </button>{" "}
                  <button className="ghost" onClick={() => quarantine(n.id)}>
                    Quarantine
                  </button>
                  {n.eligibility === "draining" || n.eligibility === "quarantined" ? (
                    <>
                      {" "}
                      <button className="ghost" onClick={() => release(n.id)}>
                        Release
                      </button>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
