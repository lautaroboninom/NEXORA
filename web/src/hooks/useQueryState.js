// web/src/hooks/useQueryState.js
// Small hook to bind a state value to a querystring key using React Router v6.
// - Keeps the value in sync with URL search params
// - Optionally debounces URL updates (useful for text filters)
// - Uses replace: true to avoid polluting history on each keystroke

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

function identity(x) { return x; }

export default function useQueryState(
  key,
  defaultValue = "",
  options = {}
) {
  const {
    parse = identity,
    serialize = (v) => (v == null ? "" : String(v)),
    debounceMs,
    replace = true,
  } = options || {};

  const [search, setSearch] = useSearchParams();
  const location = useLocation();

  // Default debounce for common text filter key
  const effectiveDebounce = useMemo(() => {
    if (typeof debounceMs === "number") return debounceMs;
    return key === "q" ? 300 : 0;
  }, [debounceMs, key]);

  // Initialize from current search
  const readFromSearch = (sp) => {
    try {
      const raw = sp.get(key);
      if (raw == null) return defaultValue;
      return parse(raw);
    } catch {
      return defaultValue;
    }
  };

  const [value, setValue] = useState(() => readFromSearch(search));
  const lastAppliedRef = useRef(undefined);

  // Keep local state in sync when location.search changes
  useEffect(() => {
    const cur = readFromSearch(search);
    const ser = serialize(cur);
    // The URL only echoes what this hook last wrote (or this key didn't change):
    // local state already has it or is ahead. The router re-renders after each
    // navigate(); a keystroke landing in that gap got overwritten by the stale
    // URL value, so typing "alquilado" dropped letters.
    if (ser === lastAppliedRef.current) return;
    setValue((prev) => (Object.is(prev, cur) ? prev : cur));
    // Track what is currently in URL to avoid redundant sets
    lastAppliedRef.current = ser;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.search]);

  // Helper to push value into URL if it differs
  const applyToSearch = (val) => {
    const ser = serialize(val);
    if (lastAppliedRef.current === ser) return; // already in URL
    // Build on the live URL, not the last rendered one: another key written in
    // this same tick (a saved-filters restore, two filters set together) isn't
    // rendered yet, and a stale base silently dropped it.
    const base = (typeof window !== "undefined" ? window.location.search : location.search) || "";
    const next = new URLSearchParams(base);
    if (ser == null || ser === "") next.delete(key);
    else next.set(key, ser);
    setSearch(next, { replace });
    lastAppliedRef.current = ser;
  };

  // Update URL when value changes (debounced if configured)
  useEffect(() => {
    if (effectiveDebounce && effectiveDebounce > 0) {
      const h = setTimeout(() => applyToSearch(value), effectiveDebounce);
      return () => clearTimeout(h);
    }
    applyToSearch(value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, effectiveDebounce, location.pathname]);

  return [value, setValue];
}
