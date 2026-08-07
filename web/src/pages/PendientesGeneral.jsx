import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import WorkQueueTable from "../components/WorkQueueTable.jsx";
import useQueryState from "../hooks/useQueryState";
import {
  ingresoIdOf,
  norm,
  resolveFechaCreacion,
  tipoEquipoOf,
} from "../lib/ui-helpers";
import {
  matchesWorkQueueFilters,
  WORK_QUEUE_EMPTY_FILTER,
  workQueueNextAction,
  workQueueTechnician,
} from "../lib/work-queue";

const ENDPOINT = "/api/ingresos/pendientes/";
const PRIORITY_OPTIONS = [
  { value: "devuelto", label: "Devuelto" },
  { value: "urgente", label: "Urgente" },
  { value: "presupuesto", label: "Presupuesto" },
  { value: "sin_tecnico", label: "Sin técnico" },
  { value: "16_mas", label: "16+ días" },
  { value: "espera", label: "En espera" },
  { value: "normal", label: "Normal" },
];

const AGE_OPTIONS = [
  { value: "0_6", label: "0 a 6 días" },
  { value: "7_15", label: "7 a 15 días" },
  { value: "16_30", label: "16 a 30 días" },
  { value: "31_mas", label: "31 días o más" },
];

const PROPERTY_OPTIONS = [
  { value: "MG BIO", label: "MG BIO" },
  { value: "Cliente", label: "Cliente" },
  { value: "Cliente (MG histórico)", label: "Cliente (MG histórico)" },
];

function uniqueOptions(rows, accessor, emptyLabel = "") {
  const values = new Map();
  for (const row of rows || []) {
    const raw = accessor(row);
    const value = String(raw ?? "").trim();
    if (!value) continue;
    const key = norm(value);
    if (!values.has(key)) values.set(key, value);
  }
  const options = Array.from(values.values())
    .sort((a, b) => a.localeCompare(b, "es-AR", { numeric: true, sensitivity: "base" }))
    .map((value) => ({ value, label: value }));
  if (emptyLabel) options.unshift({ value: WORK_QUEUE_EMPTY_FILTER, label: emptyLabel });
  return options;
}

export default function PendientesGeneral() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const [prioridad, setPrioridad] = useQueryState("prioridad", "");
  const [os, setOs] = useQueryState("os", "");
  const [antiguedad, setAntiguedad] = useQueryState("antiguedad", "");
  const [cliente, setCliente] = useQueryState("cliente", "");
  const [propiedad, setPropiedad] = useQueryState("propiedad", "");
  const [tipo, setTipo] = useQueryState("tipo", "");
  const [marca, setMarca] = useQueryState("marca", "");
  const [modelo, setModelo] = useQueryState("modelo", "");
  const [variante, setVariante] = useQueryState("variante", "");
  const [identificacion, setIdentificacion] = useQueryState("identificacion", "");
  const [tecnico, setTecnico] = useQueryState("tecnico", "");
  const [motivo, setMotivo] = useQueryState("motivo", "");
  const [estado, setEstado] = useQueryState("estado", "");
  const [presupuesto, setPresupuesto] = useQueryState("presupuesto", "");
  const [accion, setAccion] = useQueryState("accion", "");
  const navigate = useNavigate();

  const filters = {
    prioridad,
    os,
    antiguedad,
    cliente,
    propiedad,
    tipo,
    marca,
    modelo,
    variante,
    identificacion,
    tecnico,
    motivo,
    estado,
    presupuesto,
    accion,
  };

  const filterSetters = {
    prioridad: setPrioridad,
    os: setOs,
    antiguedad: setAntiguedad,
    cliente: setCliente,
    propiedad: setPropiedad,
    tipo: setTipo,
    marca: setMarca,
    modelo: setModelo,
    variante: setVariante,
    identificacion: setIdentificacion,
    tecnico: setTecnico,
    motivo: setMotivo,
    estado: setEstado,
    presupuesto: setPresupuesto,
    accion: setAccion,
  };

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

  const filterOptions = useMemo(() => ({
    prioridad: PRIORITY_OPTIONS,
    antiguedad: AGE_OPTIONS,
    propiedad: PROPERTY_OPTIONS,
    tipo: uniqueOptions(rows, tipoEquipoOf),
    marca: uniqueOptions(rows, (row) => row?.marca ?? row?.equipo?.marca),
    modelo: uniqueOptions(rows, (row) => row?.modelo ?? row?.equipo?.modelo),
    variante: uniqueOptions(
      rows,
      (row) => row?.equipo_variante ?? row?.variante ?? row?.equipo?.variante
    ),
    tecnico: uniqueOptions(rows, workQueueTechnician, "Sin técnico"),
    motivo: uniqueOptions(rows, (row) => row?.motivo),
    estado: uniqueOptions(rows, (row) => row?.estado),
    presupuesto: uniqueOptions(rows, (row) => row?.presupuesto_estado, "Sin presupuesto"),
    accion: uniqueOptions(rows, workQueueNextAction),
  }), [rows]);

  const filteredAndSorted = useMemo(() => {
    return defaultSortedRows.filter((row) => matchesWorkQueueFilters(row, { q, ...filters }));
  }, [
    defaultSortedRows,
    q,
    filters,
  ]);

  const activeFilterCount = [q, ...Object.values(filters)].filter(Boolean).length;

  const clearFilters = () => {
    setQ("");
    Object.values(filterSetters).forEach((setter) => setter(""));
  };

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Pendientes general</div>

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">
          {err}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filtrar por OS, cliente, equipo, estado, serie..."
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar pendientes"
        />
        <button className="btn" onClick={load} title="Recargar lista" type="button">
          Recargar
        </button>
        {activeFilterCount > 0 && (
          <button className="btn" onClick={clearFilters} type="button">
            Limpiar filtros ({activeFilterCount})
          </button>
        )}
      </div>

      <WorkQueueTable
        rows={filteredAndSorted}
        totalCount={rows.length}
        loading={loading}
        emptyText="No hay pendientes que coincidan con el filtro."
        onOpen={go}
        sort={sort}
        onSortChange={setSort}
        showFilters
        filters={filters}
        onFilterChange={(field, value) => filterSetters[field]?.(value)}
        filterOptions={filterOptions}
      />
    </div>
  );
}
