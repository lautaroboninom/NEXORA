// web/src/hooks/useDebouncedValue.js
// Returns a debounced copy of `value` that only updates `delayMs` after the
// last change. Used to avoid firing network requests / re-renders on every
// keystroke while keeping the input's own value immediate/responsive.

import { useEffect, useState } from "react";

export default function useDebouncedValue(value, delayMs = 300) {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const h = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(h);
  }, [value, delayMs]);

  return debounced;
}
