import { useEffect, useMemo, useState } from "react";

function normalizeNumberInput(value) {
  return String(value ?? "").replace(",", ".").trim();
}

export default function RejectedBudgetChargeModal({
  open,
  title,
  confirmLabel,
  saving = false,
  loading = false,
  error = "",
  money,
  initialComment = "",
  initialCharge = "",
  showComment = true,
  quoteSummary = null,
  referenceTitle = "Presupuesto rechazado",
  referenceWarning = "",
  onClose,
  onConfirm,
}) {
  const [comment, setComment] = useState(initialComment);
  const [charge, setCharge] = useState(String(initialCharge ?? ""));
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    if (!open) return;
    setComment(initialComment || "");
    setCharge(String(initialCharge ?? ""));
    setLocalError("");
  }, [open, initialComment, initialCharge]);

  const normalizedCharge = useMemo(() => normalizeNumberInput(charge), [charge]);
  const chargeNumber = Number(normalizedCharge);
  const chargeIsValid = normalizedCharge !== "" && Number.isFinite(chargeNumber) && chargeNumber >= 0;
  const iva = chargeIsValid ? chargeNumber * 0.21 : null;
  const total = chargeIsValid ? chargeNumber + iva : null;

  async function handleConfirm() {
    if (!chargeIsValid) {
      setLocalError("Ingrese un importe neto válido.");
      return;
    }

    setLocalError("");
    await onConfirm({
      rechazo_comentario: comment.trim(),
      presupuesto_rechazado_cobro_neto: normalizedCharge,
    });
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      onClick={() => !saving && onClose()}
    >
      <div className="w-full max-w-2xl rounded bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="border-b px-4 py-3">
          <div className="text-lg font-semibold">{title}</div>
          <div className="text-sm text-gray-600">Definí el valor neto que realmente se va a cobrar.</div>
        </div>

        <div className="space-y-4 px-4 py-4">
          {showComment ? (
            <label className="block">
              <div className="mb-1 text-sm text-gray-600">Motivo del rechazo</div>
              <textarea
                className="min-h-[96px] w-full rounded border p-2"
                placeholder="Opcional"
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                disabled={saving}
              />
            </label>
          ) : null}

          <label className="block">
            <div className="mb-1 text-sm text-gray-600">Importe neto a cobrar</div>
            <input
              type="number"
              step="0.01"
              min="0"
              inputMode="decimal"
              className="w-full rounded border p-2 text-right"
              placeholder="0.00"
              value={charge}
              onChange={(e) => setCharge(e.target.value)}
              disabled={saving}
            />
          </label>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div className="rounded border p-3">
              <div className="text-sm text-gray-600">Neto</div>
              <div className="text-lg font-semibold">{chargeIsValid ? money(chargeNumber) : "-"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-sm text-gray-600">IVA 21%</div>
              <div className="text-lg font-semibold">{chargeIsValid ? money(iva) : "-"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-sm text-gray-600">Total</div>
              <div className="text-lg font-semibold">{chargeIsValid ? money(total) : "-"}</div>
            </div>
          </div>

          <div className="rounded border bg-slate-50 p-3">
            <div className="mb-2 text-sm font-medium text-slate-700">{referenceTitle}</div>
            {loading ? (
              <div className="text-sm text-gray-500">Cargando referencia...</div>
            ) : quoteSummary ? (
              <div className="space-y-1 text-sm">
                <div className="text-slate-700">
                  {quoteSummary.versionNum ? `Versión ${quoteSummary.versionNum}` : "Versión rechazada"}
                </div>
                <div className="text-slate-700">
                  Neto: <span className="font-medium">{money(quoteSummary.subtotal || 0)}</span>
                </div>
                <div className="text-slate-700">
                  IVA 21%: <span className="font-medium">{money(quoteSummary.iva_21 || 0)}</span>
                </div>
                <div className="text-slate-700">
                  Total: <span className="font-medium">{money(quoteSummary.total || 0)}</span>
                </div>
              </div>
            ) : (
              <div className="text-sm text-gray-500">No hay un presupuesto rechazado vinculado para mostrar como referencia.</div>
            )}
            {referenceWarning ? <div className="mt-2 text-sm text-amber-700">{referenceWarning}</div> : null}
          </div>

          {localError || error ? (
            <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
              {localError || error}
            </div>
          ) : null}
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-4 py-3">
          <button
            className="rounded border px-3 py-2 text-sm hover:bg-gray-50"
            onClick={onClose}
            type="button"
            disabled={saving}
          >
            Cancelar
          </button>
          <button
            className="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-60"
            onClick={handleConfirm}
            type="button"
            disabled={saving}
          >
            {saving ? "Guardando..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
