// web/src/pages/PendientesPresupuesto.jsx
import { useEffect, useMemo, useRef, useState } from "react";
import { getPendientesPresupuesto } from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, resolveFechaIngreso, catalogEquipmentLabel } from "../lib/ui-helpers";
import { estadoLabel } from "../lib/constants";
import StatusChip from "../components/StatusChip.jsx";
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


const SCROLL_STORAGE_KEY = "nexora:pendientes-presupuesto:scroll-y";

const COLUMNS = [
  { field: "os", label: "OS", cellClassName: "underline", render: (row) => formatOS(row), filter: "text" },
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
    field: "estado",
    label: "Estado",
    render: (row) => <StatusChip value={row?.estado} title="Estado del equipo" />,
    filter: "select",
    filterLabel: (value) => estadoLabel(value),
  },
  {
    field: "identificacion",
    label: "N/S",
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
    field: "fecha_servicio",
    label: "Fecha servicio",
    cellClassName: "whitespace-nowrap",
    render: (row) => formatDateOnly(row?.fecha_servicio),
  },
];

export default function PendientesPresupuesto() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();
  const restoredScrollRef = useRef(false);

  useEffect(() => {
    const saveScroll = () => {
      try {
        sessionStorage.setItem(SCROLL_STORAGE_KEY, String(window.scrollY || 0));
      } catch {
        // La posición es una mejora opcional; no debe bloquear la navegación.
      }
    };

    window.addEventListener("scroll", saveScroll, { passive: true });
    return () => {
      saveScroll();
      window.removeEventListener("scroll", saveScroll);
    };
  }, []);

  useEffect(() => {
    if (loading || restoredScrollRef.current) return;
    restoredScrollRef.current = true;

    let savedY = 0;
    try {
      savedY = Math.max(0, Number(sessionStorage.getItem(SCROLL_STORAGE_KEY) || 0));
    } catch {
      savedY = 0;
    }
    if (!savedY) return;

    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({ top: savedY, left: 0, behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [loading]);

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const list = await getPendientesPresupuesto();
      // Ordenar por fecha de servicio: ms antiguo primero
       list.sort((a, b) => {
        const da = new Date(a?.fecha_servicio ?? 0).getTime();
        const db = new Date(b?.fecha_servicio ?? 0).getTime();
        return da - db;
      });
      setRows(list);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los pendientes de presupuesto");
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
        row?.presupuesto_estado,
        String(row?.presupuesto_numero ?? ""),
        String(row?.presupuesto_monto ?? ""),
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
      <div className="h1 mb-3">Pendientes de Presupuesto</div>

      <AlertBanner tone="error">{err}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, equipo, NS, estado, presupuesto"
        searchLabel="Filtrar pendientes de presupuesto"
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
        emptyText="No hay pendientes que coincidan con el filtro."
        totalCount={rows.length}
      />
    </div>
  );
}
