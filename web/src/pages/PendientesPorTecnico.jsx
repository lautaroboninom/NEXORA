import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getTecnicos } from "../lib/api";
import api from "../lib/api";
import WorkQueueTable from "../components/WorkQueueTable.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import useQueryState from "../hooks/useQueryState";
import useColumnFilters from "../hooks/useColumnFilters";
import { ingresoIdOf } from "../lib/ui-helpers";
import { matchesWorkQueueFilters } from "../lib/work-queue";
import { workQueueFilterOptions } from "../lib/work-queue-filter-options";

const UNASSIGNED_TECHNICIAN_FILTER = "sin_asignar";

export default function PendientesPorTecnico() {
  const [tecnicos, setTecnicos] = useState([]);
  const [tecnicoId, setTecnicoId] = useQueryState("tecnico_id", "");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();

  useEffect(() => {
    getTecnicos().then(setTecnicos).catch((e) => setErr(e.message));
  }, []);

  async function load() {
    if (!tecnicoId) {
      setRows([]);
      return;
    }
    setLoading(true);
    setErr("");
    try {
      const data = await api.get(`/api/ingresos/pendientes/?tecnico_id=${encodeURIComponent(tecnicoId)}`);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e?.message || "Error cargando pendientes");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tecnicoId]);

  const filtered = useMemo(
    () => rows.filter((row) => matchesWorkQueueFilters(row, { q, ...filters })),
    [rows, q, filters]
  );

  // El técnico ya viene elegido arriba, salvo en la lista de los sin asignar.
  const showTechnician = tecnicoId === UNASSIGNED_TECHNICIAN_FILTER;
  const filterOptions = useMemo(() => {
    const options = workQueueFilterOptions(rows);
    if (!showTechnician) delete options.tecnico;
    return options;
  }, [rows, showTechnician]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Pendientes por técnico</div>
      <AlertBanner tone="error">{err}</AlertBanner>

      <div className="mb-3">
        <select
          className="border rounded p-2 w-full max-w-xs"
          value={tecnicoId}
          onChange={(e) => setTecnicoId(e.target.value)}
          aria-label="Seleccionar técnico"
        >
          <option value="">-- Seleccionar técnico --</option>
          <option value={UNASSIGNED_TECHNICIAN_FILTER}>-- Sin técnico asignado --</option>
          {tecnicos.map((t) => (
            <option key={t.id} value={t.id}>{t.nombre}</option>
          ))}
        </select>
      </div>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, marca, equipo, serie"
        searchLabel="Filtrar pendientes por técnico"
        onReload={load}
        loading={loading}
        showFilterToggle
        filtersOpen={filtersOpen}
        onToggleFilters={() => setFiltersOpen((open) => !open)}
        activeFilterCount={activeCount}
        onClearFilters={clearFilters}
      />

      {!tecnicoId ? (
        <div className="text-sm text-gray-500">Elija un técnico o la opción sin asignar para ver los pendientes.</div>
      ) : (
        <WorkQueueTable
          rows={filtered}
          totalCount={rows.length}
          loading={loading}
          emptyText="Sin pendientes."
          onOpen={go}
          showTechnician={showTechnician}
          sort={sort}
          onSortChange={setSort}
          showFilters={filtersOpen}
          filters={filters}
          onFilterChange={setFilter}
          filterOptions={filterOptions}
        />
      )}
    </div>
  );
}
