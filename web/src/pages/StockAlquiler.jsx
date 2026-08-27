//web\src\pages\StockAlquiler.jsx

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getDeliveryOrderRentalAvailableEquipment } from "../lib/api";
import { ingresoIdOf, formatOS, norm, tipoEquipoOf, catalogEquipmentLabel } from "../lib/ui-helpers";
import { useAuth } from "../context/AuthContext";
import useQueryState from "../hooks/useQueryState";
import useColumnFilters from "../hooks/useColumnFilters";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import IngresoTable from "../components/IngresoTable.jsx";
import {
  columnFilterOptions,
  matchesColumnFilters,
} from "../lib/ingreso-list-filters";

// Catálogo (DB):
const ESTADOS_EXCLUIR = new Set(["entregado", "alquilado", "baja", "vendido_pendiente_entrega", "vendido_entregado"]);
const estadoValido = (r) => {
  const estado = (r?.estado || '').toString().trim().toLowerCase();
  return !ESTADOS_EXCLUIR.has(estado);
};

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

const marcaOf = (row) => (row?.marca ?? row?.equipo?.marca ?? "-");
const modeloOf = (row) => {
  const candidates = [row?.modelo, row?.equipo?.modelo, row?.modelo_nombre, row?.equipo?.modelo_nombre, row?.modelo_serie, row?.serie_nombre];
  for (const raw of candidates) {
    if (typeof raw === "string") {
      const v = raw.trim();
      if (v) return v;
    }
  }
  return "-";
};
const varianteOf = (row) => {
  const candidates = [row?.equipo_variante, row?.modelo_variante, row?.variante, row?.variante_nombre];
  for (const raw of candidates) {
    if (typeof raw === "string") {
      const v = raw.trim();
      if (v) return v;
    }
  }
  return "-";
};

const COLUMNS = [
  { field: "os", label: "OS", cellClassName: "underline", render: (row) => formatOS(row), filter: "text" },
  {
    field: "tipo",
    label: "Tipo de equipo",
    render: (row) => tipoEquipoOf(row) || "-",
    filter: "select",
  },
  { field: "marca", label: "Marca", render: marcaOf, filter: "select" },
  { field: "modelo", label: "Modelo", render: modeloOf, filter: "select" },
  { field: "variante", label: "Variante", render: varianteOf, filter: "select" },
  {
    field: "identificacion",
    label: "Serie",
    render: (row) => <DeviceIdentifier row={row} />,
    filter: "text",
  },
];

export default function StockAlquiler() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [warning, setWarning] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const nav = useNavigate();

  const { user, loading: authLoading } = useAuth();

  const load = useCallback(async () => {
    setErr("");
    setWarning("");
    setLoading(true);
    try {
      const data = await getDeliveryOrderRentalAvailableEquipment({ limit: 200 });
      const safe = Array.isArray(data?.items) ? data.items : [];
      setRows(safe.map(rowFromRentalOption).filter(estadoValido));
      if (data?.warning) setWarning(data.warning);
    } catch (e) {
      setErr(e?.message || "Error cargando stock");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      setLoading(false);
      return;
    }
    load();
  }, [authLoading, user, load]);

  const filtered = useMemo(() => {
    const needle = norm(q);
    return rows.filter((row) => {
      if (!estadoValido(row)) return false;
      if (!matchesColumnFilters(row, COLUMNS, filters)) return false;
      if (!needle) return true;
      const campos = [
        formatOS(row),
        row?.marca,
        catalogEquipmentLabel(row),
        tipoEquipoOf(row),
        row?.numero_serie,
        row?.numero_interno,
        row?.razon_social,
      ];
      return campos.some((c) => norm(c).includes(needle));
    });
  }, [rows, q, filters]);

  const filterOptions = useMemo(() => columnFilterOptions(rows, COLUMNS), [rows]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    nav(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Stock de alquiler</div>

      <AlertBanner tone="error">{err}</AlertBanner>
      <AlertBanner tone="warning">{warning}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, marca, equipo, serie"
        searchLabel="Filtrar stock de alquiler"
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
        emptyText="No hay equipos disponibles en Estantería de Alquiler con stock STL."
        totalCount={rows.length}
        countLabel="equipos"
      />
    </div>
  );
}
