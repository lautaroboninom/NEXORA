import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import WorkQueueTable from "../components/WorkQueueTable.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import useQueryState from "../hooks/useQueryState";
import useColumnFilters from "../hooks/useColumnFilters";
import { ingresoIdOf, resolveFechaCreacion } from "../lib/ui-helpers";
import { matchesWorkQueueFilters } from "../lib/work-queue";
import { workQueueFilterOptions } from "../lib/work-queue-filter-options";
import { QUEUE_KEYS } from "../lib/queue-badges";
import { useMarkQueueSeen } from "../hooks/useQueueBadges";

const ENDPOINT = "/api/ingresos/pendientes/";

export default function PendientesGeneral() {
  // Abrir la pantalla apaga el indicador de novedades del menú.
  useMarkQueueSeen(QUEUE_KEYS.PENDIENTES_GENERAL);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  // Si se entra con filtros puestos (un link guardado), el panel arranca abierto.
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get(ENDPOINT);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los pendientes");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const defaultSortedRows = useMemo(() => {
    return [...rows].sort((a, b) => {
      const ad = a?.derivado_devuelto ? 1 : 0;
      const bd = b?.derivado_devuelto ? 1 : 0;
      if (ad !== bd) return bd - ad;

      const au = (a?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      const bu = (b?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      if (au !== bu) return bu - au;

      const rawA = resolveFechaCreacion(a);
      const rawB = resolveFechaCreacion(b);
      const dtA = rawA ? new Date(rawA).getTime() : Number.POSITIVE_INFINITY;
      const dtB = rawB ? new Date(rawB).getTime() : Number.POSITIVE_INFINITY;
      return dtA - dtB;
    });
  }, [rows]);

  const filterOptions = useMemo(() => workQueueFilterOptions(rows), [rows]);

  const filteredAndSorted = useMemo(() => {
    return defaultSortedRows.filter((row) => matchesWorkQueueFilters(row, { q, ...filters }));
  }, [defaultSortedRows, q, filters]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Pendientes general</div>

      <AlertBanner tone="error">{err}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, equipo, estado, serie..."
        searchLabel="Filtrar pendientes"
        onReload={load}
        loading={loading}
        showFilterToggle
        filtersOpen={filtersOpen}
        onToggleFilters={() => setFiltersOpen((open) => !open)}
        activeFilterCount={activeCount}
        onClearFilters={clearFilters}
      />

      <WorkQueueTable
        rows={filteredAndSorted}
        totalCount={rows.length}
        loading={loading}
        emptyText="No hay pendientes que coincidan con el filtro."
        onOpen={go}
        sort={sort}
        onSortChange={setSort}
        showFilters={filtersOpen}
        filters={filters}
        onFilterChange={setFilter}
        filterOptions={filterOptions}
      />
    </div>
  );
}
