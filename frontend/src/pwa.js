// Progressive web app: service worker registration and the "Install app" prompt.
import { useEffect, useState } from "react";

export function registerServiceWorker() {
  if (!import.meta.env.PROD || !("serviceWorker" in navigator)) return;    // dev server: no caching surprises
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("Service worker not registered:", e));
  });
}

// Chrome / Edge / Android fire beforeinstallprompt once the app is installable; keep it until the user asks.
let deferred = null;
const listeners = new Set();
const notify = () => listeners.forEach((fn) => fn());
if (typeof window !== "undefined") {
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferred = e; notify(); });
  window.addEventListener("appinstalled", () => { deferred = null; notify(); });
}

const standalone = () =>
  window.matchMedia?.("(display-mode: standalone)").matches || window.navigator.standalone === true;
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

/** { canInstall, iosHint, install } — iosHint: Safari has no install prompt; tell the user how to add it. */
export function useInstallPrompt() {
  const [, force] = useState(0);
  useEffect(() => {
    const fn = () => force((n) => n + 1);
    listeners.add(fn);
    return () => listeners.delete(fn);
  }, []);
  const installed = standalone();
  return {
    canInstall: !installed && !!deferred,
    iosHint: !installed && !deferred && isIOS(),
    async install() {
      if (!deferred) return;
      deferred.prompt();
      await deferred.userChoice.catch(() => null);
      deferred = null;
      notify();
    },
  };
}
