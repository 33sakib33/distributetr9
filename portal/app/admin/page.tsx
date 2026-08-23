"use client";

import { useEffect, useState } from "react";
import { api, fmtBytes, type NodeRow } from "@/lib/api";

export default function AdminPage() {
  const [nodes, setNodes] = useState<NodeRow[]>([]);
  const [error, setError] = useState("");
  const [reason, setReason] = useState("admin quarantine");

  async function refresh() {
    try {
      const data = (await api("/api/nodes")) as { nodes: NodeRow[] };
      setNodes(data.nodes);
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
      </div>
      {error ? <p className="err">{error}</p> : null}
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
              <th></th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((n) => (
              <tr key={n.id}>
                <td>
                  {n.hostname}
                  <div className="muted" style={{ fontSize: 12 }}>
                    {n.agent_version} · {n.role || "train"} · {n.last_heartbeat_at || "no heartbeat"}
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
                  <button className="ghost" onClick={() => drain(n.id)}>
                    Drain
                  </button>{" "}
                  <button className="ghost" onClick={() => quarantine(n.id)}>
                    Quarantine
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
