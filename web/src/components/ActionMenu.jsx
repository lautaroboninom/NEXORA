import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// Menú de acciones "⋮" compartido.
//
// El panel se monta con portal y `position: fixed` porque dentro de una tabla con
// overflow (DesktopTableWrap) un panel absoluto queda recortado. Cierra por click
// afuera, por Escape, y se reposiciona en resize/scroll.

const TONE_CLASSES = {
  default: "text-gray-700 hover:bg-gray-50",
  danger: "text-red-700 hover:bg-red-50",
  success: "text-emerald-700 hover:bg-emerald-50",
  warning: "text-amber-700 hover:bg-amber-50",
  info: "text-sky-700 hover:bg-sky-50",
  accent: "text-indigo-700 hover:bg-indigo-50",
};

const toneClass = (tone) => TONE_CLASSES[tone] || TONE_CLASSES.default;

const DISABLED_CLASSES = "disabled:opacity-50 disabled:cursor-not-allowed";

/**
 * Renderiza los mismos descriptores como botones planos, para el layout mobile.
 * @param {{items: Array, onSelect: Function, className?: string}} props
 */
export function ActionButtons({ items = [], onSelect, className = "" }) {
  if (!items.length) return null;
  return (
    <div className={`grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 ${className}`.trim()}>
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          className={`rounded border px-3 py-2 text-sm ${toneClass(item.tone)} ${DISABLED_CLASSES}`}
          onClick={() => onSelect?.(item.key)}
          disabled={Boolean(item.disabled)}
          title={item.title || undefined}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

/**
 * @param {object} props
 *   items    — [{ key, label, tone, disabled, title }]
 *   onSelect — (key) => void; el menú se cierra solo antes de invocarlo
 *   variant  — "plain" (filas de tabla) | "bordered" (cabecera de página)
 *   width    — clase Tailwind de ancho del panel
 */
export default function ActionMenu({
  items = [],
  onSelect,
  variant = "plain",
  width = "w-56",
  label = "Acciones",
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const buttonRef = useRef(null);
  const panelRef = useRef(null);
  const [coords, setCoords] = useState({ top: 0, left: 0 });

  const recalcPosition = () => {
    const btn = buttonRef.current;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const panelWidth = panelRef.current?.offsetWidth || 224;
    const margin = 8;
    const viewportWidth = window.innerWidth || 0;
    let left = rect.right - panelWidth;
    if (left < margin) left = margin;
    if (left + panelWidth > viewportWidth - margin) {
      left = Math.max(margin, viewportWidth - panelWidth - margin);
    }
    setCoords({ top: rect.bottom + 6, left });
  };

  useEffect(() => {
    if (!open) return undefined;
    const raf = requestAnimationFrame(recalcPosition);
    return () => cancelAnimationFrame(raf);
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const onMouseDown = (e) => {
      const insideTrigger = ref.current && ref.current.contains(e.target);
      const insidePanel = panelRef.current && panelRef.current.contains(e.target);
      if (!insideTrigger && !insidePanel) setOpen(false);
    };
    const onKeyDown = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onReposition = () => recalcPosition();
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onReposition);
    window.addEventListener("scroll", onReposition, true);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onReposition);
      window.removeEventListener("scroll", onReposition, true);
    };
  }, [open]);

  // Sin acciones no hay nada que abrir: no dibujamos el disparador.
  if (!items.length) return null;

  const triggerClass =
    variant === "bordered"
      ? "p-2 rounded border text-gray-600 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-400"
      : "px-2 py-1 rounded hover:bg-gray-100";

  // Cerrar siempre antes de actuar: si la acción abre un modal, el panel flotante
  // no puede quedar encima.
  const handleSelect = (key) => {
    setOpen(false);
    onSelect?.(key);
  };

  return (
    <div ref={ref} className="inline-block text-left">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={triggerClass}
        aria-haspopup="menu"
        aria-expanded={open ? "true" : "false"}
        aria-label={label}
      >
        <span aria-hidden className={variant === "bordered" ? "text-xl leading-none" : ""}>
          {"⋮"}
        </span>
      </button>
      {open
        ? createPortal(
          <div
            ref={panelRef}
            style={{ position: "fixed", top: coords.top, left: coords.left, zIndex: 80 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className={`${width} rounded border border-gray-200 bg-white py-1 shadow-lg`} role="menu">
              {items.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  role="menuitem"
                  className={`block w-full px-3 py-2 text-left text-sm ${toneClass(item.tone)} ${DISABLED_CLASSES}`}
                  onClick={() => handleSelect(item.key)}
                  disabled={Boolean(item.disabled)}
                  title={item.title || undefined}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>,
          document.body
        )
        : null}
    </div>
  );
}
