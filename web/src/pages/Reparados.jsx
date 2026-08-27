// web/src/pages/Reparados.jsx
import { useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import { useNavigate } from "react-router-dom";
import {
  ingresoIdOf,
  formatOS,
  formatDateOnly,
  norm,
  tipoEquipoOf,
  resolveFechaIngreso,
  catalogEquipmentLabel,
} from "../lib/ui-helpers";
import { estadoLabel } from "../lib/constants";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
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
import { isJefe } from "../lib/authz";
import {
  columnFilterOptions,
  matchesColumnFilters,
} from "../lib/ingreso-list-filters";

const ENDPOINT = "/api/ingresos/aprobados-reparados/";

const COLUMNS = [
  { field: "os", label: "OS", cellClassName: "underline", render: (row) => formatOS(row), filter: "text" },
  {
    field: "estado",
    label: "Estado",
    render: (row) => <StatusChip value={row?.estado} />,
    filter: "select",
    filterLabel: (value) => estadoLabel(value),
  },
  {
    field: "cliente",
    label: "Cliente",
    render: (row) => row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-",
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

export default function Reparados() {
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

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get(ENDPOINT);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los reparados");
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
      title="Revisar salida"
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
        canManageResolution={isJefe(user)}
        onClose={() => setReleaseRow(null)}
        onReleased={load}
      />
      <BulkReleaseModal
        open={Boolean(bulkRows)}
        rows={bulkRows || []}
        canManageResolution={isJefe(user)}
        onClose={() => setBulkRows(null)}
        onDone={onBulkDone}
      />
      <div className="h1 mb-3">Reparados</div>

      <AlertBanner tone="error">{err}</AlertBanner>
      <AlertBanner tone="info">{bulkResultMsg}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, marca, equipo, serie"
        searchLabel="Filtrar reparados"
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
        emptyText="No hay reparados que coincidan con el filtro."
        totalCount={rows.length}
      />
    </div>
  );
}
