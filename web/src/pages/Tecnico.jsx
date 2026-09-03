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

export default function Tecnico() {
  // Abrir la pantalla apaga el indicador de novedades del menú.
  useMarkQueueSeen(QUEUE_KEYS.MIS_PENDIENTES);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();

  const sortPendientes = (arr) => {
    return [...arr].sort((a, b) => {
      const aDev = a?.derivado_devuelto ? 1 : 0;
      const bDev = b?.derivado_devuelto ? 1 : 0;
      if (aDev !== bDev) return bDev - aDev;

      const au = (a?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      const bu = (b?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      if (au !== bu) return bu - au;

      const rawA = resolveFechaCreacion(a);
      const rawB = resolveFechaCreacion(b);
      const dtA = rawA ? new Date(rawA).getTime() : Number.POSITIVE_INFINITY;
      const dtB = rawB ? new Date(rawB).getTime() : Number.POSITIVE_INFINITY;
      return dtA - dtB;
    });
  };

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get("/api/tecnico/mis-pendientes/");
      setRows(sortPendientes(Array.isArray(data) ? data : []));
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

  const displayRows = useMemo(
    () => rows.filter((row) => matchesWorkQueueFilters(row, { q, ...filters })),
    [rows, q, filters]
  );

  // "Mis pendientes" son siempre del mismo técnico: ese filtro no aporta nada.
  const filterOptions = useMemo(() => {
    const { tecnico, ...resto } = workQueueFilterOptions(rows);
    return resto;
  }, [rows]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Mis pendientes</div>

      <AlertBanner tone="error">{err}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, equipo, serie, estado..."
        searchLabel="Filtrar mis pendientes"
        onReload={load}
        loading={loading}
        showFilterToggle
        filtersOpen={filtersOpen}
        onToggleFilters={() => setFiltersOpen((open) => !open)}
        activeFilterCount={activeCount}
        onClearFilters={clearFilters}
      />

      <WorkQueueTable
        rows={displayRows}
        totalCount={rows.length}
        loading={loading}
        emptyText={q ? "No se encontraron pendientes para el filtro aplicado." : "No tiene pendientes por ahora."}
        onOpen={go}
        showTechnician={false}
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
