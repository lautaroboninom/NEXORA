// web/src/pages/JefePresupuestos.jsx
import { useEffect, useMemo, useState } from "react";
import api, { downloadAuth, getBlob } from "../lib/api";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import {
  catalogEquipmentLabel,
  formatDateOnly,
  formatMoney,
  formatOS,
  ingresoIdOf,
  norm,
  resolveFechaCreacion,
  tipoEquipoOf,
} from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import useQueryState from "../hooks/useQueryState";
import { DesktopTableWrap, MobileDataCard, MobileDataField, MobileDataList } from "../components/Responsive.jsx";
import { reservePdfWindow } from "../lib/pdf";

// ENDPOINT para "presupuestados" (ya emitidos/enviados)
const ENDPOINT = "/api/ingresos/presupuestados/"; // Ajustar si la API usa otra ruta.

export default function JefePresupuestos() {
  const { user } = useAuth();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [bulkResultMsg, setBulkResultMsg] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [busyId, setBusyId] = useState(null);
  const [bulkApproving, setBulkApproving] = useState(false);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [exporting, setExporting] = useState(false);

  const navigate = useNavigate();
  const canApprove = can(user, PERMISSION_CODES.ACTION_PRESUPUESTO_MANAGE);

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get(ENDPOINT);
      const list = Array.isArray(data) ? data : [];
      // Orden sugerido: mas recientes primero por fecha de emision/envio o ingreso
      list.sort((a, b) => {
        const da = new Date(
          a?.presupuesto_fecha_envio ?? a?.presupuesto_fecha_emision ?? resolveFechaCreacion(a) ?? 0
        ).getTime();
        const db = new Date(
          b?.presupuesto_fecha_envio ?? b?.presupuesto_fecha_emision ?? resolveFechaCreacion(b) ?? 0
        ).getTime();
        return db - da;
      });
      setRows(list);
    } catch (e) {
      setErr(e?.message || "No se pudo cargar la lista de presupuestados");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    const needle = norm(q);
    if (!needle) return rows;
    return rows.filter((row) => {
      const campos = [
        formatOS(row),
        row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
        row?.marca ?? row?.equipo?.marca,
        catalogEquipmentLabel(row),
        tipoEquipoOf(row),
        row?.estado,
        row?.numero_serie,
        row?.numero_interno,
        String(row?.presupuesto_monto ?? row?.presupuesto_total ?? ""),
      ];
      return campos.some((c) => norm(c).includes(needle));
    });
  }, [rows, q]);

  const rowById = useMemo(() => {
    const map = new Map();
    for (const row of rows) {
      const id = ingresoIdOf(row);
      if (id == null || id === "") continue;
      map.set(id, row);
    }
    return map;
  }, [rows]);

  const visibleIds = useMemo(() => new Set(filtered.map((r) => ingresoIdOf(r))), [filtered]);
  const allVisibleSelected = useMemo(() => {
    if (visibleIds.size === 0) return false;
    for (const id of visibleIds) if (!selectedIds.has(id)) return false;
    return true;
  }, [visibleIds, selectedIds]);

  const toggleSelectAllVisible = () => {
    if (bulkApproving) return;
    const next = new Set(selectedIds);
    if (allVisibleSelected) {
      for (const id of visibleIds) next.delete(id);
    } else {
      for (const id of visibleIds) next.add(id);
    }
    setSelectedIds(next);
  };

  const toggleRow = (e, row) => {
    e.stopPropagation();
    if (bulkApproving) return;
    const id = ingresoIdOf(row);
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelectedIds(next);
  };

  async function exportByIds(ids, fnameHint = "presupuestados") {
    if (!ids || ids.length === 0) return;
    try {
      setExporting(true);
      const qs = new URLSearchParams({ ids: ids.join(",") }).toString();
      await downloadAuth(`/api/ingresos/presupuestados/export/?${qs}`, `${fnameHint}.xlsx`);
    } catch (e) {
      setErr(e?.message || "No se pudo exportar el Excel");
    } finally {
      setExporting(false);
    }
  }

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  const onRowKeyDown = (e, row) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      go(row);
    }
  };

  function isReparado(row) {
    return norm(row?.estado) === "reparado";
  }

  function closeIfOpen(win) {
    if (!win) return;
    try {
      if (typeof win.close === "function") {
        win.close();
      } else if (!win.closed) {
        win.close();
      }
    } catch (_) {
      // noop
    }
  }

  function remitosSalidaPath(ingresoIds) {
    const ids = (ingresoIds || []).filter(Boolean);
    if (ids.length === 1) return `/api/ingresos/${encodeURIComponent(ids[0])}/remito/`;
    const qs = new URLSearchParams({ ids: ids.join(",") }).toString();
    return `/api/ingresos/remitos-salida/?${qs}`;
  }

  async function openRemitosSalida(ingresoIds, reservedWindow) {
    const ids = (ingresoIds || []).filter(Boolean);
    if (ids.length === 0) return [];
    const blob = await getBlob(remitosSalidaPath(ids));
    if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");
    const opened = reservedWindow?.open ? reservedWindow.open(blob) : false;
    if (!opened) throw new Error("El navegador bloqueó la apertura de la orden de salida.");
    return ids;
  }

  async function approveRows(rowsToApprove, { askPrint = true, confirmPrintMessage = "" } = {}) {
    const validRows = [];
    const approvalFailures = [];
    for (const row of rowsToApprove || []) {
      const ingresoId = ingresoIdOf(row);
      if (!ingresoId) {
        approvalFailures.push({
          ingresoId,
          error: new Error("No se encontró el ID de ingreso para aprobar."),
        });
        continue;
      }
      validRows.push(row);
    }

    const reparadoRows = validRows.filter(isReparado);
    const reparadoIds = new Set(reparadoRows.map((row) => ingresoIdOf(row)));
    let shouldPrint = false;
    if (askPrint && reparadoRows.length > 0) {
      shouldPrint = window.confirm(
        confirmPrintMessage || "Este equipo ya está reparado. ¿Imprimir orden de salida?"
      );
    }

    let reservedPrintWindow = null;
    if (shouldPrint) {
      reservedPrintWindow = reservePdfWindow({
        title: reparadoRows.length > 1 ? "Órdenes de salida" : "Orden de salida",
        message:
          reparadoRows.length > 1
            ? "Preparando órdenes de salida..."
            : "Preparando orden de salida...",
        fallbackMessage:
          "NEXORA sigue preparando el PDF de la orden de salida. Si ya terminó, puede cerrar esta pestaña y reintentar la impresión.",
      });
    }

    const approvedIds = [];
    const printedIds = [];
    const printFailures = [];

    for (const row of validRows) {
      const ingresoId = ingresoIdOf(row);
      try {
        await api.post(`/api/quotes/${ingresoId}/aprobar/`);
        approvedIds.push(ingresoId);
      } catch (e) {
        approvalFailures.push({ ingresoId, error: e });
        continue;
      }
    }

    const idsToPrint = shouldPrint ? approvedIds.filter((ingresoId) => reparadoIds.has(ingresoId)) : [];
    if (idsToPrint.length > 0) {
      try {
        printedIds.push(...(await openRemitosSalida(idsToPrint, reservedPrintWindow)));
      } catch (e) {
        for (const ingresoId of idsToPrint) {
          printFailures.push({ ingresoId, error: e });
        }
        closeIfOpen(reservedPrintWindow);
      }
    } else {
      closeIfOpen(reservedPrintWindow);
    }

    return {
      approvedIds,
      printedIds,
      approvalFailures,
      printFailures,
      shouldPrint,
    };
  }

  // Accion individual: se mantiene, reutilizando la logica comun.
  async function aprobar(row) {
    if (!canApprove || bulkApproving || busyId !== null) return;
    const ingresoId = ingresoIdOf(row);
    if (!ingresoId) {
      setErr("No se encontró el ID de ingreso para aprobar.");
      return;
    }
    try {
      setBusyId(ingresoId);
      setErr("");
      setBulkResultMsg("");
      const result = await approveRows([row], {
        askPrint: true,
        confirmPrintMessage: "Este equipo ya está reparado. ¿Imprimir orden de salida?",
      });
      await load();

      if (result.approvalFailures.length > 0) {
        const detail = result.approvalFailures[0]?.error?.message;
        setErr(detail || "No se pudo aprobar el presupuesto");
        return;
      }
      if (result.printFailures.length > 0) {
        const detail = result.printFailures[0]?.error?.message;
        setErr(detail || "No se pudo imprimir la orden de salida");
      }
    } catch (e) {
      setErr(e?.message || "No se pudo aprobar el presupuesto");
    } finally {
      setBusyId(null);
    }
  }

  async function aprobarSeleccion() {
    if (!canApprove || bulkApproving || busyId !== null || selectedIds.size === 0) return;
    const selectedSnapshot = Array.from(selectedIds);
    setErr("");
    setBulkResultMsg("");
    setBulkApproving(true);
    try {
      const selectedRows = [];
      const missingIds = [];
      for (const id of selectedSnapshot) {
        const row = rowById.get(id);
        if (!row) {
          missingIds.push(id);
          continue;
        }
        selectedRows.push(row);
      }

      const reparadosCount = selectedRows.filter(isReparado).length;
      const result = await approveRows(selectedRows, {
        askPrint: true,
        confirmPrintMessage: `Hay ${reparadosCount} equipos reparados. ¿Imprimir todas las órdenes de salida juntas?`,
      });
      const totalApprovalFailures = missingIds.length + result.approvalFailures.length;

      await load();

      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const id of result.approvedIds) next.delete(id);
        for (const id of missingIds) next.delete(id);
        return next;
      });

      const summary = [
        `Total: ${selectedSnapshot.length}.`,
        `Aprobados OK: ${result.approvedIds.length}.`,
        `Órdenes de salida impresas: ${result.printedIds.length}.`,
        `Fallos de aprobación: ${totalApprovalFailures}.`,
        `Fallos de impresión: ${result.printFailures.length}.`,
      ];
      if (reparadosCount > 0 && !result.shouldPrint) {
        summary.push("Impresión cancelada por el usuario.");
      }
      setBulkResultMsg(summary.join(" "));
    } catch (e) {
      setErr(e?.message || "No se pudieron aprobar los elegidos");
    } finally {
      setBulkApproving(false);
    }
  }

  const approvingBusy = bulkApproving || busyId !== null;

  return (
    <div className="card">
      <div className="h1 mb-3">Presupuestados</div>

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">
          {err}
        </div>
      )}

      {bulkResultMsg && (
        <div className="bg-blue-100 border border-blue-300 text-blue-800 p-2 rounded mb-3">
          {bulkResultMsg}
        </div>
      )}

      <div className="mb-3 flex flex-col gap-2 lg:flex-row lg:items-center">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filtrar por OS, cliente, equipo, estado, monto"
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar presupuestados"
        />
        <button
          className="btn"
          onClick={load}
          title="Recargar lista"
          disabled={loading || bulkApproving}
          aria-busy={loading ? "true" : "false"}
        >
          Recargar
        </button>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3 lg:ml-auto">
          <button
            className="btn"
            onClick={() => exportByIds(filtered.map(ingresoIdOf), `presupuestados_filtrados_${filtered.length}`)}
            disabled={exporting || bulkApproving || filtered.length === 0}
            aria-busy={exporting ? "true" : "false"}
            title="Exportar todos los filtrados a Excel"
          >
            Exportar filtrados
          </button>
          <button
            className="btn"
            onClick={() => exportByIds(Array.from(selectedIds), `presupuestados_seleccion_${selectedIds.size}`)}
            disabled={exporting || bulkApproving || selectedIds.size === 0}
            aria-busy={exporting ? "true" : "false"}
            title="Exportar elegidos a Excel"
          >
            Exportar elegidos
          </button>
          {canApprove ? (
            <button
              className="btn"
              onClick={aprobarSeleccion}
              disabled={approvingBusy || selectedIds.size === 0}
              aria-busy={bulkApproving ? "true" : "false"}
              title="Aprobar elegidos"
            >
              {bulkApproving ? "Aprobando..." : "Aprobar elegidos"}
            </button>
          ) : null}
        </div>
      </div>

      {loading ? (
        "Cargando..."
      ) : filtered.length === 0 ? (
        <div className="text-sm text-gray-500">No hay presupuestos emitidos/enviados.</div>
      ) : (
        <div>
          <MobileDataList>
            {filtered.map((row) => {
              const moneda = row?.presupuesto_moneda ?? "ARS";
              const monto = row?.presupuesto_monto ?? row?.presupuesto_total ?? null;
              const ingresoId = ingresoIdOf(row);
              return (
                <MobileDataCard
                  key={ingresoId}
                  onClick={() => go(row)}
                  onKeyDown={(e) => onRowKeyDown(e, row)}
                  className="cursor-pointer hover:bg-gray-50"
                  role="link"
                  tabIndex={0}
                  aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                  data-testid={`row-mobile-${ingresoId}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="font-semibold text-gray-900 underline">{formatOS(row)}</div>
                    <span onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={selectedIds.has(ingresoId)}
                        onChange={(e) => toggleRow(e, row)}
                        disabled={bulkApproving}
                        aria-label={`Seleccionar ${formatOS(row)}`}
                        className="h-5 w-5"
                      />
                    </span>
                  </div>
                  <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                    <MobileDataField label="Cliente" value={row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"} />
                    <MobileDataField label="Equipo" value={catalogEquipmentLabel(row) ?? "-"} />
                    <MobileDataField label="Serie">
                      <DeviceIdentifier row={row} />
                    </MobileDataField>
                    <MobileDataField label="Estado" value={row?.estado ?? "-"} />
                    <MobileDataField label="Monto" value={formatMoney(monto, moneda)} />
                    <MobileDataField label="Fecha emisión" value={formatDateOnly(row?.presupuesto_fecha_emision ?? row?.fecha_emision)} />
                  </div>
                  {canApprove ? (
                    <button
                      className="btn mt-3 w-full justify-center"
                      onClick={(e) => {
                        e.stopPropagation();
                        aprobar(row);
                      }}
                      disabled={approvingBusy}
                      aria-busy={approvingBusy ? "true" : "false"}
                      title="Aprobar presupuesto"
                    >
                      Aprobar
                    </button>
                  ) : null}
                </MobileDataCard>
              );
            })}
          </MobileDataList>
          <DesktopTableWrap>
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th scope="col" className="p-2">
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleSelectAllVisible}
                    disabled={bulkApproving}
                    aria-label="Seleccionar todos los visibles"
                  />
                </th>
                <th scope="col" className="p-2">OS</th>
                <th scope="col" className="p-2">Cliente</th>
                <th scope="col" className="p-2">Equipo</th>
                <th scope="col" className="p-2">Serie</th>
                <th scope="col" className="p-2">Estado</th>
                <th scope="col" className="p-2">Monto</th>
                <th scope="col" className="p-2">Fecha emisión</th>
                <th scope="col" className="p-2 text-right">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => {
                const moneda = row?.presupuesto_moneda ?? "ARS";
                const monto = row?.presupuesto_monto ?? row?.presupuesto_total ?? null;
                const ingresoId = ingresoIdOf(row);

                return (
                  <tr
                    key={ingresoId}
                    onClick={() => go(row)}
                    onKeyDown={(e) => onRowKeyDown(e, row)}
                    className="hover:bg-gray-50 cursor-pointer border-t"
                    role="link"
                    tabIndex={0}
                    aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                    data-testid={`row-${ingresoId}`}
                  >
                    <td className="p-2" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={selectedIds.has(ingresoId)}
                        onChange={(e) => toggleRow(e, row)}
                        disabled={bulkApproving}
                        aria-label={`Seleccionar ${formatOS(row)}`}
                      />
                    </td>
                    <td className="p-2 underline">{formatOS(row)}</td>
                    <td className="p-2">
                      {row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}
                    </td>
                    <td className="p-2">{catalogEquipmentLabel(row) ?? "-"}</td>
                    <td className="p-2"><DeviceIdentifier row={row} /></td>
                    <td className="p-2">{row?.estado ?? "-"}</td>
                    <td className="p-2">{formatMoney(monto, moneda)}</td>
                    <td className="p-2 whitespace-nowrap">
                      {formatDateOnly(row?.presupuesto_fecha_emision ?? row?.fecha_emision)}
                    </td>
                    <td className="p-2">
                      <div className="flex gap-2 justify-end">
                        {canApprove ? (
                          <button
                            className="btn"
                            onClick={(e) => {
                              e.stopPropagation();
                              aprobar(row);
                            }}
                            disabled={approvingBusy}
                            aria-busy={approvingBusy ? "true" : "false"}
                            title="Aprobar presupuesto"
                          >
                            Aprobar
                          </button>
                        ) : null}
                        {/* Si el backend permite rechazar o anular, se puede agregar otro botón acá. */}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </DesktopTableWrap>
          <div className="text-xs text-gray-500 mt-2">
            Mostrando {filtered.length} de {rows.length}.
          </div>
        </div>
      )}
    </div>
  );
}
