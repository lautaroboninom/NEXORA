import { useEffect, useState } from "react";
import { Check, Loader2 } from "lucide-react";

export const RIS_PROGRESS_MIN_MS = 900;

export function waitForRisProgressPaint() {
  if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
  });
}

export function waitForRisProgressMinimum(startedAt, minMs = RIS_PROGRESS_MIN_MS) {
  const elapsed = Date.now() - startedAt;
  const remaining = Math.max(0, minMs - elapsed);
  if (!remaining) return Promise.resolve();
  return new Promise((resolve) => window.setTimeout(resolve, remaining));
}

function formatElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** Contador vivo: el tick vive acá para no re-renderizar la página entera cada segundo. */
function ElapsedCounter({ startedAt }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!startedAt) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  if (!startedAt) return null;
  return (
    <span className="ml-2 shrink-0 font-mono text-xs tabular-nums text-gray-500">
      {formatElapsed(now - startedAt)}
    </span>
  );
}

export default function RisProgressModal({
  open,
  title = "Emitiendo RIS",
  status = "Emitiendo RIS en Bejerman",
  detail = "NEXORA está preparando el PDF. Se abrirá automáticamente cuando esté listo.",
  // El remito ya confirmado en Bejerman: mientras esto tenga valor, lo importante
  // ya salió bien y lo único que falta es el papel.
  documentLabel = "",
  waitStartedAt = 0,
  onCancel,
}) {
  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-gray-950/45 px-4"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="ris-progress-title"
      aria-describedby="ris-progress-status"
    >
      <div className="w-full max-w-sm rounded-lg border border-sky-200 bg-white p-5 shadow-xl">
        <div className="flex items-start gap-4">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-sky-50 text-sky-700">
            <Loader2 className="h-6 w-6 animate-spin" aria-hidden="true" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 id="ris-progress-title" className="text-base font-semibold text-gray-950">
              {title}
            </h2>
            <div className="mt-1 flex items-start justify-between gap-2">
              <p id="ris-progress-status" className="min-w-0 text-sm font-medium text-sky-800">
                {status}
              </p>
              <ElapsedCounter startedAt={waitStartedAt} />
            </div>
            <p className="mt-2 text-sm text-gray-600">{detail}</p>

            {documentLabel && (
              <p className="mt-3 flex items-start gap-1.5 rounded border border-emerald-200 bg-emerald-50 px-2.5 py-2 text-sm text-emerald-800">
                <Check className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  <strong className="font-semibold">{documentLabel}</strong> ya emitido en Bejerman.
                  NEXORA está preparando el PDF para imprimir.
                </span>
              </p>
            )}

            {onCancel && (
              <div className="mt-4 flex justify-end">
                <button
                  type="button"
                  onClick={onCancel}
                  className="rounded border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
                >
                  Cancelar espera
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
