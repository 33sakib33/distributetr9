import type { ReactNode } from "react";
import { requestIdFor } from "@/lib/api";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="states-loading" role="status" aria-live="polite">
      <div className="states-spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: ReactNode; action?: ReactNode }) {
  return (
    <div className="states-empty">
      <div className="states-empty-title">{title}</div>
      {hint ? <div className="states-empty-hint">{hint}</div> : null}
      {action ? <div>{action}</div> : null}
    </div>
  );
}

export function ErrorBox({
  message,
  requestId,
  onRetry,
}: {
  message: string;
  requestId?: string | null;
  onRetry?: () => void;
}) {
  const shownId = requestId ?? requestIdFor(message);
  return (
    <div className="states-error" role="alert">
      <div>{message}</div>
      {shownId ? <div className="states-error-id mono">Request ID: {shownId}</div> : null}
      {onRetry ? (
        <button type="button" className="ghost" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}
