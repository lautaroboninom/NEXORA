import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Printer, X } from "lucide-react";

import StatusChip from "./StatusChip.jsx";
import DeviceIdentifier from "./DeviceIdentifier.jsx";
import DeviceSwapModal from "./DeviceSwapModal.jsx";
import { ResponsiveModalOverlay, ResponsiveModalPanel } from "./Responsive.jsx";
import { getBlob, postCerrarReparacion } from "../lib/api";
import { openPrintablePdf } from "../lib/pdf";
import { RESOLUCION, RESOLUCION_OPTIONS, resolutionLabel } from "../lib/constants";
import { catalogEquipmentLabel, deviceCustomerTitle, formatOS, ingresoIdOf } from "../lib/ui-helpers";
import { getReleaseFlow, normalizeReleaseValue, releaseResolutionSuggestion } from "../lib/release-flow";
import { describeSwapSummary, getDeviceSwapState } from "../lib/device-swap.js";

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
  canSwapDevice = false,
  onClose,
  onReleased,
  onSwapChanged,
}) {
  const ingresoId = ingresoIdOf(row);

  const [resolution, setResolution] = useState("");
  // El cambio ya no se escribe acá: se registra vinculando un equipo real. Este estado
  // guarda lo que devuelve el modal del cambio para no mostrar datos viejos mientras el
  // refresco del listado vuelve.
  const [swapOverride, setSwapOverride] = useState(undefined);
  const [swapModalOpen, setSwapModalOpen] = useState(false);
  const [charge, setCharge] = useState("");
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState("");
  const [manualPrint, setManualPrint] = useState(null);

  const flow = useMemo(() => {
    // Si puede gestionar la resolución, la que elija en el combo cuenta ya
    // mismo para habilitar el botón: se persiste antes de pedir el remito
    // (ver shouldPersistResolution), así que el gate no puede esperar a que
    // vuelva a guardarse en `row` primero.
    const pendingResolution = canManageResolution ? normalizeReleaseValue(resolution) : "";
    const effectiveRow = pendingResolution ? { ...row, resolucion: pendingResolution } : row;
    return getReleaseFlow(effectiveRow, { canManageResolution });
  }, [row, canManageResolution, resolution]);

  useEffect(() => {
    if (!open) return;
    setResolution(releaseResolutionSuggestion(row));
    setSwapOverride(undefined);
    setSwapModalOpen(false);
    setCharge(String(row?.presupuesto_rechazado_cobro_neto ?? ""));
    setLocalError("");
    setManualPrint(null);
  }, [open, row?.id]);

  if (!open || !row) return null;

  const currentResolution = normalizeReleaseValue(row?.resolucion);
  const selectedResolution = normalizeReleaseValue(resolution);
  const missingResolution = !currentResolution && !flow.isReprint;
  const canEditResolution = Boolean(canManageResolution && !flow.isReprint);
  const shouldPersistResolution = Boolean(
    !flow.isReprint &&
      canManageResolution &&
      selectedResolution &&
      (
        selectedResolution !== currentResolution ||
        selectedResolution === RESOLUCION.PRESUPUESTO_RECHAZADO ||
        selectedResolution === RESOLUCION.CAMBIO
      )
  );
  const usesBackendAutoResolution = Boolean(
    !currentResolution &&
      flow.suggestedResolution &&
      selectedResolution === flow.suggestedResolution &&
      !canManageResolution &&
      !flow.isReprint
  );
  const resolutionBlockedByPermission = Boolean(
      missingResolution &&
      !canManageResolution &&
      !usesBackendAutoResolution
  );

  const normalizedCharge = normalizeNumberInput(charge);
  const chargeNumber = Number(normalizedCharge);
  const chargeValid = normalizedCharge !== "" && Number.isFinite(chargeNumber) && chargeNumber >= 0;
  const needsCharge = selectedResolution === RESOLUCION.PRESUPUESTO_RECHAZADO;
  const needsSerial = selectedResolution === RESOLUCION.CAMBIO;
  // El cambio se registra vinculando un equipo real; acá sólo se comprueba que exista.
  // Al sobreescribir hay que pisar también las columnas planas: los listados traen el
  // cambio ahí y, si sólo se limpiara `cambio_equipo`, un cambio deshecho seguiría vivo.
  const swapRow =
    swapOverride === undefined
      ? row
      : {
        ...row,
        cambio_equipo: swapOverride,
        serial_cambio: swapOverride?.serial_cambio ?? "",
        device_cambio_id: swapOverride?.device_cambio_id ?? null,
        cambio_destino_viejo: swapOverride?.destino_viejo ?? "",
        cambio_sin_entrega: swapOverride?.sin_entrega ?? false,
      };
  const swapState = getDeviceSwapState(
    { ...swapRow, resolucion: selectedResolution || swapRow?.resolucion },
    { canSwap: canSwapDevice }
  );
  const swapSummary = describeSwapSummary({
    ...swapRow,
    resolucion: selectedResolution || swapRow?.resolucion,
  });
  const validationError = flow.blockedReason
    ? ""
    : resolutionBlockedByPermission
      ? "Falta definir la resolución. Debe hacerlo un jefe antes de liberar."
      : !selectedResolution && !flow.isReprint
      ? "Seleccioná la resolución de reparación."
      : needsCharge && !chargeValid
        ? "Ingresá el importe neto del presupuesto rechazado."
        : needsSerial && !swapState.hasSwap
          ? "Registrá el cambio de equipo: falta elegir el equipo que se entrega."
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

      // El cambio de equipo ya dejó puesta la resolución al vincular el equipo, así que no
      // se vuelve a pasar por `cerrar/`: hacerlo sólo reescribiría el mismo snapshot.
      if (shouldPersistResolution && !needsSerial) {
        const payload = { resolucion: selectedResolution };
        if (needsCharge) payload.presupuesto_rechazado_cobro_neto = normalizedCharge;
        await postCerrarReparacion(ingresoId, payload);
      }

      const remitoPath = `/api/ingresos/${ingresoId}/remito/${flow.isReprint ? "?reprint=1" : ""}`;
      const blob = await getBlob(remitoPath);
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
            <Detail label="Cliente">{deviceCustomerTitle(row)}</Detail>
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

          {!flow.isReprint ? (
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
                <div className="sm:col-span-2">
                  <div className="mb-1 text-sm text-gray-600">Cambio de equipo</div>
                  <div className="rounded border bg-gray-50 p-3 text-sm">
                    {swapState.hasSwap ? (
                      <div className="text-gray-900">{swapSummary}</div>
                    ) : (
                      <div className="text-amber-800">
                        Todavía no está registrado qué equipo se entrega.
                      </div>
                    )}
                    {canSwapDevice ? (
                      <button
                        type="button"
                        className="mt-2 rounded border px-3 py-1.5 text-sm hover:bg-white disabled:opacity-60"
                        onClick={() => setSwapModalOpen(true)}
                        disabled={saving}
                      >
                        {swapState.hasSwap ? "Ver / editar cambio" : "Registrar cambio de equipo"}
                      </button>
                    ) : (
                      <div className="mt-1 text-xs text-gray-600">
                        No tenés permiso para registrarlo: lo carga recepción, administración o
                        jefatura.
                      </div>
                    )}
                  </div>
                </div>
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
                    disabled={saving || (!canManageResolution && row?.presupuesto_rechazado_cobro_neto == null)}
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
            {saving
              ? "Preparando..."
              : flow.isSalePending || flow.isSoldDelivered
                ? "Ver e imprimir salida"
                : flow.isReprint
                  ? "Ver y reimprimir orden"
                  : "Liberar e imprimir"}
          </button>
        </div>

        {swapModalOpen ? (
          <DeviceSwapModal
            open={swapModalOpen}
            row={swapRow}
            ingresoId={ingresoId}
            canSwap={canSwapDevice}
            onClose={() => setSwapModalOpen(false)}
            onDone={async ({ cambio } = {}) => {
              // El listado que abrió este modal todavía tiene la fila vieja: se guarda el
              // cambio recién registrado para no pedir liberar con datos desactualizados.
              setSwapOverride(cambio ?? null);
              await onSwapChanged?.();
            }}
          />
        ) : null}
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
  );
}
