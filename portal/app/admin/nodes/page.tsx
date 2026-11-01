"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import ConfirmButton from "@/components/ConfirmButton";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import Time from "@/components/Time";
import { api, fmtBytes, type NodeList, type NodeRow } from "@/lib/api";
import { RINGS, ringLabel } from "@/lib/rings";

const JSON_HEADERS = { "content-type": "application/json" };
const PAGE_SIZE = 50;

/** Quarantine with its own reason, asked for when the button is clicked. */
function QuarantineButton({ node, onQuarantine }: { node: NodeRow; onQuarantine: (reason: string) => Promise<void> }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("admin quarantine");
  const [busy, setBusy] = useState(false);

  if (!asking) {
    return (
      <button type="button" className="ghost small" onClick={() => setAsking(true)}>
        Quarantine
      </button>
    );
  }
  return (
    <form
      className="nodes-quarantine"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          await onQuarantine(reason.trim() || "admin quarantine");
        } finally {
          setBusy(false);
          setAsking(false);
        }
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && !busy) setAsking(false);
      }}
    >
      <label>
        Reason for quarantining {node.hostname}
        <input value={reason} onChange={(e) => setReason(e.target.value)} autoFocus maxLength={200} />
      </label>
      <div className="nodes-actions">
        <button type="submit" className="danger small" disabled={busy}>
          {busy ? "Working…" : "Quarantine"}
        </button>
        <button type="button" className="ghost small" disabled={busy} onClick={() => setAsking(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** Room and labels of one PC, shown as text with an inline editor. */
function RoomCell({ node, onSaved, onError }: { node: NodeRow; onSaved: () => void; onError: (m: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [room, setRoom] = useState(node.room ?? "");
  const [labels, setLabels] = useState((node.labels ?? []).join(", "));
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      await api(`/api/nodes/${node.id}/meta`, {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({
          room: room.trim(),
          labels: labels
            .split(",")
            .map((l) => l.trim())
            .filter(Boolean),
        }),
      });
      setEditing(false);
      onSaved();
    } catch (err) {
      onError(err instanceof Error ? err.message : "could not save room");
    } finally {
      setBusy(false);
    }
  }

  if (!editing) {
    return (
      <div>
        {node.room ? node.room : <span className="muted">unassigned</span>}
        {node.labels?.length ? <div className="muted nodes-labels">{node.labels.join(", ")}</div> : null}
        <button
          type="button"
          className="ghost small"
          onClick={() => {
            setRoom(node.room ?? "");
            setLabels((node.labels ?? []).join(", "));
            setEditing(true);
          }}
        >
          Edit
        </button>
      </div>
    );
  }
  return (
    <form
      className="nodes-edit"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <label>
        Room
        <input value={room} onChange={(e) => setRoom(e.target.value)} maxLength={64} placeholder="e.g. Lab 301" />
      </label>
      <label>
        Labels (comma separated)
        <input value={labels} onChange={(e) => setLabels(e.target.value)} placeholder="e.g. fast-disk, front-row" />
      </label>
      <div>
        <button type="submit" className="small" disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>{" "}
        <button type="button" className="ghost small" disabled={busy} onClick={() => setEditing(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export default function AdminNodesPage() {
  const [nodes, setNodes] = useState<NodeRow[]>([]);
  const [target, setTarget] = useState<string | null>(null);
  const [rolloutRing, setRolloutRing] = useState(2);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const data = (await api("/api/nodes")) as NodeList;
      setNodes(data.nodes);
      setTarget(data.agent_target_version);
      setRolloutRing(data.agent_rollout_ring);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "admin required");
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 5000);
    return () => clearInterval(t);
  }, [refresh]);

  async function act(path: string, body?: unknown) {
    try {
      await api(`/api/nodes/${path}`, {
        method: "POST",
        ...(body === undefined ? {} : { headers: JSON_HEADERS, body: JSON.stringify(body) }),
      });
      setActionError("");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "action failed");
    }
    await refresh();
  }

  const matching = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return nodes;
    return nodes.filter((n) =>
      [n.hostname, n.compatibility_class, n.gpu_model, n.room ?? ""].some((v) => (v ?? "").toLowerCase().includes(needle)),
    );
  }, [nodes, filter]);
  const pages = Math.max(1, Math.ceil(matching.length / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const pageRows = matching.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  const columns: Column<NodeRow>[] = [
    {
      key: "host",
      header: "Host",
      render: (n) => (
        <>
          <Link href={`/admin/nodes/${n.id}`}>{n.hostname}</Link>
          <div className="muted nodes-sub">
            {n.agent_version}
            {target && n.agent_version !== target ? " (update pending)" : ""} · {n.role || "train"} ·{" "}
            {n.last_heartbeat_at ? <Time iso={n.last_heartbeat_at} prefix="seen " /> : "no heartbeat"}
          </div>
        </>
      ),
    },
    {
      key: "room",
      header: "Room",
      render: (n) => <RoomCell node={n} onSaved={() => void refresh()} onError={setActionError} />,
    },
    {
      key: "gpu",
      header: "GPU",
      render: (n) => (
        <>
          {n.gpu_model}
          <div className="muted nodes-sub">{n.vram_allocatable_mb} MB</div>
        </>
      ),
    },
    { key: "class", header: "Class", className: "mono", render: (n) => n.compatibility_class },
    {
      key: "state",
      header: "State",
      render: (n) => (
        <>
          <span className={`chip ${n.eligibility}`}>{n.eligibility}</span>
          {n.gpu_desktop_busy ? <div className="muted">GPU busy</div> : null}
          {n.eligibility_reason ? <div className="muted">{n.eligibility_reason}</div> : null}
        </>
      ),
    },
    { key: "disk", header: "Disk", className: "muted", render: (n) => fmtBytes(n.disk_free_bytes) },
    {
      key: "ring",
      header: "Ring",
      render: (n) => (
        <select
          aria-label={`Update ring for ${n.hostname}`}
          value={n.update_ring}
          onChange={(e) => void act(`${n.id}/ring`, { ring: Number(e.target.value) })}
        >
          {RINGS.map((r) => (
            <option key={r} value={r}>
              {ringLabel(r)}
            </option>
          ))}
        </select>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (n) => (
        <div className="nodes-actions">
          <button type="button" className="ghost small" onClick={() => void act(`${n.id}/drain`)}>
            Drain
          </button>
          <QuarantineButton node={n} onQuarantine={(reason) => act(`${n.id}/quarantine`, { reason })} />
          {n.eligibility === "draining" || n.eligibility === "quarantined" ? (
            <button type="button" className="ghost small" onClick={() => void act(`${n.id}/release`)}>
              Release
            </button>
          ) : null}
          <details className="danger-menu danger-zone">
            <summary>More</summary>
            <ConfirmButton
              className="ghost danger small"
              message={`Forget ${n.hostname}? Its credential is revoked.`}
              onConfirm={() => act(`${n.id}/forget`)}
            >
              Forget this PC
            </ConfirmButton>
          </details>
        </div>
      ),
    },
  ];

  return (
    <div className="stack">
      <div>
        <h1>Nodes</h1>
        <p className="muted">
          Admin only. Drain finishes the current job then skips claims. Quarantine fences in-flight work and asks for a
          reason. Forget (under More) retires a PC for good; reinstall with --reset-identity to bring it back.
        </p>
        <p className="muted">
          Agent target {target || "unknown"} · rolling out to ring {ringLabel(rolloutRing)} and below
        </p>
      </div>
      {error ? <ErrorBox message={error} onRetry={() => void refresh()} /> : null}
      {actionError ? <ErrorBox message={actionError} /> : null}
      <div className="card filters">
        <label className="toolbar-search" htmlFor="node-filter">
          Filter by hostname, GPU class or room
          <input
            id="node-filter"
            type="search"
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setPage(0);
            }}
            placeholder="e.g. lab-301 or ampere"
            autoComplete="off"
          />
        </label>
        <span className="muted" aria-live="polite">
          {loaded ? `${matching.length} of ${nodes.length} PCs` : ""}
        </span>
      </div>
      {!loaded ? (
        <Loading label="Loading nodes…" />
      ) : (
        <div className="card">
          <DataTable
            columns={columns}
            rows={pageRows}
            rowKey={(n) => n.id}
            caption="Lab nodes"
            empty={
              nodes.length === 0 ? (
                <Empty title="No PCs have registered yet" hint="Install the agent on a lab PC to see it here." />
              ) : (
                <Empty title="No PCs match" hint="Change the filter." />
              )
            }
          />
          {pages > 1 ? (
            <div className="pager">
              <span>
                Page {safePage + 1} of {pages}
              </span>
              <span className="btn-row">
                <button type="button" className="ghost small" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>
                  Previous
                </button>
                <button
                  type="button"
                  className="ghost small"
                  disabled={safePage >= pages - 1}
                  onClick={() => setPage(safePage + 1)}
                >
                  Next
                </button>
              </span>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
