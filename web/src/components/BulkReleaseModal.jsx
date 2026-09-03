import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Printer, X } from "lucide-react";

import { ResponsiveModalOverlay, ResponsiveModalPanel } from "./Responsive.jsx";
import { getBlobWithMeta } from "../lib/api";
import { reservePdfWindow } from "../lib/pdf";
import { catalogEquipmentLabel, deviceCustomerTitle, formatOS, ingresoIdOf } from "../lib/ui-helpers";
import {
  BULK_RELEASE_MAX_IDS,
  bulkReleaseFailuresFromError,
  bulkReleasePath,
  classifyReleaseSelection,
  parseBulkReleaseHeaders,
  summarizeBulkRelease,
} from "../lib/release-bulk";

function errorMessage(error, fallback) {
  const raw = error?.data?.detail || error?.message || String(error || "");
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.detail) return parsed.detail;
  } catch (_) {}
  return raw;
}

// La orden de salida se imprime a nombre del dueño, no del cliente contenedor.
const clienteOf = deviceCustomerTitle;

function RowLine({ row, id, tone, detail }) {
  const Icon = tone === "ok" ? CheckCircle2 : AlertTriangle;
  const toneClass =
    tone === "ok" ? "text-emerald-600" : tone === "error" ? "text-red-600" : "text-amber-600";
  return (
    <li className="flex items-start gap-2 py-1.5">
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${toneClass}`} aria-hidden="true" />
      <div className="min-w-0">
        <div className="text-sm text-gray-900">
          <span className="font-medium">{row ? formatOS(row) : `OS ${id}`}</span>
          {row ? <span className="text-gray-600"> - {clienteOf(row)}</span> : null}
        </div>
        {row ? (
          <div className="text-xs text-gray-500">{catalogEquipmentLabel(row) || "Equipo"}</div>
        ) : null}
        {detail ? <div className={`text-xs ${toneClass}`}>{detail}</div> : null}
      </div>
    </li>
  );
}

/**
 * Libera varias OS de una y abre un único PDF con todas las órdenes de salida.
 *
 * Antes de tocar nada muestra el preflight: liberar cambia el estado, avisa a
 * cobranzas y encola los movimientos de stock, así que conviene ver qué entra y
 * qué queda afuera. El lote se manda con skip_errors para que una OS trabada no
 * deje al resto liberado y sin comprobante.
 */
export default function BulkReleaseModal({
  open,
  rows,
  canManageResolution = false,
  onClose,
  onDone,
}) {
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState("");
  const [result, setResult] = useState(null);

  const selectedRows = useMemo(() => (Array.isArray(rows) ? rows : []), [rows]);
  const { ready, blocked } = useMemo(
    () => classifyReleaseSelection(selectedRows, { canManageResolution }),
    [selectedRows, canManageResolution]
  );
  const rowById = useMemo(() => {
    const map = new Map();
    for (const row of selectedRows) {
      const id = ingresoIdOf(row);
      if (id) map.set(id, row);
    }
    return map;
  }, [selectedRows]);

  useEffect(() => {
    if (!open) return;
    setSaving(false);
    setLocalError("");
    setResult(null);
  }, [open]);

  if (!open) return null;

  const overLimit = ready.length > BULK_RELEASE_MAX_IDS;
  const canConfirm = ready.length > 0 && !overLimit && !saving && !result;

  async function handleConfirm() {
    if (!canConfirm) return;
    const ids = ready.map((item) => item.id);
    const varias = ids.length > 1;

    setSaving(true);
    setLocalError("");

    // La pestaña se reserva antes del pedido: si se abriera después, el
    // navegador la trata como popup y la bloquea.
    const reserved = reservePdfWindow({
      title: varias ? "Órdenes de salida - NEXORA" : "Orden de salida - NEXORA",
      heading: varias ? "Preparando las órdenes de salida" : "Preparando la orden de salida",
      message: varias
        ? `NEXORA está liberando ${ids.length} órdenes`
        : "NEXORA está liberando la orden",
      detail: "Esta pestaña muestra las órdenes de salida cuando el PDF esté listo.",
      fallbackMessage:
        "NEXORA sigue preparando el PDF. Si ya terminó, puede cerrar esta pestaña y reintentar la impresión.",
      steps: [
        { id: "pdf", label: varias ? "Generando las órdenes de salida" : "Generando la orden de salida" },
        { id: "print", label: "Abriendo la impresión" },
      ],
    });

    try {
      reserved.update?.({
        status: varias ? "Generando las órdenes de salida" : "Generando la orden de salida",
        step: "pdf",
      });
      const { blob, headers } = await getBlobWithMeta(bulkReleasePath(ids));
      if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");

      const parsed = parseBulkReleaseHeaders(headers);
      // El endpoint de una sola OS no informa headers: si respondió el PDF, salió.
      const emitidos = parsed.emitidos.length > 0 ? parsed.emitidos : ids;

      reserved.update?.({ status: "Abriendo impresión", step: "print", state: "ready" });
      const opened = reserved.openPrintable(blob, {
        title: varias ? `Órdenes de salida (${emitidos.length})` : `Orden de salida ${formatOS(ready[0]?.row)}`,
        documentLabel: varias ? `Órdenes de salida (${emitidos.length})` : `Orden de salida ${formatOS(ready[0]?.row)}`,
      });

      setResult({ requested: ids.length, emitidos, fallidos: parsed.fallidos, opened });
      if (!opened) {
        setLocalError(
          "Las órdenes ya están emitidas, pero el navegador bloqueó la ventana de impresión. Buscá la pestaña que abrió NEXORA."
        );
      }
      await onDone?.({ emitidos, fallidos: parsed.fallidos });
    } catch (error) {
      reserved.close?.();
      const fallidos = bulkReleaseFailuresFromError(error);
      if (fallidos.length > 0) {
        setResult({ requested: ids.length, emitidos: [], fallidos, opened: false });
      }
      setLocalError(errorMessage(error, "No se pudieron liberar las órdenes elegidas."));
      await onDone?.({ emitidos: [], fallidos });
    } finally {
      setSaving(false);
    }
  }

  return (
    <ResponsiveModalOverlay
      role="dialog"
      aria-modal="true"
      onClick={() => !saving && onClose?.()}
      className="bg-black/45"
    >
      <ResponsiveModalPanel className="max-w-3xl" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
          <div>
            <h2 className="text-lg font-semibold text-gray-950">
              {result ? "Resultado de la liberación" : `Liberar ${ready.length} órdenes`}
            </h2>
            <div className="text-sm text-gray-600">
              {result
                ? summarizeBulkRelease({
                    requested: result.requested,
                    emitidos: result.emitidos,
                    fallidos: result.fallidos,
                    excluidos: blocked.length,
                  })
                : `${selectedRows.length} elegidas - se emite un solo PDF con todas las órdenes de salida.`}
            </div>
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
          {!result ? (
            <>
              {ready.length > 0 ? (
                <div>
                  <div className="text-[11px] font-semibold uppercase text-gray-500">
                    Se liberan ({ready.length})
                  </div>
                  <ul className="mt-1 divide-y divide-gray-100">
                    {ready.map((item) => (
                      <RowLine key={item.id} id={item.id} row={item.row} tone="ok" />
                    ))}
                  </ul>
                </div>
              ) : (
                <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                  Ninguna de las órdenes elegidas se puede liberar todavía.
                </div>
              )}

              {blocked.length > 0 ? (
                <div>
                  <div className="text-[11px] font-semibold uppercase text-gray-500">
                    Se excluyen ({blocked.length})
                  </div>
                  <ul className="mt-1 divide-y divide-gray-100">
                    {blocked.map((item, index) => (
                      <RowLine
                        key={item.id ?? `blocked-${index}`}
                        id={item.id}
                        row={item.row}
                        tone="warn"
                        detail={item.reason}
                      />
                    ))}
                  </ul>
                </div>
              ) : null}

              {overLimit ? (
                <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                  Son {ready.length} órdenes y el máximo por lote es {BULK_RELEASE_MAX_IDS}. Elegí menos y repetí.
                </div>
              ) : (
                <div className="rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
                  Liberar no se puede deshacer: cambia el estado de la OS, avisa que quedó lista para
                  facturar y encola el movimiento de stock.
                </div>
              )}
            </>
          ) : (
            <>
              {result.emitidos.length > 0 ? (
                <div>
                  <div className="text-[11px] font-semibold uppercase text-gray-500">
                    Liberadas ({result.emitidos.length})
                  </div>
                  <ul className="mt-1 divide-y divide-gray-100">
                    {result.emitidos.map((id) => (
                      <RowLine key={`ok-${id}`} id={id} row={rowById.get(id)} tone="ok" />
                    ))}
                  </ul>
                </div>
              ) : null}

              {result.fallidos.length > 0 ? (
                <div>
                  <div className="text-[11px] font-semibold uppercase text-gray-500">
                    No se pudieron liberar ({result.fallidos.length})
                  </div>
                  <ul className="mt-1 divide-y divide-gray-100">
                    {result.fallidos.map((item) => (
                      <RowLine
                        key={`fail-${item.id}`}
                        id={item.id}
                        row={rowById.get(item.id)}
                        tone="error"
                        detail={item.detail}
                      />
                    ))}
                  </ul>
                </div>
              ) : null}
            </>
          )}

          {localError ? (
            <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
              {localError}
            </div>
          ) : null}
        </div>

        <div className="flex flex-col gap-2 border-t px-4 py-3 sm:flex-row sm:items-center sm:justify-end">
          <button
            type="button"
            className="rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-60"
            onClick={onClose}
            disabled={saving}
          >
            {result ? "Cerrar" : "Cancelar"}
          </button>
          {!result ? (
            <button
              type="button"
              className="btn inline-flex w-full items-center justify-center gap-2 disabled:opacity-60 sm:w-auto"
              onClick={handleConfirm}
              disabled={!canConfirm}
              aria-busy={saving ? "true" : "false"}
            >
              <Printer className="h-4 w-4" aria-hidden="true" />
              {saving ? "Liberando..." : `Liberar ${ready.length} e imprimir`}
            </button>
          ) : null}
        </div>
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
  );
}
