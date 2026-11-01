export type StageState = "pending" | "active" | "done" | "failed" | "skipped";

export type Stage = {
  name: string;
  label: string;
  state: StageState;
  detail?: string;
  progress?: { done: number; total: number; unit?: string };
};

const MARK: Record<StageState, string> = {
  pending: "",
  active: "",
  done: "✓",
  failed: "!",
  skipped: "–",
};

const SR: Record<StageState, string> = {
  pending: "pending",
  active: "in progress",
  done: "done",
  failed: "failed",
  skipped: "skipped",
};

/** Horizontal stage timeline. The active stage shows a progress bar when `progress` is set. */
export default function StageBar({ stages }: { stages: Stage[] }) {
  return (
    <ol className="stagebar" aria-label="Job stages">
      {stages.map((s) => {
        const p = s.progress;
        const pct = p && p.total > 0 ? Math.min(100, Math.max(0, (p.done / p.total) * 100)) : null;
        return (
          <li
            key={s.name}
            className={`stagebar-stage ${s.state}`}
            aria-current={s.state === "active" ? "step" : undefined}
          >
            <span className="stagebar-dot" aria-hidden="true">
              {MARK[s.state]}
            </span>
            <div className="stagebar-label">
              {s.label}
              <span className="sr-only"> ({SR[s.state]})</span>
            </div>
            {s.detail ? <div className="stagebar-detail">{s.detail}</div> : null}
            {s.state === "active" && p ? (
              <div className="stagebar-progress">
                {pct !== null ? (
                  <div
                    className="stagebar-track"
                    role="progressbar"
                    aria-label={`${s.label} progress`}
                    aria-valuemin={0}
                    aria-valuemax={p.total}
                    aria-valuenow={Math.min(p.done, p.total)}
                  >
                    <div className="stagebar-fill" style={{ width: `${pct}%` }} />
                  </div>
                ) : null}
                <div className="stagebar-progress-text">
                  {p.done} / {p.total}
                  {p.unit ? ` ${p.unit}` : ""}
                </div>
              </div>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}
