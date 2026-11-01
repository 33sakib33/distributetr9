"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Empty, ErrorBox, Loading } from "@/components/States";
import {
  api,
  type AdminJob,
  type AdminJobList,
  type AlertGroup,
  type AlertList,
  type NodeList,
  type NodeRow,
} from "@/lib/api";
import Time from "@/components/Time";

type TileState = "busy" | "idle" | "offline" | "quarantined" | "draining";

const ACTIVE = "starting,running,uploading,unknown,cancelling";
const WAITING = "queued,building";
const PAGE_LIMIT = 200;
const MAX_PAGES = 3;
const TOP_CLASSES = 10;
const TILE_CAP = 48;

/** Fetch up to a few pages of /admin/jobs; `more` is true when the list was cut off. */
async function fetchJobs(status: string): Promise<{ items: AdminJob[]; more: boolean }> {
  const items: AdminJob[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < MAX_PAGES; page++) {
    const qs = new URLSearchParams({ status, limit: String(PAGE_LIMIT) });
    if (cursor) qs.set("cursor", cursor);
    const data = (await api(`/api/admin/jobs?${qs}`)) as AdminJobList;
    items.push(...data.items);
    cursor = data.next_cursor;
    if (!cursor) return { items, more: false };
  }
  return { items, more: true };
}

function tileState(n: NodeRow, busy: boolean): TileState {
  if (n.eligibility === "offline") return "offline";
  if (n.eligibility === "quarantined") return "quarantined";
  if (n.eligibility === "draining") return "draining";
  return busy ? "busy" : "idle";
}

/** "GPU model · class", leaving out a part that repeats the host name or the other part. */
function identity(n: NodeRow): string {
  const seen = new Set([n.hostname.trim().toLowerCase()]);
  const parts: string[] = [];
  for (const part of [n.gpu_model, n.compatibility_class]) {
    const text = (part ?? "").trim();
    if (!text || seen.has(text.toLowerCase())) continue;
    seen.add(text.toLowerCase());
    parts.push(text);
  }
  return parts.join(" · ");
}

