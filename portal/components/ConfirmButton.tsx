"use client";

import { useState, type ReactNode } from "react";

/** Button that asks "Are you sure? Yes / No" inline before running a destructive action. */
export default function ConfirmButton({
  children,
  onConfirm,
  message = "Are you sure?",
  className = "ghost",
  disabled = false,
}: {
  children: ReactNode;
  onConfirm: () => void | Promise<void>;
  message?: string;
  /** Classes for the initial button, e.g. "ghost danger small". */
  className?: string;
  disabled?: boolean;
}) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);

  async function yes() {
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
      setAsking(false);
    }
  }

  if (!asking) {
    return (
      <button type="button" className={className} disabled={disabled} onClick={() => setAsking(true)}>
        {children}
      </button>
    );
  }
  return (
    <span
      className="confirmbutton"
      role="group"
      aria-label={message}
      onKeyDown={(e) => {
        if (e.key === "Escape" && !busy) setAsking(false);
      }}
    >
      <span className="confirmbutton-ask">{message}</span>
      <button type="button" className="danger small" disabled={busy} onClick={yes} autoFocus>
        {busy ? "Working…" : "Yes"}
      </button>
      <button type="button" className="ghost small" disabled={busy} onClick={() => setAsking(false)}>
        No
      </button>
    </span>
  );
}
