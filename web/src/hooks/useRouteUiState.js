import { useCallback, useEffect, useMemo, useRef } from "react";
import { useLocation } from "react-router-dom";

const STORAGE_KEY = "sepid:route-ui-state:v1";
const MAX_ROUTES = 60;
const MAX_CONTROLS_PER_ROUTE = 220;
const SAVE_THROTTLE_MS = 120;
const RESTORE_DELAYS_MS = [0, 90, 260, 700];

function canPersistControls(pathname) {
  if (!pathname) return false;
  if (pathname === "/ingresos" || pathname === "/ingresos/historico") return true;
  if (pathname.startsWith("/ingresos/")) return false;
  return true;
}

function isPersistableControl(element) {
  if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement)) {
    return false;
  }
  if (element.closest("[data-no-route-state='true']")) return false;
  if (element instanceof HTMLInputElement) {
    const type = (element.type || "").toLowerCase();
    if (["button", "submit", "reset", "file", "password", "hidden", "image"].includes(type)) {
      return false;
    }
  }
  return true;
}

function controlFingerprint(element) {
  const tag = element.tagName.toLowerCase();
  const type = element instanceof HTMLInputElement ? (element.type || "").toLowerCase() : "";
  const name = element.getAttribute("name") || "";
  const id = element.id || "";
  const placeholder = element.getAttribute("placeholder") || "";
  const ariaLabel = element.getAttribute("aria-label") || "";
  const dataTestId = element.getAttribute("data-testid") || "";
  return [tag, type, name, id, placeholder, ariaLabel, dataTestId].join("::");
}

function captureControls() {
  const controls = Array.from(document.querySelectorAll("input, select, textarea"))
    .filter(isPersistableControl)
    .slice(0, MAX_CONTROLS_PER_ROUTE);

  const counters = new Map();

  return controls.map((element) => {
    const fp = controlFingerprint(element);
    const index = counters.get(fp) || 0;
    counters.set(fp, index + 1);

    if (element instanceof HTMLInputElement && (element.type === "checkbox" || element.type === "radio")) {
      return { fp, index, kind: "checked", checked: !!element.checked };
    }
    if (element instanceof HTMLSelectElement && element.multiple) {
      return {
        fp,
        index,
        kind: "multi",
        value: Array.from(element.selectedOptions).map((option) => option.value),
      };
    }
    return { fp, index, kind: "value", value: element.value ?? "" };
  });
}

function readStore() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeStore(store) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // Ignore quota/storage errors.
  }
}

function pruneStore(store) {
  const entries = Object.entries(store);
  if (entries.length <= MAX_ROUTES) return;
  entries.sort((a, b) => (a[1]?.updatedAt || 0) - (b[1]?.updatedAt || 0));
  const removeCount = entries.length - MAX_ROUTES;
  for (let i = 0; i < removeCount; i += 1) {
    delete store[entries[i][0]];
  }
}

function setNativeProperty(element, prop, value) {
  const prototype = Object.getPrototypeOf(element);
  const descriptor = prototype ? Object.getOwnPropertyDescriptor(prototype, prop) : null;
  if (descriptor?.set) descriptor.set.call(element, value);
  else element[prop] = value;
}

function restoreControls(controlSnapshots, touchedByUser) {
  if (!Array.isArray(controlSnapshots) || controlSnapshots.length === 0) return;

  const controls = Array.from(document.querySelectorAll("input, select, textarea")).filter(isPersistableControl);
  const grouped = new Map();

  controls.forEach((element) => {
    const fp = controlFingerprint(element);
    const current = grouped.get(fp);
    if (current) current.push(element);
    else grouped.set(fp, [element]);
  });

  controlSnapshots.forEach((snapshot) => {
    const candidates = grouped.get(snapshot.fp);
    const target = candidates?.[snapshot.index];
    if (!target || touchedByUser?.has(target)) return;

    if (snapshot.kind === "checked" && target instanceof HTMLInputElement) {
      const nextChecked = !!snapshot.checked;
      if (target.checked === nextChecked) return;
      setNativeProperty(target, "checked", nextChecked);
      target.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }

    if (snapshot.kind === "multi" && target instanceof HTMLSelectElement && target.multiple) {
      const selectedValues = Array.isArray(snapshot.value) ? new Set(snapshot.value.map(String)) : new Set();
      let changed = false;
      Array.from(target.options).forEach((option) => {
        const shouldBeSelected = selectedValues.has(String(option.value));
        if (option.selected !== shouldBeSelected) {
          option.selected = shouldBeSelected;
          changed = true;
        }
      });
      if (changed) target.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }

    const nextValue = snapshot.value == null ? "" : String(snapshot.value);
    if (target.value === nextValue) return;
    setNativeProperty(target, "value", nextValue);
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function restoreScroll(snapshot) {
  const x = Number(snapshot?.scrollX || 0);
  const y = Number(snapshot?.scrollY || 0);
  window.scrollTo({ left: x, top: y, behavior: "auto" });
}

export default function useRouteUiState() {
  const location = useLocation();
  const saveTimerRef = useRef(null);
  const persistControls = canPersistControls(location.pathname);

  const routeKey = useMemo(() => location.pathname, [location.pathname]);

  const saveNow = useCallback(() => {
    const store = readStore();
    store[routeKey] = {
      updatedAt: Date.now(),
      scrollX: window.scrollX || 0,
      scrollY: window.scrollY || 0,
      controls: persistControls ? captureControls() : [],
    };
    pruneStore(store);
    writeStore(store);
  }, [persistControls, routeKey]);

  const scheduleSave = useCallback(() => {
    if (saveTimerRef.current != null) return;
    saveTimerRef.current = window.setTimeout(() => {
      saveTimerRef.current = null;
      saveNow();
    }, SAVE_THROTTLE_MS);
  }, [saveNow]);

  useEffect(() => {
    const onControlChange = () => scheduleSave();
    const onScroll = () => scheduleSave();

    document.addEventListener("input", onControlChange, true);
    document.addEventListener("change", onControlChange, true);
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("beforeunload", saveNow);

    return () => {
      document.removeEventListener("input", onControlChange, true);
      document.removeEventListener("change", onControlChange, true);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("beforeunload", saveNow);
      if (saveTimerRef.current != null) {
        clearTimeout(saveTimerRef.current);
        saveTimerRef.current = null;
      }
      saveNow();
    };
  }, [saveNow, scheduleSave]);

  useEffect(() => {
    const snapshot = readStore()[routeKey];
    if (!snapshot) return undefined;

    // What the user types after arriving wins: the 260/700 ms retries used to
    // overwrite it with the value saved on the previous visit.
    const touchedByUser = new WeakSet();
    const onUserInput = (event) => {
      if (event.isTrusted && event.target) touchedByUser.add(event.target);
    };
    document.addEventListener("input", onUserInput, true);
    document.addEventListener("change", onUserInput, true);

    const timers = RESTORE_DELAYS_MS.map((delay) =>
      window.setTimeout(() => {
        if (persistControls) restoreControls(snapshot.controls, touchedByUser);
        restoreScroll(snapshot);
      }, delay)
    );

    return () => {
      timers.forEach((timer) => clearTimeout(timer));
      document.removeEventListener("input", onUserInput, true);
      document.removeEventListener("change", onUserInput, true);
    };
  }, [persistControls, routeKey]);
}
