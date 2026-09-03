// web/src/pages/ListosParaLiberar.jsx
import { useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import { useNavigate } from "react-router-dom";
import {
  ingresoCustomerName,
  ingresoIdOf,
  formatOS,
  formatDateOnly,
  norm,
  tipoEquipoOf,
  resolveFechaIngreso,
  catalogEquipmentLabel,
} from "../lib/ui-helpers";
import { estadoLabel } from "../lib/constants";
import CustomerCell from "../components/CustomerCell.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DerivadoMark } from "../components/DerivadoBadge.jsx";
import StatusChip from "../components/StatusChip.jsx";
import ReleaseOrderModal from "../components/ReleaseOrderModal.jsx";
import BulkReleaseModal from "../components/BulkReleaseModal.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import IngresoTable from "../components/IngresoTable.jsx";
import useQueryState from "../hooks/useQueryState";
import useRowSelection from "../hooks/useRowSelection";
import useColumnFilters from "../hooks/useColumnFilters";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import {
  columnFilterOptions,
  matchesColumnFilters,
} from "../lib/ingreso-list-filters";
import { QUEUE_KEYS } from "../lib/queue-badges";
import { useMarkQueueSeen } from "../hooks/useQueueBadges";

const ENDPOINT = "/api/ingresos/aprobados-reparados/";

// Mismo orden que el resto de las colas (OS · Cliente · Equipo · Estado · Serie ·
// fechas): el operador barre las listas de arriba abajo buscando siempre el
// cliente en la misma columna.
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
    field: "estado",
    label: "Estado",
    render: (row) => <StatusChip value={row?.estado} />,
    filter: "select",
    filterLabel: (value) => estadoLabel(value),
  },
  {
    field: "identificacion",
    label: "Serie",
    render: (row) => <DeviceIdentifier row={row} />,
    filter: "text",
  },
  {
    field: "fecha_ingreso",
    label: "Fecha ingreso",
    cellClassName: "whitespace-nowrap",
    render: (row) => formatDateOnly(resolveFechaIngreso(row)),
  },
  {
    field: "fecha_reparado",
    label: "Fecha reparación",
    cellClassName: "whitespace-nowrap",
    render: (row) =>
      formatDateOnly(
        row?.fecha_reparado ?? row?.fecha_reparacion ?? row?.reparado_fecha ?? row?.estado_fecha
      ),
  },
];

export default function ListosParaLiberar() {
  // Abrir la pantalla apaga el indicador de novedades del menú.
  useMarkQueueSeen(QUEUE_KEYS.LISTOS_PARA_LIBERAR);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const [releaseRow, setReleaseRow] = useState(null);
  const [bulkRows, setBulkRows] = useState(null);
  const [bulkResultMsg, setBulkResultMsg] = useState("");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();
  const { user } = useAuth();
  const release = can(user, PERMISSION_CODES.ACTION_INGRESO_PRINT_EXIT_ORDER);
  // El backend exige `repair_transitions` para cerrar la reparación, no el rol jefe.
  const canRepairTransitions = can(user, PERMISSION_CODES.ACTION_INGRESO_REPAIR_TRANSITIONS);
  const canSwapDevice = can(user, PERMISSION_CODES.ACTION_INGRESO_DEVICE_SWAP);

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get(ENDPOINT);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los listos para liberar");
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
      ];
      return campos.some((c) => norm(c).includes(needle));
    });
  }, [rows, q, filters]);

  const filterOptions = useMemo(() => columnFilterOptions(rows, COLUMNS), [rows]);

  const visibleIds = useMemo(() => filtered.map(ingresoIdOf).filter(Boolean), [filtered]);
  const selection = useRowSelection(visibleIds);

  function abrirLiberacionMultiple() {
    const elegidas = selection.selectedRowsIn(rows);
    if (elegidas.length === 0) return;
    setErr("");
    setBulkResultMsg("");
    // Con una sola OS conviene el modal de siempre: es el único que deja al
    // jefe elegir la resolución antes de liberar.
    if (elegidas.length === 1) {
      setReleaseRow(elegidas[0]);
      return;
    }
    setBulkRows(elegidas);
  }

  async function onBulkDone({ emitidos = [], fallidos = [] } = {}) {
    selection.remove(emitidos);
    setBulkResultMsg(
      fallidos.length > 0
        ? `Se liberaron ${emitidos.length} órdenes y ${fallidos.length} quedaron pendientes.`
        : `Se liberaron ${emitidos.length} órdenes.`
    );
    await load();
  }

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  const rowActions = (row) => (
    <button
      className="btn"
      type="button"
      title={
        row?.derivacion_activa
          ? "El equipo sigue derivado. Marcá la derivación como devuelta antes de liberarlo."
          : "Revisar salida"
      }
      disabled={Boolean(row?.derivacion_activa)}
      onClick={() => {
        setErr("");
        setReleaseRow(row);
      }}
    >
      Revisar salida
    </button>
  );

  return (
    <div className="card">
      <ReleaseOrderModal
        open={Boolean(releaseRow)}
        row={releaseRow}
        canManageResolution={canRepairTransitions}
        canSwapDevice={canSwapDevice}
        onClose={() => setReleaseRow(null)}
        onReleased={load}
        onSwapChanged={load}
      />
      <BulkReleaseModal
        open={Boolean(bulkRows)}
        rows={bulkRows || []}
        canManageResolution={canRepairTransitions}
        onClose={() => setBulkRows(null)}
        onDone={onBulkDone}
      />
      <div className="h1 mb-3">Listos para liberar</div>

      <AlertBanner tone="error">{err}</AlertBanner>
      <AlertBanner tone="info">{bulkResultMsg}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, marca, equipo, serie"
        searchLabel="Filtrar listos para liberar"
        onReload={load}
        loading={loading}
        showFilterToggle
        filtersOpen={filtersOpen}
        onToggleFilters={() => setFiltersOpen((open) => !open)}
        activeFilterCount={activeCount}
        onClearFilters={clearFilters}
        selectedCount={release ? selection.count : 0}
      >
        {release ? (
          <button
            className="btn"
            type="button"
            onClick={abrirLiberacionMultiple}
            disabled={selection.count === 0}
            title="Liberar las órdenes elegidas e imprimir todas juntas"
          >
            Liberar elegidas
          </button>
        ) : null}
      </ListToolbar>

      <IngresoTable
        rows={filtered}
        columns={COLUMNS}
        sort={sort}
        onSortChange={setSort}
        selection={release ? selection : undefined}
        selectionLabel={(row) => `Seleccionar ${formatOS(row)}`}
        onOpen={go}
        openLabel={(row) => `Abrir hoja de servicio de ${formatOS(row)}`}
        rowActions={release ? rowActions : undefined}
        filters={filters}
        onFilterChange={setFilter}
        filterOptions={filterOptions}
        showFilters={filtersOpen}
        loading={loading}
        emptyText="No hay equipos listos para liberar que coincidan con el filtro."
        totalCount={rows.length}
      />
    </div>
  );
}
