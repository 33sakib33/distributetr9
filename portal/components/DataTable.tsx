"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

export type Column<T> = {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  className?: string;
  align?: "left" | "right" | "center";
};

type Props<T> = {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  /** Show checkboxes. Selection is controlled when `selected` is given, otherwise internal. */
  selectable?: boolean;
  selected?: string[];
  onSelectionChange?: (keys: string[]) => void;
  hasMore?: boolean;
  onLoadMore?: () => void;
  loading?: boolean;
  /** Shown instead of the table when there are no rows. */
  empty?: ReactNode;
  caption?: string;
  rowClassName?: (row: T) => string | undefined;
};

function alignClass(a?: "left" | "right" | "center") {
  return a === "right" ? "datatable-right" : a === "center" ? "datatable-center" : "";
}

export default function DataTable<T>({
  columns,
  rows,
  rowKey,
  selectable = false,
  selected,
  onSelectionChange,
  hasMore = false,
  onLoadMore,
  loading = false,
  empty,
  caption,
  rowClassName,
}: Props<T>) {
  const [inner, setInner] = useState<string[]>([]);
  const chosen = new Set(selected ?? inner);
  const keys = rows.map(rowKey);
  const allOn = keys.length > 0 && keys.every((k) => chosen.has(k));
  const someOn = keys.some((k) => chosen.has(k));
  const master = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (master.current) master.current.indeterminate = someOn && !allOn;
  }, [someOn, allOn]);

  function commit(next: Set<string>) {
    const list = Array.from(next);
    if (selected === undefined) setInner(list);
    onSelectionChange?.(list);
  }
  function toggle(key: string) {
    const next = new Set(chosen);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    commit(next);
  }
  function toggleAll() {
    const next = new Set(chosen);
    if (allOn) keys.forEach((k) => next.delete(k));
    else keys.forEach((k) => next.add(k));
    commit(next);
  }

  if (!rows.length && !loading && empty) return <>{empty}</>;

  return (
    <div>
      <div className="datatable-wrap">
        <table className="datatable">
          {caption ? <caption className="sr-only">{caption}</caption> : null}
          <thead>
            <tr>
              {selectable ? (
                <th className="datatable-select">
                  <input
                    ref={master}
                    type="checkbox"
                    aria-label="Select all rows"
                    checked={allOn}
                    onChange={toggleAll}
                  />
                </th>
              ) : null}
              {columns.map((c) => (
                <th key={c.key} className={[alignClass(c.align), c.className].filter(Boolean).join(" ")}>
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const k = rowKey(row);
              const on = chosen.has(k);
              const cls = [on ? "datatable-row-selected" : "", rowClassName?.(row) ?? ""].filter(Boolean).join(" ");
              return (
                <tr key={k} className={cls || undefined}>
                  {selectable ? (
                    <td className="datatable-select">
                      <input type="checkbox" aria-label="Select row" checked={on} onChange={() => toggle(k)} />
                    </td>
                  ) : null}
                  {columns.map((c) => (
                    <td key={c.key} className={[alignClass(c.align), c.className].filter(Boolean).join(" ")}>
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {selectable && someOn ? (
        <div className="datatable-footer" aria-live="polite">
          <span>{chosen.size} selected</span>
        </div>
      ) : null}
      {hasMore ? (
        <div className="datatable-more">
          <button type="button" className="ghost" onClick={onLoadMore} disabled={loading}>
            {loading ? "Loading…" : "Load more"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
