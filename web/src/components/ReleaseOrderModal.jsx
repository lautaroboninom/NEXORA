import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Printer, X } from "lucide-react";

import StatusChip from "./StatusChip.jsx";
import DeviceIdentifier from "./DeviceIdentifier.jsx";
import { ResponsiveModalOverlay, ResponsiveModalPanel } from "./Responsive.jsx";
import { getBlob, postCerrarReparacion } from "../lib/api";
import { openPrintablePdf } from "../lib/pdf";
import { RESOLUCION, RESOLUCION_OPTIONS, resolutionLabel } from "../lib/constants";
import { catalogEquipmentLabel, formatOS, ingresoIdOf } from "../lib/ui-helpers";
import { getReleaseFlow, normalizeReleaseValue, releaseResolutionSuggestion } from "../lib/release-flow";

function errorMessage(error, fallback) {
  const raw = error?.data?.detail || error?.message || String(error || "");
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.detail) return parsed.detail;
  } catch (_) {}
  return raw;
}

function normalizeNumberInput(value) {
  return String(value ?? "").replace(",", ".").trim();
}

function Detail({ label, children }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-semibold uppercase text-gray-500">{label}</div>
      <div className="mt-0.5 break-words text-sm text-gray-900">{children || "-"}</div>
    </div>
  );
}

