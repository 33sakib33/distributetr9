// Opt-in desktop notifications for finished jobs (the Notification API).
// The choice lives in localStorage on this browser only.

const KEY = "lab.browserNotify";

export function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function browserNotifyEnabled(): boolean {
  if (!notificationsSupported() || Notification.permission !== "granted") return false;
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function setBrowserNotify(on: boolean) {
  try {
    if (on) window.localStorage.setItem(KEY, "1");
    else window.localStorage.removeItem(KEY);
  } catch {
    // Storage can be blocked; the setting then lasts only until the page closes.
  }
  window.dispatchEvent(new Event("lab-notify-changed"));
}
