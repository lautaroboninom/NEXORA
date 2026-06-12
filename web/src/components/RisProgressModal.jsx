import { Loader2 } from "lucide-react";

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

export default function RisProgressModal({
  open,
  title = "Emitiendo RIS",
  status = "Emitiendo RIS en Bejerman",
  detail = "NEXORA está trabajando. El PDF se abrirá cuando esté listo.",
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
          <div className="min-w-0">
            <h2 id="ris-progress-title" className="text-base font-semibold text-gray-950">
              {title}
            </h2>
            <p id="ris-progress-status" className="mt-1 text-sm font-medium text-sky-800">
              {status}
            </p>
            <p className="mt-2 text-sm text-gray-600">{detail}</p>
          </div>
        </div>
      </div>
    </div>
  );
}
