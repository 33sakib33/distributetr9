"use client";

import { useEffect, useId, useRef, useState } from "react";
import { api, type AdminProject, type Page } from "@/lib/api";

const optionLabel = (p: AdminProject) => `${p.name} (${p.created_by_email})`;

/**
 * Searchable project picker for admins, backed by GET /admin/projects: one
 * text box with a suggestion list. Type to narrow the list (searched on the
 * server by name) and choose a suggestion; clearing the box means any project.
 */
export default function ProjectPicker({
  value,
  onChange,
  label = "Project",
}: {
  value: string;
  onChange: (id: string) => void;
  label?: string;
}) {
  const uid = useId();
  const [text, setText] = useState("");
  const [items, setItems] = useState<AdminProject[]>([]);
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);
  // Labels seen so far, so a chosen project keeps its name while the list is filtered.
  const labels = useRef(new Map<string, string>());
  const searching = useRef(false);

  useEffect(() => {
    let stopped = false;
    const chosen = !!value && text === labels.current.get(value);
    const query = searching.current && !chosen ? text.trim() : "";
    const timer = setTimeout(() => {
      const qs = new URLSearchParams({ limit: "50" });
      if (query) qs.set("q", query);
      api(`/api/admin/projects?${qs}`)
        .then((data) => {
          if (stopped) return;
          const page = data as Page<AdminProject>;
          page.items.forEach((p) => labels.current.set(p.id, optionLabel(p)));
          setItems(page.items);
          setMore(page.next_cursor !== null);
          setError("");
          // A project chosen from the address (?project_id=) shows its name once known.
          if (value && !searching.current) setText((cur) => cur || labels.current.get(value) || "");
        })
        .catch((err) => {
          if (!stopped) setError(err instanceof Error ? err.message : "Could not load projects.");
        });
    }, query ? 250 : 0);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, value]);

  // The value was cleared from outside (Clear filters).
  useEffect(() => {
    if (!value) {
      searching.current = false;
      setText("");
    }
  }, [value]);

  function onInput(next: string) {
    searching.current = true;
    setText(next);
    if (!next.trim()) {
      onChange("");
      return;
    }
    const hit = items.find((p) => optionLabel(p) === next);
    if (hit) {
      labels.current.set(hit.id, next);
      onChange(hit.id);
    } else if (value) {
      // Typing after a choice means a new search; the old choice no longer applies.
      onChange("");
    }
  }

  const listId = `${uid}-projects`;
  return (
    <div className="project-picker">
      <label htmlFor={`${uid}-input`}>
        {label}
        <input
          id={`${uid}-input`}
          type="search"
          list={listId}
          value={text}
          onChange={(e) => onInput(e.target.value)}
          placeholder="Any project; type to search"
          autoComplete="off"
        />
      </label>
      <datalist id={listId}>
        {items.map((p) => (
          <option key={p.id} value={optionLabel(p)} />
        ))}
      </datalist>
      {more ? <span className="muted nodes-sub">More projects match; type to narrow the list.</span> : null}
      {error ? (
        <span className="field-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
