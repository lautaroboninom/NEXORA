import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { postDerivacionDevuelto } from "../lib/api";
import {
  ingresoIdOf,
  formatOS,
  formatDateOnly,
  norm,
  tipoEquipoOf,
  catalogEquipmentLabel,
} from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
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
  { field: "os", label: "OS", cellClassName: "underline", render: (row) => formatOS(row), filter: "text" },
  {
    field: "cliente",
    label: "Cliente",
    render: (row) => row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-",
    filter: "text",
  },
  { field: "proveedor", label: "Proveedor", render: (row) => row?.proveedor ?? "-", filter: "select" },
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
    field: "fecha_derivacion",
    label: "Fecha derivación",
    cellClassName: "whitespace-nowrap",
    render: (row) => (row?.fecha_deriv ? formatDateOnly(row.fecha_deriv) : "-"),
  },
];

export default function Derivados() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [fechaMap, setFechaMap] = useState({}); // {ingreso_id: 'YYYY-MM-DD'}
  const [busyId, setBusyId] = useState(null);
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();

  const load = async () => {
    try {
      setErr(""); setLoading(true);
      const data = await api.get("/api/ingresos/derivados/");
      setRows(Array.isArray(data) ? data : []);
      // preset fechas por fila (hoy)
      const today = new Date().toISOString().slice(0,10);
      const m = {};
      (Array.isArray(data) ? data : []).forEach(r => { m[ingresoIdOf(r)] = today; });
      setFechaMap(m);
    } catch (e) {
      setErr(e?.message || "No se pudo cargar la lista de derivados");
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const sorted = useMemo(() => {
    // ms recientes primero por fecha_deriv (tratar YYYY-MM-DD como local)
    const toDate = (v) => (v && typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) ? new Date(`${v}T00:00:00`) : (v ? new Date(v) : new Date(0));
    return [...rows].sort((a,b) => toDate(b?.fecha_deriv) - toDate(a?.fecha_deriv));
  }, [rows]);

  const filtered = useMemo(() => {
    const needle = norm(q);
    return sorted.filter((row) => {
      if (!matchesColumnFilters(row, COLUMNS, filters)) return false;
      if (!needle) return true;
      const campos = [
        formatOS(row),
        row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
        row?.proveedor,
        row?.marca ?? row?.equipo?.marca,
        catalogEquipmentLabel(row),
        tipoEquipoOf(row),
        row?.numero_serie,
        row?.numero_interno,
      ];
      return campos.some((c) => norm(c).includes(needle));
    });
  }, [sorted, q, filters]);

  const filterOptions = useMemo(() => columnFilterOptions(rows, COLUMNS), [rows]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  const onDevuelto = async (row) => {
    const ingresoId = ingresoIdOf(row);
    const f = fechaMap[ingresoId] || null;
    try {
      setBusyId(ingresoId);
      setErr("");
      await postDerivacionDevuelto(ingresoId, (row?.deriv_id ?? row?.id), { fecha_entrega: f });
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo marcar como devuelto");
    } finally {
      setBusyId(null);
    }
  };

  const rowActions = (row, { mobile } = {}) => {
    const ingresoId = ingresoIdOf(row);
    return (
      <div
        className={
          mobile
            ? "grid grid-cols-1 gap-2 min-[420px]:grid-cols-[1fr_auto]"
            : "flex items-center gap-2 justify-end"
        }
      >
        <input
          type="date"
          value={fechaMap[ingresoId] || ""}
          onChange={(e) => setFechaMap((m) => ({ ...m, [ingresoId]: e.target.value }))}
          className={mobile ? "h-10 rounded border px-2" : "border rounded p-1"}
          aria-label={`Fecha de devolución de ${formatOS(row)}`}
        />
        <button
          className={mobile ? "btn justify-center" : "btn"}
          type="button"
          onClick={() => onDevuelto(row)}
          disabled={busyId === ingresoId}
          aria-busy={busyId === ingresoId ? "true" : "false"}
          title="Marcar la derivación como devuelta"
        >
          Devuelto
        </button>
      </div>
    );
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Derivados</div>

      <AlertBanner tone="error">{err}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, proveedor, equipo, serie"
        searchLabel="Filtrar derivados"
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
        rowActions={rowActions}
        filters={filters}
        onFilterChange={setFilter}
        filterOptions={filterOptions}
        showFilters={filtersOpen}
        loading={loading}
        emptyText="No hay equipos derivados."
        totalCount={rows.length}
      />
    </div>
  );
}
