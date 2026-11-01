"use client";

import { useEffect, useState } from "react";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, jsonInit, type NotificationSettings } from "@/lib/api";
import {
  browserNotifyEnabled,
  notificationsSupported,
  setBrowserNotify,
} from "@/lib/browserNotify";

export default function SettingsPage() {
  const [settings, setSettings] = useState<NotificationSettings | null>(null);
  const [loadError, setLoadError] = useState("");
  const [email, setEmail] = useState(true);
  const [webhook, setWebhook] = useState("");
  const [webhookError, setWebhookError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  function load() {
    setLoadError("");
    api("/api/me/notifications")
      .then((data) => {
        const s = data as NotificationSettings;
        setSettings(s);
        setEmail(s.notify_email);
        setWebhook(s.notify_webhook_url ?? "");
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : "Could not load your settings."));
  }

  useEffect(load, []);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaved(false);
    setSaveError("");
    setWebhookError("");
    const url = webhook.trim();
    if (url && !/^https?:\/\/[^\s/]+/i.test(url)) {
      setWebhookError("Use a full address starting with http:// or https://");
      return;
    }
    setSaving(true);
    try {
      const body: Record<string, unknown> = { notify_webhook_url: url };
      if (settings?.email_available) body.notify_email = email;
      const next = (await api("/api/me/notifications", jsonInit("PUT", body))) as NotificationSettings;
      setSettings(next);
      setEmail(next.notify_email);
      setWebhook(next.notify_webhook_url ?? "");
      setSaved(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not save.";
      // The server rejects bad webhook addresses; show that next to the field.
      if (/webhook|url|address|http/i.test(message)) setWebhookError(message);
      else setSaveError(message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="stack">
      <div>
        <h1>Settings</h1>
        <p className="muted">How you hear about finished jobs.</p>
      </div>

      {loadError ? <ErrorBox message={loadError} onRetry={load} /> : null}
      {!settings && !loadError ? <Loading label="Loading your settings…" /> : null}

      {settings ? (
        <form className="card stack" onSubmit={save} noValidate>
          <h2>Job notifications</h2>
          <p className="muted">Sent when one of your jobs completes, fails or is cancelled.</p>
          <label className="check-row">
            <input
              type="checkbox"
              checked={settings.email_available && email}
              disabled={!settings.email_available}
              aria-describedby={settings.email_available ? undefined : "email-help"}
              onChange={(e) => setEmail(e.target.checked)}
            />
            Email me
          </label>
          {!settings.email_available ? (
            <p className="hint" id="email-help">
              Email is not set up on this lab server, so this option is off and unavailable. Ask an admin to configure
              SMTP, or use a webhook or browser notifications.
            </p>
          ) : null}
          <label>
            Webhook URL (optional)
            <input
              type="text"
              inputMode="url"
              value={webhook}
              onChange={(e) => setWebhook(e.target.value)}
              placeholder="https://example.com/hooks/lab"
              aria-invalid={webhookError ? true : undefined}
              aria-describedby="webhook-help"
              autoComplete="off"
            />
          </label>
          <p id="webhook-help" className="muted">
            We POST a small JSON message about the job to this address. Leave it empty to turn the webhook off.
          </p>
          {webhookError ? (
            <p className="field-error" role="alert">
              {webhookError}
            </p>
          ) : null}
          {saveError ? <ErrorBox message={saveError} /> : null}
          <div className="btn-row">
            <button type="submit" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
            {saved ? (
              <span className="ok-note" role="status">
                Saved.
              </span>
            ) : null}
          </div>
        </form>
      ) : null}

      <BrowserSection />
    </div>
  );
}

function BrowserSection() {
  const [supported, setSupported] = useState<boolean | null>(null);
  const [permission, setPermission] = useState<NotificationPermission>("default");
  const [on, setOn] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    const ok = notificationsSupported();
    setSupported(ok);
    if (ok) {
      setPermission(Notification.permission);
      setOn(browserNotifyEnabled());
    }
  }, []);

  async function enable() {
    setNote("");
    try {
      const result = await Notification.requestPermission();
      setPermission(result);
      if (result === "granted") {
        setBrowserNotify(true);
        setOn(true);
      }
    } catch {
      setNote("This browser could not ask for permission.");
    }
  }

  function disable() {
    setBrowserNotify(false);
    setOn(false);
  }

  function test() {
    try {
      new Notification("Lab jobs", { body: "Browser notifications are working.", tag: "lab-test" });
    } catch {
      setNote("This browser could not show a notification.");
    }
  }

  return (
    <div className="card stack">
      <h2>Browser notifications</h2>
      {supported === null ? (
        <Loading label="Checking this browser…" />
      ) : !supported ? (
        <Empty title="Not supported" hint="This browser cannot show notifications." />
      ) : (
        <>
          <p className="muted">
            While a Lab jobs tab is open, this browser tells you when one of your running jobs finishes. It does not
            work when every tab is closed; use email or a webhook for that. This choice is stored in this browser only.
          </p>
          <p>
            Status:{" "}
            <strong>
              {on ? "on" : permission === "denied" ? "blocked by the browser" : "off"}
            </strong>
          </p>
          <div className="btn-row">
            {on ? (
              <>
                <button type="button" className="ghost" onClick={disable}>
                  Turn off
                </button>
                <button type="button" className="ghost" onClick={test}>
                  Send a test
                </button>
              </>
            ) : (
              <button type="button" onClick={() => void enable()} disabled={permission === "denied"}>
                Turn on
              </button>
            )}
          </div>
          {permission === "denied" ? (
            <p className="hint" role="status">
              Notifications are blocked for this site in your browser. To turn them on, open the site settings (click
              the icon left of the address, or the lock, then Site settings or Permissions), set Notifications to
              Allow, then reload this page.
            </p>
          ) : null}
          {note ? <p className="hint">{note}</p> : null}
        </>
      )}
    </div>
  );
}