function classOf(job: AdminJob): string {
  const c = job.resources?.gpu_class;
  return typeof c === "string" && c ? c : "any";
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className={`dash-stat ${tone ?? ""}`}>
      <div className="stat-value tnum">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export default function AdminDashboard() {
  const [nodes, setNodes] = useState<NodeRow[] | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [active, setActive] = useState<AdminJob[]>([]);
  const [waiting, setWaiting] = useState<{ items: AdminJob[]; more: boolean }>({ items: [], more: false });
  const [groups, setGroups] = useState<AlertGroup[]>([]);
  const [error, setError] = useState("");
  const [sideErrors, setSideErrors] = useState<string[]>([]);
  const [allClasses, setAllClasses] = useState(false);
  const [expandedRooms, setExpandedRooms] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    const [n, a, q, al] = await Promise.allSettled([
      api("/api/nodes") as Promise<NodeList>,
      fetchJobs(ACTIVE),
      fetchJobs(WAITING),
      api("/api/admin/alerts") as Promise<AlertList>,
    ]);
    if (n.status === "rejected") {
      setError(n.reason instanceof Error ? n.reason.message : "admin required");
      return;
    }
    setError("");
    setNodes(n.value.nodes);
    setTarget(n.value.agent_target_version);
    const side: string[] = [];
    if (a.status === "fulfilled") setActive(a.value.items);
    else side.push("running jobs");
    if (q.status === "fulfilled") setWaiting(q.value);
    else side.push("queue");
    if (al.status === "fulfilled") setGroups(al.value.groups);
    else side.push("alerts");
    setSideErrors(side);
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 5000);
    return () => clearInterval(t);
  }, [refresh]);

  if (error && nodes === null) {
    return (
      <div className="stack">
        <h1>Lab dashboard</h1>
        <ErrorBox message={error} onRetry={() => void refresh()} />
      </div>
    );
  }
  if (nodes === null) {
    return (
      <div className="stack">
        <h1>Lab dashboard</h1>
        <Loading label="Loading the lab…" />
      </div>
    );
  }

  // The job a PC is running right now, by node id.
  const jobByNode = new Map<string, AdminJob>();
  for (const j of active) {
    const id = j.latest_attempt?.node_id;
    if (id && !jobByNode.has(id)) jobByNode.set(id, j);
  }
  // GET /nodes says which PCs are busy and what their GPUs are doing, in one request.
  const isBusy = (n: NodeRow) => n.busy ?? jobByNode.has(n.id);
  const states = new Map(nodes.map((n) => [n.id, tileState(n, isBusy(n))]));
  const count = (s: TileState) => nodes.filter((n) => states.get(n.id) === s).length;
  const offline = count("offline");
  const updating = target ? nodes.filter((n) => n.eligibility !== "offline" && n.agent_version !== target).length : 0;

  // Rooms alphabetically, PCs without one last.
  const byRoom = new Map<string, NodeRow[]>();
  for (const n of nodes) {
    const key = n.room?.trim() || "";
    byRoom.set(key, [...(byRoom.get(key) ?? []), n]);
  }
  const roomNames = Array.from(byRoom.keys()).sort((a, b) =>
    a === "" ? 1 : b === "" ? -1 : a.localeCompare(b, undefined, { numeric: true }),
  );

  // Waiting jobs per GPU class, longest queue first.
  const perClass = new Map<string, number>();
  for (const j of waiting.items) perClass.set(classOf(j), (perClass.get(classOf(j)) ?? 0) + 1);
  const queueRows = Array.from(perClass.entries()).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

  const openAlerts = groups.reduce((sum, g) => sum + g.count, 0);
  const unhandled = groups.reduce((sum, g) => sum + g.unhandled, 0);

  return (
    <div className="stack">
      <div>
        <h1>Lab dashboard</h1>
        <p className="muted">Refreshes every 5 seconds. Agent target {target || "unknown"}.</p>
      </div>
      {error ? <ErrorBox message={`Could not refresh: ${error}`} onRetry={() => void refresh()} /> : null}
      {sideErrors.length ? <p className="err" role="alert">Could not load: {sideErrors.join(", ")}.</p> : null}

      <div className="card dash-stats">
        <Stat label="Online" value={nodes.length - offline} />
        <Stat label="Busy" value={count("busy")} tone="run" />
        <Stat label="Idle" value={count("idle")} tone="ok" />
        <Stat label="Offline" value={offline} />
        <Stat label="Quarantined" value={count("quarantined")} tone={count("quarantined") ? "bad" : ""} />
        <Stat label="Draining" value={count("draining")} />
        <Stat label="Updating" value={updating} tone={updating ? "warn" : ""} />
      </div>

      <div className="grid2">
        <div className="card">
          <h3 className="dash-h">Waiting jobs by GPU class</h3>
          {queueRows.length === 0 ? (
            <p className="muted">Nothing is waiting.</p>
          ) : (
            <ul className="list-plain">
              {(allClasses ? queueRows : queueRows.slice(0, TOP_CLASSES)).map(([cls, n]) => (
                <li key={cls}>
                  <Link href={`/admin/jobs?status=queued,building&gpu_class=${encodeURIComponent(cls)}`} className="mono">
                    {cls}
                  </Link>
                  <span className="tnum">
                    {n}
                    {waiting.more ? "+" : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {queueRows.length > TOP_CLASSES ? (
            <button type="button" className="ghost small show-all" onClick={() => setAllClasses((v) => !v)}>
              {allClasses ? `Show top ${TOP_CLASSES}` : `Show all ${queueRows.length} classes`}
            </button>
          ) : null}
        </div>
        <div className="card">
          <div className="row">
            <h3 className="dash-h">Alerts</h3>
            <Link href="/admin/alerts">Open alerts</Link>
          </div>
          {openAlerts === 0 ? (
            <p className="muted">No open alerts.</p>
          ) : (
            <>
              <p>
                {openAlerts} open, {unhandled} not yet handled
              </p>
              <ul className="list-plain">
                {groups
                  .filter((g) => g.count > 0)
                  .map((g) => (
                    <li key={g.kind}>
                      <span className={`chip sev-${g.severity}`}>{g.severity}</span>
                      <Link href="/admin/alerts">{g.kind.replace(/_/g, " ")}</Link>
                      <span className="tnum">{g.count}</span>
                    </li>
                  ))}
              </ul>
            </>
          )}
        </div>
      </div>

      {nodes.length === 0 ? (
        <Empty title="No PCs have registered yet" hint="Install the agent on a lab PC to see it here." />
      ) : (
        roomNames.map((room) => (
          <section key={room || "unassigned"} aria-label={room || "Unassigned"}>
            <h2 className="dash-room">
              {room || "Unassigned"} <span className="muted">({byRoom.get(room)?.length})</span>
            </h2>
            <div className="tiles">
              {(expandedRooms[room] ? byRoom.get(room) : byRoom.get(room)?.slice(0, TILE_CAP))?.map((n) => {
                const state = states.get(n.id) ?? "idle";
                const job = jobByNode.get(n.id);
                const pct = n.gpu_pct != null ? Math.round(n.gpu_pct) : null;
                const running = n.current_attempt;
                const pending = target && n.agent_version !== target && n.eligibility !== "offline";
                return (
                  <div key={n.id} className={`tile tile-${state}`}>
                    <div className="row">
                      <Link href={`/admin/nodes/${n.id}`} className="tile-host">
                        {n.hostname}
                      </Link>
                      <span className={`chip ${state === "busy" ? "running" : state === "idle" ? "eligible" : state}`}>
                        {state}
                      </span>
                    </div>
                    {identity(n) ? <div className="muted tile-sub">{identity(n)}</div> : null}
                    {state === "offline" ? (
                      <div className="muted tile-sub">
                        <Time iso={n.last_heartbeat_at} prefix="seen " />
                      </div>
                    ) : pct != null || n.gpu_desktop_busy ? (
                      <div className="tile-sub">
                        {pct != null ? `GPU ${pct}%` : null}
                        {n.gpu_desktop_busy ? (
                          <span className="muted">
                            {pct != null ? " · " : ""}desktop in use
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                    {job ? (
                      <div className="tile-sub">
                        <Link href={`/jobs/${job.id}`}>
                          {job.entrypoint} <span className="muted">({job.user_email})</span>
                        </Link>
                      </div>
                    ) : running ? (
                      <div className="tile-sub">
                        <Link href={`/jobs/${running.job_id}`}>Running job</Link>
                      </div>
                    ) : null}
                    {pending ? <div className="hint tile-sub">update pending ({n.agent_version})</div> : null}
                    {n.eligibility_reason && state !== "idle" && state !== "busy" ? (
                      <div className="muted tile-sub">{n.eligibility_reason}</div>
                    ) : null}
                  </div>
                );
              })}
            </div>
            {(byRoom.get(room)?.length ?? 0) > TILE_CAP ? (
              <button
                type="button"
                className="ghost small show-all"
                onClick={() => setExpandedRooms((cur) => ({ ...cur, [room]: !cur[room] }))}
              >
                {expandedRooms[room] ? `Show first ${TILE_CAP}` : `Show all ${byRoom.get(room)?.length} PCs`}
              </button>
            ) : null}
          </section>
        ))
      )}
    </div>
  );
}
