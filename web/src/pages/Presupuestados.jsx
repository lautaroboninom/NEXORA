// web/src/pages/Presupuestados.jsx
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
  ingresoCustomerName,
  ingresoIdOf,
  norm,
  resolveFechaCreacion,
  tipoEquipoOf,
} from "../lib/ui-helpers";
import CustomerCell from "../components/CustomerCell.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DerivadoMark } from "../components/DerivadoBadge.jsx";
import StatusChip from "../components/StatusChip.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import IngresoTable from "../components/IngresoTable.jsx";
import useQueryState from "../hooks/useQueryState";
import useRowSelection from "../hooks/useRowSelection";
import useColumnFilters from "../hooks/useColumnFilters";
import { reservePdfWindow } from "../lib/pdf";
import { estadoLabel } from "../lib/constants";
import {
  columnFilterOptions,
  matchesColumnFilters,
} from "../lib/ingreso-list-filters";

// ENDPOINT para "presupuestados" (ya emitidos/enviados)
const ENDPOINT = "/api/ingresos/presupuestados/";

const COLUMNS = [
  {
    field: "os",
    label: "OS",
    cellClassName: "underline",
    render: (row) => (
      <>
        {formatOS(row)}
        <DerivadoMark row={row} className="ml-1.5" />
      </>
    ),
    filter: "text",
  },
  {
    field: "cliente",
    label: "Cliente",
    render: (row) => <CustomerCell row={row} />,
    filter: "text",
  },
  {
    field: "equipo",
    label: "Equipo",
    render: (row) => catalogEquipmentLabel(row) ?? "-",
    filter: "select",
  },
  {
    field: "identificacion",
    label: "Serie",
    render: (row) => <DeviceIdentifier row={row} />,
    filter: "text",
  },
  {
    field: "estado",
    label: "Estado",
    render: (row) => <StatusChip value={row?.estado} />,
    filter: "select",
    filterLabel: (value) => estadoLabel(value),
  },
  {
    field: "monto",
    label: "Monto",
    cellClassName: "whitespace-nowrap",
    render: (row) =>
      formatMoney(
        row?.presupuesto_monto ?? row?.presupuesto_total ?? null,
        row?.presupuesto_moneda ?? "ARS"
      ),
  },
  {
    field: "fecha_emision",
    label: "Fecha emisión",
    cellClassName: "whitespace-nowrap",
    render: (row) => formatDateOnly(row?.presupuesto_fecha_emision ?? row?.fecha_emision),
  },
];