export default function ReleaseOrderModal({
  open,
  row,
  canManageResolution = false,
  onClose,
  onReleased,
}) {
  const flow = useMemo(
    () => getReleaseFlow(row, { canManageResolution }),
    [row, canManageResolution],
  );
  const ingresoId = ingresoIdOf(row);

  const [resolution, setResolution] = useState("");
  const [serialCambio, setSerialCambio] = useState("");
  const [charge, setCharge] = useState("");
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState("");
  const [manualPrint, setManualPrint] = useState(null);

  useEffect(() => {
    if (!open) return;
    setResolution(releaseResolutionSuggestion(row));
    setSerialCambio(String(row?.serial_cambio || ""));
    setCharge(String(row?.presupuesto_rechazado_cobro_neto ?? ""));
    setLocalError("");
    setManualPrint(null);
  }, [open, row?.id]);

  if (!open || !row) return null;

  const currentResolution = normalizeReleaseValue(row?.resolucion);
  const selectedResolution = normalizeReleaseValue(resolution);
  const missingResolution = !currentResolution && !flow.isReleased;
  const canEditResolution = Boolean(canManageResolution && !flow.isReleased);
  const shouldPersistResolution = Boolean(
    !flow.isReleased &&
      canManageResolution &&
      selectedResolution &&
      (
        selectedResolution !== currentResolution ||
        selectedResolution === RESOLUCION.PRESUPUESTO_RECHAZADO ||
        selectedResolution === RESOLUCION.CAMBIO
      )
  );
  const usesBackendAutoRepaired = Boolean(
    !currentResolution &&
      flow.estado === "reparado" &&
      selectedResolution === RESOLUCION.REPARADO &&
      !canManageResolution
  );
  const resolutionBlockedByPermission = Boolean(
    missingResolution &&
      !canManageResolution &&
      !usesBackendAutoRepaired
  );

  const normalizedCharge = normalizeNumberInput(charge);
  const chargeNumber = Number(normalizedCharge);
  const chargeValid = normalizedCharge !== "" && Number.isFinite(chargeNumber) && chargeNumber >= 0;
  const needsCharge = selectedResolution === RESOLUCION.PRESUPUESTO_RECHAZADO;
  const needsSerial = selectedResolution === RESOLUCION.CAMBIO;
  const serialValue = String(serialCambio || "").trim();
  const validationError = flow.blockedReason
    ? ""
    : resolutionBlockedByPermission
      ? "Falta definir la resolución. Debe hacerlo un jefe antes de liberar."
      : !selectedResolution && !flow.isReleased
      ? "Seleccioná la resolución de reparación."
      : needsCharge && !chargeValid
        ? "Ingresá el importe neto del presupuesto rechazado."
        : needsSerial && !serialValue
          ? "Ingresá la serie del cambio."
          : "";
  const canConfirm = Boolean(flow.canSubmit && !validationError && !saving && !manualPrint?.blob);

  async function handleConfirm() {
    if (!canConfirm) {
      setLocalError(validationError || flow.blockedReason || "No se puede liberar este equipo.");
      return;
    }

    try {
      setSaving(true);
      setLocalError("");
      setManualPrint(null);

      if (shouldPersistResolution) {
        const payload = { resolucion: selectedResolution };
        if (needsCharge) payload.presupuesto_rechazado_cobro_neto = normalizedCharge;
        if (needsSerial) payload.serial_cambio = serialValue;
        await postCerrarReparacion(ingresoId, payload);
      }

      const blob = await getBlob(`/api/ingresos/${ingresoId}/remito/`);
      if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");

      const result = openPrintablePdf(blob, {
        title: `Orden de salida ${formatOS(row)}`,
        documentLabel: `Orden de salida ${formatOS(row)}`,
      });
      await onReleased?.({ opened: result.opened, row });

      if (result.opened) {
        onClose?.();
      } else {
        setManualPrint({ blob });
        setLocalError("La orden de salida ya está lista, pero el navegador bloqueó la ventana automática. Usá Abrir e imprimir orden de salida.");
      }
    } catch (error) {
      setLocalError(errorMessage(error, "No se pudo liberar el equipo."));
    } finally {
      setSaving(false);
    }
  }

  async function handleManualPrint() {
    if (!manualPrint?.blob) return;
    const result = openPrintablePdf(manualPrint.blob, {
      title: `Orden de salida ${formatOS(row)}`,
      documentLabel: `Orden de salida ${formatOS(row)}`,
    });
    if (result.opened) {
      await onReleased?.({ opened: true, row });
      onClose?.();
    }
  }

  return (
    <ResponsiveModalOverlay role="dialog" aria-modal="true" onClick={() => !saving && onClose?.()} className="bg-black/45">
      <ResponsiveModalPanel className="max-w-3xl" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
          <div>
            <h2 className="text-lg font-semibold text-gray-950">Salida del equipo</h2>
            <div className="text-sm text-gray-600">{formatOS(row)} - {catalogEquipmentLabel(row) || "Equipo"}</div>
          </div>
          <button
            type="button"
            className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-900 disabled:opacity-60"
            onClick={onClose}
            disabled={saving}
            aria-label="Cerrar"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>

        <div className="space-y-4 px-4 py-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Detail label="OS">{formatOS(row)}</Detail>
            <Detail label="Cliente">{row?.razon_social ?? row?.cliente ?? row?.cliente_nombre}</Detail>
            <Detail label="Serie"><DeviceIdentifier row={row} /></Detail>
            <Detail label="Estado"><StatusChip value={row?.estado} /></Detail>
            <Detail label="Presupuesto"><StatusChip value={row?.presupuesto_estado} /></Detail>
            <Detail label="Resolución">{currentResolution ? resolutionLabel(currentResolution) : "Sin definir"}</Detail>
          </div>

          <div className={`rounded border px-3 py-2 text-sm ${flow.blockedReason || validationError ? "border-amber-300 bg-amber-50 text-amber-900" : "border-emerald-200 bg-emerald-50 text-emerald-800"}`}>
            <div className="flex items-start gap-2">
              {flow.blockedReason || validationError ? (
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              ) : (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              )}
              <div>{validationError || flow.statusText}</div>
            </div>
          </div>

          {!flow.isReleased ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-sm text-gray-600">Resolución de reparación</div>
                <select
                  className="w-full rounded border p-2 disabled:bg-gray-100"
                  value={resolution}
                  onChange={(event) => setResolution(event.target.value)}
                  disabled={!canEditResolution || saving}
                >
                  <option value="">Seleccionar resolución</option>
                  {RESOLUCION_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>

              {needsSerial ? (
                <label className="block">
                  <div className="mb-1 text-sm text-gray-600">Serie del cambio</div>
                  <input
                    className="w-full rounded border p-2"
                    value={serialCambio}
                    onChange={(event) => setSerialCambio(event.target.value)}
                    disabled={saving || (!canManageResolution && !row?.serial_cambio)}
                    placeholder="Serie del equipo entregado"
                  />
                </label>
              ) : null}

              {needsCharge ? (
                <label className="block">
                  <div className="mb-1 text-sm text-gray-600">Importe neto a cobrar</div>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    inputMode="decimal"
                    className="w-full rounded border p-2 text-right"
                    value={charge}
                    onChange={(event) => setCharge(event.target.value)}
                    disabled={saving || (!canManageResolution && !row?.presupuesto_rechazado_cobro_neto)}
                    placeholder="0.00"
                  />
                </label>
              ) : null}
            </div>
          ) : null}

          {localError ? (
            <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
              {localError}
            </div>
          ) : null}
        </div>

        <div className="flex flex-col gap-2 border-t px-4 py-3 sm:flex-row sm:items-center sm:justify-end">
          {manualPrint?.blob ? (
            <button
              type="button"
              className="btn inline-flex w-full items-center justify-center gap-2 sm:w-auto"
              onClick={handleManualPrint}
              disabled={saving}
            >
              <Printer className="h-4 w-4" aria-hidden="true" />
              Abrir e imprimir orden de salida
            </button>
          ) : null}
          <button
            type="button"
            className="rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-60"
            onClick={onClose}
            disabled={saving}
          >
            Cancelar
          </button>
          <button
            type="button"
            className="btn inline-flex w-full items-center justify-center gap-2 disabled:opacity-60 sm:w-auto"
            onClick={handleConfirm}
            disabled={!canConfirm}
            aria-busy={saving ? "true" : "false"}
          >
            <Printer className="h-4 w-4" aria-hidden="true" />
            {saving ? "Preparando..." : flow.isReleased ? "Reimprimir orden" : "Liberar e imprimir"}
          </button>
        </div>
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
  );
}
