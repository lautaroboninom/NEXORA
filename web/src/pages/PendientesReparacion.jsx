// web/src/pages/PendientesReparacion.jsx
import { useEffect, useMemo, useState } from "react";
import { getAprobadosParaReparar } from "../lib/api";
import { useNavigate } from "react-router-dom";
import {
  ingresoCustomerName,
  ingresoIdOf,
  formatOS,
  formatDateOnly,
  norm,
  tipoEquipoOf,
  catalogEquipmentLabel,
} from "../lib/ui-helpers";
import { estadoLabel } from "../lib/constants";
import StatusChip from "../components/StatusChip.jsx";
import CustomerCell from "../components/CustomerCell.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DerivadoMark } from "../components/DerivadoBadge.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import IngresoTable from "../components/IngresoTable.jsx";
import useQueryState from "../hooks/useQueryState";
import useColumnFilters from "../hooks/useColumnFilters";
import {
  columnFilterOptions,
  matchesColumnFilters,
} from "../lib/ingreso-list-filters";

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
    field: "fecha_aprobado",
    label: "Fecha aprob.",
    cellClassName: "whitespace-nowrap",
    // Las OS que entran por `estado='reparar'` sin presupuesto aprobado (garantía,
    // sin cargo) no tienen fecha de aprobación: la columna queda vacía a propósito.
    render: (row) => formatDateOnly(row?.fecha_aprobado || row?.presupuesto_fecha_aprobacion),
  },
];

export default function PendientesReparacion() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await getAprobadosParaReparar();
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los pendientes de reparación");
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
        row?.resolucion,
        row?.numero_serie,
        row?.numero_interno,
      ];
      return campos.some((c) => norm(c).includes(needle));
    });
  }, [rows, q, filters]);

  const filterOptions = useMemo(() => columnFilterOptions(rows, COLUMNS), [rows]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Pendientes de reparación</div>

      <AlertBanner tone="error">{err}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, marca, equipo, serie"
        searchLabel="Filtrar pendientes de reparación"
        onReload={load}
        loading={loading}
        showFilterToggle
        filtersOpen={filtersOpen}
        onToggleFilters={() => setFiltersOpen((open) => !open)}
        activeFilterCount={activeCount}
        onClearFilters={clearFilters}
      />

      <IngresoTable
        rows={filtered}
        columns={COLUMNS}
        sort={sort}
        onSortChange={setSort}
        onOpen={go}
        openLabel={(row) => `Abrir hoja de servicio de ${formatOS(row)}`}
        filters={filters}
        onFilterChange={setFilter}
        filterOptions={filterOptions}
        showFilters={filtersOpen}
        loading={loading}
        emptyText="No hay equipos pendientes de reparación que coincidan con el filtro."
        totalCount={rows.length}
      />
    </div>
  );
}
