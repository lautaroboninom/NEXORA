// web/src/pages/Depositos.jsx
//
// "Dónde está parado el equipo", en una sola pantalla. Antes eran dos páginas
// distintas (Stock de alquiler y Depósitos/Bajas) que respondían la misma
// pregunta con dos plumbing distintos; acá el origen de los datos es una opción
// más del selector de ubicación.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  getUbicaciones,
  getGeneralEquipos,
  getDeliveryOrderRentalAvailableEquipment,
} from "../lib/api";
import {
  ingresoIdOf,
  formatOS,
  formatDateOnly,
  norm,
  locationDisplayName,
  canonicalLocationName,
  locationNameKey,
  tipoEquipoOf,
  catalogEquipmentLabel,
} from "../lib/ui-helpers";
import { estadoLabel } from "../lib/constants";
import CustomerCell from "../components/CustomerCell.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DerivadoMark } from "../components/DerivadoBadge.jsx";
import StatusChip from "../components/StatusChip.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import IngresoTable from "../components/IngresoTable.jsx";
import useQueryState from "../hooks/useQueryState";
import useColumnFilters from "../hooks/useColumnFilters";
import {
  columnFilterOptions,
  matchesColumnFilters,
} from "../lib/ingreso-list-filters";

// Opciones fijas del selector, antes de la lista de ubicaciones.
const FUENTE_ALQUILER = "alquiler";
const FUENTE_BAJAS = "bajas";

// El endpoint de alquiler ya devuelve sólo equipos disponibles, pero un ingreso
// cerrado que quedó apuntando a la estantería no es stock: se filtra igual.
const ESTADOS_EXCLUIR = new Set([
  "entregado",
  "alquilado",
  "baja",
  "vendido_pendiente_entrega",
  "vendido_entregado",
]);
const estadoValido = (r) => !ESTADOS_EXCLUIR.has((r?.estado || "").toString().trim().toLowerCase());

// El listado de alquiler viene con las claves del selector de órdenes de entrega:
// se traducen a las que usan el resto de los listados de ingresos.
const rowFromRentalOption = (item) => ({
  ...item,
  id: item?.ingresoId,
  ingreso_id: item?.ingresoId,
  device_id: item?.deviceId,
  ubicacion_id: item?.ubicacionId,
  ubicacion_nombre: item?.ubicacionNombre,
  numero_serie: item?.equipmentSerial,
  numero_interno: item?.equipmentInternalNumber,
  marca: item?.marca,
  modelo: item?.modelo,
  equipo_variante: item?.equipoVariante,
  tipo_equipo: item?.tipoEquipo,
  razon_social: item?.ownerCustomerName,
});

const marcaOf = (row) => row?.marca ?? row?.equipo?.marca ?? "-";
const modeloOf = (row) => {
  const candidates = [row?.modelo, row?.equipo?.modelo, row?.modelo_nombre, row?.equipo?.modelo_nombre];
  for (const raw of candidates) {
    if (typeof raw === "string" && raw.trim()) return raw.trim();
  }
  return "-";
};
const varianteOf = (row) => {
  const candidates = [row?.equipo_variante, row?.modelo_variante, row?.variante, row?.variante_nombre];
  for (const raw of candidates) {
    if (typeof raw === "string" && raw.trim()) return raw.trim();
  }
  return "-";
};

const COLUMNS = [
  {
    field: "os",
    label: "OS",
    cellClassName: "underline",
    // Un equipo de alquiler dado de alta sin ingreso no tiene OS: `formatOS`
    // devolvería "00000", que se lee como una OS real y encima subrayada.
    render: (row) =>
      ingresoIdOf(row) ? (
        <>
          {formatOS(row)}
          <DerivadoMark row={row} className="ml-1.5" />
        </>
      ) : (
        "-"
      ),
    filter: "text",
  },
  {
    field: "cliente",
    label: "Cliente",
    render: (row) => <CustomerCell row={row} />,
    filter: "text",
  },
  { field: "marca", label: "Marca", render: marcaOf, filter: "select" },
  { field: "modelo", label: "Modelo", render: modeloOf, filter: "select" },
  { field: "variante", label: "Variante", render: varianteOf, filter: "select" },
  {
    field: "tipo",
    label: "Tipo de equipo",
    render: (row) => tipoEquipoOf(row) || "-",
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
    // Los equipos de alquiler dados de alta sin OS no tienen estado de ingreso.
    render: (row) => (row?.estado ? <StatusChip value={row.estado} /> : "-"),
    filter: "select",
    filterLabel: (value) => estadoLabel(value),
  },
  {
    field: "fecha_ingreso",
    label: "Fecha ingreso",
    cellClassName: "whitespace-nowrap",
    render: (row) => formatDateOnly(row?.fecha_ingreso),
  },
];