export default function JefePresupuestos() {
  const { user } = useAuth();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [bulkResultMsg, setBulkResultMsg] = useState("");
  const [bulkFailures, setBulkFailures] = useState([]);
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const [busyId, setBusyId] = useState(null);
  const [bulkApproving, setBulkApproving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);

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
    return rows.filter((row) => {
      if (!matchesColumnFilters(row, COLUMNS, filters)) return false;
      if (!needle) return true;
      const campos = [
        formatOS(row),
        ingresoCustomerName(row),
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
  }, [rows, q, filters]);

  const filterOptions = useMemo(() => columnFilterOptions(rows, COLUMNS), [rows]);

  const rowById = useMemo(() => {
    const map = new Map();
    for (const row of rows) {
      const id = ingresoIdOf(row);
      if (id == null || id === "") continue;
      map.set(id, row);
    }
    return map;
  }, [rows]);

  const visibleIds = useMemo(() => filtered.map(ingresoIdOf).filter(Boolean), [filtered]);
  const selection = useRowSelection(visibleIds);

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

  function isReparado(row) {
    return norm(row?.estado) === "reparado";
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
    reservedWindow?.update?.({
      status: ids.length > 1 ? "Generando las órdenes de salida" : "Generando la orden de salida",
      step: "pdf",
    });
    const blob = await getBlob(remitosSalidaPath(ids));
    if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");
    reservedWindow?.update?.({ status: "Abriendo impresión", step: "print", state: "ready" });
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
      const variasOrdenes = reparadoRows.length > 1;
      reservedPrintWindow = reservePdfWindow({
        title: variasOrdenes ? "Órdenes de salida - NEXORA" : "Orden de salida - NEXORA",
        heading: variasOrdenes ? "Preparando las órdenes de salida" : "Preparando la orden de salida",
        message: variasOrdenes
          ? `NEXORA está aprobando ${reparadoRows.length} presupuestos`
          : "NEXORA está aprobando el presupuesto",
        detail: "Esta pestaña muestra la orden de salida cuando el PDF esté listo.",
        fallbackMessage:
          "NEXORA sigue preparando el PDF de la orden de salida. Si ya terminó, puede cerrar esta pestaña y reintentar la impresión.",
        // La orden de salida la arma NEXORA: acá no hay emisión en Bejerman.
        steps: [
          { id: "pdf", label: variasOrdenes ? "Generando las órdenes de salida" : "Generando la orden de salida" },
          { id: "print", label: "Abriendo la impresión" },
        ],
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
        reservedPrintWindow?.close?.();
      }
    } else {
      reservedPrintWindow?.close?.();
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
      setBulkFailures([]);
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
    if (!canApprove || bulkApproving || busyId !== null || selection.count === 0) return;
    const selectedSnapshot = Array.from(selection.ids);
    setErr("");
    setBulkResultMsg("");
    setBulkFailures([]);
    setBulkApproving(true);
    try {
      // En el orden de la lista, no en el que se fueron tildando: ese orden
      // es el que termina teniendo el PDF de las órdenes de salida.
      const selectedRows = selection.selectedRowsIn(rows);
      const presentes = new Set(selectedRows.map(ingresoIdOf));
      const missingIds = selectedSnapshot.filter((id) => !presentes.has(id));

      const reparadosCount = selectedRows.filter(isReparado).length;
      const result = await approveRows(selectedRows, {
        askPrint: true,
        confirmPrintMessage: `Hay ${reparadosCount} equipos reparados. ¿Imprimir todas las órdenes de salida juntas?`,
      });
      const totalApprovalFailures = missingIds.length + result.approvalFailures.length;

      await load();

      selection.remove([...result.approvedIds, ...missingIds]);

      // El detalle por OS es lo único accionable cuando falla parte del lote:
      // saber que "fallaron 3" no dice cuáles hay que reintentar ni por qué.
      const failures = [
        ...missingIds.map((id) => ({
          id,
          detail: "Ya no está en la lista: puede haber cambiado de estado.",
        })),
        ...result.approvalFailures.map((item) => ({
          id: item.ingresoId,
          detail: item.error?.message || "No se pudo aprobar el presupuesto.",
        })),
        ...result.printFailures.map((item) => ({
          id: item.ingresoId,
          detail: item.error?.message || "No se pudo imprimir la orden de salida.",
        })),
      ];
      setBulkFailures(failures);

      const summary = [
        `Elegidos: ${selectedSnapshot.length}.`,
        `Aprobados: ${result.approvedIds.length}.`,
        `Órdenes de salida impresas: ${result.printedIds.length}.`,
      ];
      if (totalApprovalFailures > 0) summary.push(`Fallos de aprobación: ${totalApprovalFailures}.`);
      if (result.printFailures.length > 0) {
        summary.push(`Fallos de impresión: ${result.printFailures.length}.`);
      }
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

  const rowActions = (row) => (
    <button
      className="btn"
      type="button"
      onClick={() => aprobar(row)}
      disabled={approvingBusy}
      aria-busy={approvingBusy ? "true" : "false"}
      title="Aprobar presupuesto"
    >
      Aprobar
    </button>
  );

  return (
    <div className="card">
      <div className="h1 mb-3">Presupuestados</div>

      <AlertBanner tone="error">{err}</AlertBanner>

      {bulkResultMsg && (
        <AlertBanner tone={bulkFailures.length > 0 ? "warning" : "info"}>
          <div>{bulkResultMsg}</div>
          {bulkFailures.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-sm">
              {bulkFailures.map((item) => (
                <li key={`fail-${item.id}`}>
                  <span className="font-medium">
                    {formatOS(rowById.get(item.id) || { id: item.id })}
                  </span>
                  : {item.detail}
                </li>
              ))}
            </ul>
          )}
        </AlertBanner>
      )}

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, equipo, estado, monto"
        searchLabel="Filtrar presupuestados"
        onReload={load}
        loading={loading || bulkApproving}
        showFilterToggle
        filtersOpen={filtersOpen}
        onToggleFilters={() => setFiltersOpen((open) => !open)}
        activeFilterCount={activeCount}
        onClearFilters={clearFilters}
        selectedCount={selection.count}
      >
        <button
          className="btn"
          type="button"
          onClick={() => exportByIds(filtered.map(ingresoIdOf), `presupuestados_filtrados_${filtered.length}`)}
          disabled={exporting || bulkApproving || filtered.length === 0}
          aria-busy={exporting ? "true" : "false"}
          title="Exportar todos los filtrados a Excel"
        >
          Exportar filtrados
        </button>
        <button
          className="btn"
          type="button"
          onClick={() =>
            exportByIds(
              selection.selectedRowsIn(rows).map(ingresoIdOf),
              `presupuestados_seleccion_${selection.count}`
            )
          }
          disabled={exporting || bulkApproving || selection.count === 0}
          aria-busy={exporting ? "true" : "false"}
          title="Exportar elegidos a Excel"
        >
          Exportar elegidos
        </button>
        {canApprove ? (
          <button
            className="btn"
            type="button"
            onClick={aprobarSeleccion}
            disabled={approvingBusy || selection.count === 0}
            aria-busy={bulkApproving ? "true" : "false"}
            title="Aprobar elegidos"
          >
            {bulkApproving ? "Aprobando..." : "Aprobar elegidos"}
          </button>
        ) : null}
      </ListToolbar>

      <IngresoTable
        rows={filtered}
        columns={COLUMNS}
        sort={sort}
        onSortChange={setSort}
        selection={selection}
        selectionLabel={(row) => `Seleccionar ${formatOS(row)}`}
        onOpen={go}
        openLabel={(row) => `Abrir hoja de servicio de ${formatOS(row)}`}
        rowActions={canApprove ? rowActions : undefined}
        filters={filters}
        onFilterChange={setFilter}
        filterOptions={filterOptions}
        showFilters={filtersOpen}
        loading={loading}
        emptyText="No hay presupuestos emitidos/enviados."
        totalCount={rows.length}
      />
    </div>
  );
}
