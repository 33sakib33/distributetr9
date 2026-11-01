"use client";

import { useEffect } from "react";
import { reportClientError } from "@/lib/clientErrors";

/** Mounted once in the root layout: forwards uncaught errors and rejections to the server. */
export default function ClientErrorReporter() {
  useEffect(() => {
    function onError(ev: ErrorEvent) {
      const err = ev.error as Error | undefined;
      reportClientError({ message: err?.message || ev.message, stack: err?.stack });
    }
    function onRejection(ev: PromiseRejectionEvent) {
      const reason = ev.reason as unknown;
      if (reason instanceof Error) {
        reportClientError({ message: `Unhandled rejection: ${reason.message}`, stack: reason.stack });
      } else {
        reportClientError({ message: `Unhandled rejection: ${String(reason)}` });
      }
    }
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);
  return null;
}