export default function Depositos() {
  const [ubicaciones, setUbicaciones] = useState([]);
  const [loadingUbicaciones, setLoadingUbicaciones] = useState(true);
  const [ubicacionId, setUbicacionId] = useQueryState("ubicacion_id", "");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [warning, setWarning] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const nav = useNavigate();

  useEffect(() => {
    setLoadingUbicaciones(true);
    getUbicaciones()
      .then((data) => {
        setUbicaciones(Array.isArray(data) ? data : []);
      })
      .catch((e) => setErr(e.message || "Error ubicaciones"))
      .finally(() => setLoadingUbicaciones(false));
  }, []);

  const ubicacionesFiltradas = useMemo(() => {
    const banned = new Set(["-", "alquilado", "desguace"]);
    const seen = new Set();
    return (ubicaciones || []).filter((u) => {
      const name = String(u?.nombre || "").trim();
      if (!name || banned.has(name.toLowerCase())) return false;
      const canonical = canonicalLocationName(name);
      const key = locationNameKey(canonical);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [ubicaciones]);

  const ubicacionNombre = useMemo(() => {
    const u = ubicacionesFiltradas.find((x) => String(x.id) === String(ubicacionId));
    return u?.nombre || "";
  }, [ubicacionesFiltradas, ubicacionId]);

  const load = useCallback(async () => {
    if (!ubicacionId) {
      setRows([]);
      setWarning("");
      return;
    }
    // Si es una ubicación de base de datos y todavía se están cargando los catálogos,
    // esperar a tener la lista antes de consultar para no traer todos los equipos sin filtro.
    if (ubicacionId !== FUENTE_ALQUILER && ubicacionId !== FUENTE_BAJAS && loadingUbicaciones) {
      return;
    }
    setLoading(true);
    setErr("");
    setWarning("");
    try {
      if (ubicacionId === FUENTE_ALQUILER) {
        const data = await getDeliveryOrderRentalAvailableEquipment({ limit: 200 });
        const safe = Array.isArray(data?.items) ? data.items : [];
        setRows(safe.map(rowFromRentalOption).filter(estadoValido));
        if (data?.warning) setWarning(data.warning);
        return;
      }
      const params = {};
      if (ubicacionId === FUENTE_BAJAS) {
        params.estado = "baja";
      } else if (ubicacionNombre) {
        params.ubicacion = canonicalLocationName(ubicacionNombre);
        params.ubicacion_id = ubicacionId;
      } else {
        setRows([]);
        return;
      }
      const data = await getGeneralEquipos(params);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e.message || "Error cargando equipos");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [ubicacionId, ubicacionNombre, loadingUbicaciones]);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    const needle = norm(q);
    return rows.filter((row) => {
      if (!matchesColumnFilters(row, COLUMNS, filters)) return false;
      if (!needle) return true;
      const campos = [
        formatOS(row),
        row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
        row?.propietario_nombre,
        marcaOf(row),
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

  // El stock de alquiler incluye equipos dados de alta sin OS: sin el fallback a
  // `device_id` todas esas filas comparten la misma key vacía.
  const rowKey = (row) => ingresoIdOf(row) ?? (row?.device_id ? `device-${row.device_id}` : null);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    nav(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Stock / Depósitos</div>

      <div className="mb-3 flex items-center gap-2">
        <span className="text-sm">Depósito / Ubicación:</span>
        <select
          className="rounded border p-2"
          value={ubicacionId}
          onChange={(e) => setUbicacionId(e.target.value)}
          aria-label="Elegir depósito o ubicación"
        >
          <option value="">Seleccione una ubicación</option>
          <option value={FUENTE_ALQUILER}>Stock de alquiler</option>
          <option value={FUENTE_BAJAS}>Bajas</option>
          {ubicacionesFiltradas.map((u) => (
            <option key={u.id} value={u.id}>
              {u.label || locationDisplayName(u.nombre)}
            </option>
          ))}
        </select>
      </div>

      <AlertBanner tone="error">{err}</AlertBanner>
      <AlertBanner tone="warning">{warning}</AlertBanner>

      {!ubicacionId ? (
        <div className="text-sm text-gray-500">Elegí una ubicación para ver los equipos.</div>
      ) : (
        <>
          <ListToolbar
            q={q}
            onQChange={setQ}
            searchPlaceholder="Filtrar por OS, cliente, marca, equipo, serie"
            searchLabel="Filtrar equipos"
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
            rowKey={rowKey}
            onOpen={go}
            openLabel={(row) => `Abrir hoja de servicio de ${formatOS(row)}`}
            filters={filters}
            onFilterChange={setFilter}
            filterOptions={filterOptions}
            showFilters={filtersOpen}
            loading={loading}
            emptyText={
              ubicacionId === FUENTE_ALQUILER
                ? "No hay equipos disponibles en Estantería de Alquiler con stock STL."
                : ubicacionNombre
                  ? `No hay equipos en ${locationDisplayName(ubicacionNombre)}.`
                  : "Sin resultados."
            }
            totalCount={rows.length}
            countLabel="equipos"
          />
        </>
      )}
    </div>
  );
}
