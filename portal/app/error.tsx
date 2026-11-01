"use client";

import { useEffect } from "react";
import { ErrorBox } from "@/components/States";
import { reportClientError } from "@/lib/clientErrors";

/** Error boundary for every page below the root layout. */
export default function PageError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    reportClientError({ message: error.message, stack: error.stack });
  }, [error]);
  return (
    <div className="stack" data-testid="error-boundary">
      <h1>Something went wrong</h1>
      <p className="muted">
        This page hit an unexpected error. It has been reported. Try again, and if it keeps happening tell the lab
        admin.
      </p>
      <ErrorBox message={error.message || "Unexpected error"} onRetry={reset} />
      {error.digest ? <p className="muted mono">Error ID: {error.digest}</p> : null}
    </div>
  );
}
