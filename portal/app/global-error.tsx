"use client";

import { useEffect } from "react";
import "./globals.css";
import { reportClientError } from "@/lib/clientErrors";

/** Last-resort boundary for errors in the root layout itself; it replaces the whole document. */
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    reportClientError({ message: error.message, stack: error.stack });
  }, [error]);
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <main id="main" data-testid="error-boundary">
            <div className="stack">
              <h1>Something went wrong</h1>
              <p className="muted">The portal hit an unexpected error. It has been reported.</p>
              {error.digest ? <p className="mono">Error ID: {error.digest}</p> : null}
              <div className="btn-row">
                <button type="button" onClick={reset}>
                  Try again
                </button>
              </div>
            </div>
          </main>
        </div>
      </body>
    </html>
  );
}
