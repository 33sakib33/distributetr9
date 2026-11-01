"use client";

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import ConfirmButton from "@/components/ConfirmButton";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, jsonInit, type NodeList, type NodeRow, type ScheduleList, type ScheduleWindow } from "@/lib/api";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOUR_PX = 28;

type Form = {
  kind: "room" | "node";
  room: string;
  nodeId: string;
  days: boolean[];
  start: string;
  end: string;
  toEnd: boolean;
  note: string;
};

const BLANK: Form = {
  kind: "room",
  room: "",
  nodeId: "",
  days: [true, false, false, false, false, false, false],
  start: "09:00",
  end: "13:00",
  toEnd: false,
  note: "",
};

function hhmm(minute: number) {
  const m = Math.min(1440, Math.max(0, minute));
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

function minutes(text: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text);
  if (!m) return null;
  const v = Number(m[1]) * 60 + Number(m[2]);
  return v <= 1439 ? v : null;
}

const HOURS = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, "0"));
const MINUTE_STEPS = Array.from({ length: 12 }, (_, i) => String(i * 5).padStart(2, "0"));

/** 24-hour time as two selects (hour, minute), so the format never depends on the browser locale. */
function TimePick({
  label,
  value,
  onChange,
  disabled = false,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const [h = "00", m = "00"] = /^\d{1,2}:\d{2}$/.test(value) ? value.split(":") : ["00", "00"];
  const hour = h.padStart(2, "0");
  const minutes = MINUTE_STEPS.includes(m) ? MINUTE_STEPS : [...MINUTE_STEPS, m].sort();
  return (
    <div className="field" role="group" aria-labelledby={id}>
      <span id={id}>{label} (24-hour)</span>
      <span className="timepick">
        <select aria-label={`${label} hour`} value={hour} disabled={disabled} onChange={(e) => onChange(`${e.target.value}:${m}`)}>
          {HOURS.map((x) => (
            <option key={x} value={x}>
              {x}
            </option>
          ))}
        </select>
        <span className="timepick-colon" aria-hidden="true">
          :
        </span>
        <select aria-label={`${label} minute`} value={m} disabled={disabled} onChange={(e) => onChange(`${hour}:${e.target.value}`)}>
          {minutes.map((x) => (
            <option key={x} value={x}>
              {x}
            </option>
          ))}
        </select>
      </span>
    </div>
  );
}

type Msg = { ok: boolean; text: string } | null;

function labelFor(key: string, hostnames: Map<string, string>) {
  const [kind, ...rest] = key.split(":");
  const id = rest.join(":");
  return kind === "node" ? `PC ${hostnames.get(id) ?? id.slice(0, 8)}` : `Room ${id}`;
}

function targetKey(w: { room: string | null; node_id: string | null }) {
  return w.node_id ? `node:${w.node_id}` : `room:${w.room ?? ""}`;
}

export default function AdminSchedulesPage() {
  const [data, setData] = useState<ScheduleList | null>(null);
  const [nodes, setNodes] = useState<NodeRow[]>([]);
  const [nodeRooms, setNodeRooms] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [target, setTarget] = useState("");
  const [editing, setEditing] = useState<ScheduleWindow | null>(null);
  const [form, setForm] = useState<Form>(BLANK);
  const [formMsg, setFormMsg] = useState<Msg>(null);
  const [listMsg, setListMsg] = useState<Msg>(null);
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, n] = await Promise.all([
        api("/api/admin/schedules") as Promise<ScheduleList>,
        (api("/api/nodes") as Promise<NodeList>).catch(() => null),
      ]);
      setData(s);
      if (n) {
        setNodes(n.nodes);
        setNodeRooms(n.rooms ?? []);
      }
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load schedules");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const rooms = useMemo(() => {
    const set = new Set<string>(nodeRooms);
    nodes.forEach((n) => n.room && set.add(n.room));
    data?.items.forEach((w) => w.room && set.add(w.room));
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [nodes, nodeRooms, data]);

  const hostnames = useMemo(() => {
    const map = new Map<string, string>();
    nodes.forEach((n) => map.set(n.id, n.hostname));
    data?.items.forEach((w) => w.node_id && w.hostname && map.set(w.node_id, w.hostname));
    return map;
  }, [nodes, data]);

  const targets = useMemo(() => {
    const out: { key: string; label: string; count: number }[] = [];
    const count = (key: string) => data?.items.filter((w) => targetKey(w) === key).length ?? 0;
    rooms.forEach((r) => out.push({ key: `room:${r}`, label: `Room ${r}`, count: count(`room:${r}`) }));
    const nodeIds = new Set<string>(nodes.map((n) => n.id));
    data?.items.forEach((w) => w.node_id && nodeIds.add(w.node_id));
    Array.from(nodeIds)
      .sort((a, b) => (hostnames.get(a) ?? a).localeCompare(hostnames.get(b) ?? b))
      .forEach((id) =>
        out.push({ key: `node:${id}`, label: `PC ${hostnames.get(id) ?? id.slice(0, 8)}`, count: count(`node:${id}`) }),
      );
    // The current target stays listed even when it has no window and no known PC any more.
    if (target && !out.some((t) => t.key === target)) out.push({ key: target, label: labelFor(target, hostnames), count: 0 });
    return out;
  }, [rooms, nodes, data, hostnames, target]);

  // Pick a first target once the lists are in: the first room, else the first PC that is online.
  useEffect(() => {
    if (target || !data || targets.length === 0) return;
    const room = targets.find((t) => t.key.startsWith("room:"));
    const online = nodes.find((n) => n.eligibility !== "offline");
    const first = room ?? targets.find((t) => online && t.key === `node:${online.id}`) ?? targets[0];
    setTarget(first.key);
  }, [target, data, targets, nodes]);

  // A new target resets the form to that target (unless an edit is in progress).
  useEffect(() => {
    if (editing || !target) return;
    const [kind, ...rest] = target.split(":");
    const id = rest.join(":");
    setForm((f) => (kind === "room" ? { ...f, kind: "room", room: id } : { ...f, kind: "node", nodeId: id }));
  }, [target, editing]);

  const windows = useMemo(
    () =>
      (data?.items ?? [])
        .filter((w) => targetKey(w) === target)
        .sort((a, b) => a.weekday - b.weekday || a.start_minute - b.start_minute),
    [data, target],
  );

  function startEdit(w: ScheduleWindow) {
    setEditing(w);
    setFormMsg(null);
    setListMsg(null);
    setForm({
      kind: w.node_id ? "node" : "room",
      room: w.room ?? "",
      nodeId: w.node_id ?? "",
      days: DAYS.map((_, i) => i === w.weekday),
      start: hhmm(w.start_minute),
      end: w.end_minute >= 1440 ? "23:59" : hhmm(w.end_minute),
      toEnd: w.end_minute >= 1440,
      note: w.note ?? "",
    });
  }

  function cancelEdit() {
    setEditing(null);
    setFormMsg(null);
    const [kind, ...rest] = target.split(":");
    const id = rest.join(":");
    setForm({ ...BLANK, kind: kind === "node" ? "node" : "room", room: kind === "room" ? id : "", nodeId: kind === "node" ? id : "" });
  }

  function toggleDay(i: number) {
    setForm((f) => {
      if (editing) return { ...f, days: DAYS.map((_, d) => d === i) };
      const days = f.days.slice();
      days[i] = !days[i];
      return { ...f, days };
    });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setFormMsg(null);
    setListMsg(null);
    const fail = (text: string) => setFormMsg({ ok: false, text });
    const start = minutes(form.start);
    const end = form.toEnd ? 1440 : minutes(form.end);
    const room = form.room.trim();
    if (form.kind === "room" && !room) return fail("Enter a room name.");
    if (form.kind === "node" && !form.nodeId) return fail("Choose a PC.");
    const days = form.days.map((on, i) => (on ? i : -1)).filter((i) => i >= 0);
    if (days.length === 0) return fail("Choose at least one day.");
    if (start === null || end === null) return fail("Choose the start and end time.");
    if (end <= start) {
      return fail("A window must end after it starts and cannot cross midnight. Add one window per day instead.");
    }
    const where = form.kind === "room" ? { room } : { node_id: form.nodeId };
    setSaving(true);
    try {
      if (editing) {
        await api(
          `/api/admin/schedules/${editing.id}`,
          jsonInit("PATCH", { ...where, weekday: days[0], start_minute: start, end_minute: end, note: form.note.trim() || null }),
        );
        setFormMsg({ ok: true, text: "Window saved." });
        setEditing(null);
      } else {
        let made = 0;
        try {
          for (const weekday of days) {
            await api(
              "/api/admin/schedules",
              jsonInit("POST", { ...where, weekday, start_minute: start, end_minute: end, note: form.note.trim() || null }),
            );
            made += 1;
          }
        } finally {
          if (made) setFormMsg({ ok: true, text: `Added ${made} window${made === 1 ? "" : "s"}.` });
        }
      }
      setTarget(form.kind === "room" ? `room:${room}` : `node:${form.nodeId}`);
      await refresh();
    } catch (err) {
      setFormMsg({ ok: false, text: err instanceof Error ? err.message : "Could not save the window." });
      await refresh();
    } finally {
      setSaving(false);
    }
  }

  async function remove(w: ScheduleWindow, where: "form" | "list" = "list") {
    const say = where === "form" ? setFormMsg : setListMsg;
    try {
      await api(`/api/admin/schedules/${w.id}`, { method: "DELETE" });
      say({ ok: true, text: "Window deleted." });
      if (editing?.id === w.id) setEditing(null);
    } catch (err) {
      say({ ok: false, text: err instanceof Error ? err.message : "Could not delete the window." });
    }
    await refresh();
  }

  const columns: Column<ScheduleWindow>[] = [
    { key: "day", header: "Day", render: (w) => DAYS[w.weekday] },
    { key: "time", header: "Time", render: (w) => <span className="mono tnum">{w.start}–{w.end}</span> },
    { key: "note", header: "Note", render: (w) => w.note ?? <span className="muted">—</span> },
    {
      key: "actions",
      header: "",
      render: (w) => (
        <div className="nodes-actions">
          <button type="button" className="ghost small" onClick={() => startEdit(w)}>
            Edit
          </button>
          <ConfirmButton className="ghost danger small" message="Delete this window?" onConfirm={() => remove(w)}>
            Delete
          </ConfirmButton>
        </div>
      ),
    },
  ];

  if (data === null) {
    return (
      <div className="stack">
        <h1>Schedules</h1>
        {error ? <ErrorBox message={error} onRetry={() => void refresh()} /> : <Loading label="Loading schedules…" />}
      </div>
    );
  }

  const targetLabel = targets.find((t) => t.key === target)?.label ?? (target ? labelFor(target, hostnames) : "");

  return (
    <div className="stack">
      <div>
        <h1>Schedules</h1>
        <p className="muted">
          Windows when a room or PC is closed to student jobs, for example class hours. Times are in{" "}
          <strong>{data.timezone}</strong>. A window cannot cross midnight, so add one per day.
        </p>
      </div>
      {error ? <ErrorBox message={`Could not refresh: ${error}`} onRetry={() => void refresh()} /> : null}

      <div className="card stack">
        <label>
          Show windows for
          <select value={target} onChange={(e) => setTarget(e.target.value)} disabled={targets.length === 0}>
            {targets.length === 0 ? <option value="">No rooms or PCs yet</option> : null}
            {targets.map((t) => (
              <option key={t.key} value={t.key}>
                {t.label} ({t.count} window{t.count === 1 ? "" : "s"})
              </option>
            ))}
          </select>
        </label>
        {target ? (
          <div className="sched-wrap">
            <div className="sched-grid" role="group" aria-label={`Weekly closed windows for ${targetLabel}`}>
              <div className="sched-head" />
              {DAYS.map((d) => (
                <div key={d} className="sched-head">
                  {d}
                </div>
              ))}
              <div className="sched-hours" aria-hidden="true">
                {Array.from({ length: 24 }, (_, i) => i).map((h) => (
                  <span key={h} className={`sched-hour${h === 0 ? " first" : ""}`} style={{ top: h * HOUR_PX }}>
                    {String(h).padStart(2, "0")}:00
                  </span>
                ))}
              </div>
              {DAYS.map((d, wd) => (
                <div key={d} className="sched-day">
                  {windows
                    .filter((w) => w.weekday === wd)
                    .map((w) => (
                      <button
                        key={w.id}
                        type="button"
                        className={`sched-block${editing?.id === w.id ? " editing" : ""}`}
                        style={{
                          top: (w.start_minute / 60) * HOUR_PX,
                          height: Math.max(18, ((w.end_minute - w.start_minute) / 60) * HOUR_PX - 1),
                        }}
                        title={`${d} ${w.start}–${w.end}${w.note ? ` · ${w.note}` : ""}`}
                        aria-label={`${d} ${w.start} to ${w.end}${w.note ? `, ${w.note}` : ""}. Edit`}
                        onClick={() => startEdit(w)}
                      >
                        {w.start}–{w.end}
                        {w.note ? <span className="sched-block-note">{w.note}</span> : null}
                      </button>
                    ))}
                </div>
              ))}
            </div>
          </div>
        ) : (
          <Empty title="Nothing to schedule yet" hint="Rooms and PCs appear here once a lab PC has registered." />
        )}
      </div>

      <datalist id="sched-rooms">
        {rooms.map((r) => (
          <option key={r} value={r} />
        ))}
      </datalist>

      <form className="card stack" onSubmit={submit}>
        <div className="row">
          <h3 className="dash-h">{editing ? "Edit window" : "Add windows"}</h3>
          {editing ? (
            <button type="button" className="ghost small" onClick={cancelEdit}>
              Cancel edit
            </button>
          ) : null}
        </div>
        <div className="form-grid">
          <label>
            Applies to
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as "room" | "node" })}>
              <option value="room">A room</option>
              <option value="node">One PC</option>
            </select>
          </label>
          {form.kind === "room" ? (
            <label>
              Room
              <input
                list="sched-rooms"
                value={form.room}
                onChange={(e) => setForm({ ...form, room: e.target.value })}
                maxLength={60}
                placeholder={rooms[0] ?? "e.g. Lab 301"}
                autoComplete="off"
              />
            </label>
          ) : (
            <label>
              PC
              <select value={form.nodeId} onChange={(e) => setForm({ ...form, nodeId: e.target.value })}>
                <option value="">Choose a PC…</option>
                {Array.from(hostnames.entries())
                  .sort((a, b) => a[1].localeCompare(b[1]))
                  .map(([id, name]) => (
                    <option key={id} value={id}>
                      {name}
                    </option>
                  ))}
              </select>
            </label>
          )}
          <TimePick label="Start" value={form.start} onChange={(v) => setForm({ ...form, start: v })} />
          <TimePick label="End" value={form.end} disabled={form.toEnd} onChange={(v) => setForm({ ...form, end: v })} />
          <label className="label-inline">
            <input type="checkbox" checked={form.toEnd} onChange={(e) => setForm({ ...form, toEnd: e.target.checked })} />
            Until midnight (24:00)
          </label>
          <label className="wide">
            Note (optional)
            <input value={form.note} maxLength={200} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="CS101 lab" />
          </label>
        </div>
        <fieldset className="checks plain-fieldset">
          <legend className="muted">{editing ? "Day" : "Days"}</legend>
          {DAYS.map((d, i) => (
            <label key={d}>
              <input type="checkbox" checked={form.days[i]} onChange={() => toggleDay(i)} />
              {d}
            </label>
          ))}
        </fieldset>
        <div className="nodes-actions">
          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : editing ? "Save window" : "Add windows"}
          </button>
          {editing ? (
            <ConfirmButton className="ghost danger" message="Delete this window?" onConfirm={() => remove(editing, "form")}>
              Delete
            </ConfirmButton>
          ) : null}
        </div>
        {formMsg ? (
          <p className={formMsg.ok ? "msg-ok" : "msg-err"} role={formMsg.ok ? "status" : "alert"}>
            {formMsg.text}
          </p>
        ) : null}
      </form>

      {target ? (
        <div className="card stack">
          <h3 className="dash-h">{targetLabel}: windows</h3>
          {listMsg ? (
            <p className={listMsg.ok ? "msg-ok" : "msg-err"} role={listMsg.ok ? "status" : "alert"}>
              {listMsg.text}
            </p>
          ) : null}
          <DataTable
            columns={columns}
            rows={windows}
            rowKey={(w) => w.id}
            caption={`Closed windows for ${targetLabel}`}
            empty={<Empty title="No windows" hint="Student jobs can run at any time on this target." />}
          />
        </div>
      ) : null}
    </div>
  );
}
