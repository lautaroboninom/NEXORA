// web/src/pages/AdminListos.jsx
import { useEffect, useMemo, useState } from "react";
import api, { getBlob } from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, catalogEquipmentLabel } from "../lib/ui-helpers";
import StatusChip from "../components/StatusChip.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import AlertBanner from "../components/AlertBanner.jsx";
import ListToolbar from "../components/ListToolbar.jsx";
import IngresoTable from "../components/IngresoTable.jsx";
import { resolutionLabel } from "../lib/constants";
import useQueryState from "../hooks/useQueryState";
import useColumnFilters from "../hooks/useColumnFilters";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import {
  columnFilterOptions,
  matchesColumnFilters,
} from "../lib/ingreso-list-filters";


// Ajustar si el backend usa otra ruta.
const ENDPOINT = "/api/listos-para-retiro/";

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
    field: "resolucion",
    label: "Resolución",
    render: (row) => <StatusChip value={resolutionLabel(row?.resolucion)} title="Resolución" />,
    filter: "select",
  },
  {
    field: "identificacion",
    label: "Serie",
    render: (row) => <DeviceIdentifier row={row} />,
    filter: "text",
  },
  {
    field: "fecha_listo",
    label: "Fecha listo",
    cellClassName: "whitespace-nowrap",
    render: (row) => formatDateOnly(row?.fecha_listo ?? "-"),
  },
];

export default function AdminListos() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const [busyId, setBusyId] = useState(null);
  const [remitoBusyId, setRemitoBusyId] = useState(null);
  const { filters, setFilter, clearFilters, activeCount } = useColumnFilters();
  const [filtersOpen, setFiltersOpen] = useState(() => activeCount > 0);
  const navigate = useNavigate();
  const { user } = useAuth();
  const release = can(user, PERMISSION_CODES.ACTION_INGRESO_PRINT_EXIT_ORDER);
  const canDeliver = can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY);

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get(ENDPOINT);
      const list = Array.isArray(data) ? data : [];
      const clientNameOf = (row) => (row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "").trim();
      const readyTime = (row) =>
        new Date(
          row?.fecha_entrega ?? row?.fecha_listo ?? row?.fecha_reparado ?? row?.fecha_estado ?? row?.estado_fecha ?? 0,
        ).getTime();
      list.sort((a, b) => {
        const nameComparison = clientNameOf(a).localeCompare(clientNameOf(b), undefined, { sensitivity: "base" });
        if (nameComparison !== 0) return nameComparison;
        return readyTime(b) - readyTime(a);
      });
      setRows(list);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los equipos listos para retiro");
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
        row?.resolucion,
        resolutionLabel(row?.resolucion ?? ""),
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

  async function entregar(row) {
    const id = ingresoIdOf(row);
    if (!id) return;
    if (String(row?.estado || "").toLowerCase() === "vendido_pendiente_entrega") {
      navigate(`/ingresos/${id}`);
      return;
    }
    try {
      setBusyId(id);
      await api.post(`/api/ingresos/${id}/entregar/`);
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo marcar como entregado");
    } finally {
      setBusyId(null);
    }
  }

  async function verOrdenSalida(row) {
    const id = ingresoIdOf(row);
    if (!id) return;
    try {
      setRemitoBusyId(id);
      const blob = await getBlob(`/api/ingresos/${id}/remito/`);
      if (!(blob instanceof Blob)) {
        throw new Error("La respuesta no fue un PDF");
      }
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo obtener la orden de salida");
    } finally {
      setRemitoBusyId(null);
    }
  }

  const rowActions = (row, { mobile } = {}) => {
    const id = ingresoIdOf(row);
    const isSalePending = String(row?.estado || "").toLowerCase() === "vendido_pendiente_entrega";
    return (
      <div className={mobile ? "grid grid-cols-1 gap-2 min-[420px]:grid-cols-2" : "flex gap-2 justify-end"}>
        {release && (
          <button
            className={mobile ? "btn w-full justify-center" : "btn"}
            type="button"
            onClick={() => verOrdenSalida(row)}
            disabled={remitoBusyId === id}
            aria-busy={remitoBusyId === id ? "true" : "false"}
            title="Ver o reimprimir orden de salida"
          >
            Ver OS
          </button>
        )}
        {canDeliver && (
          <button
            className={mobile ? "btn-secondary w-full justify-center" : "btn-secondary"}
            type="button"
            onClick={() => entregar(row)}
            disabled={busyId === id}
            aria-busy={busyId === id ? "true" : "false"}
            title={isSalePending ? "Completar entrega de venta" : "Marcar como entregado"}
          >
            {isSalePending ? "Completar entrega" : "Entregado"}
          </button>
        )}
      </div>
    );
  };

  return (
    <div className="card">
      {/* El menú lateral lo llama "Liberados"; el subtítulo mantiene el nombre
          con el que se lo conoce en el mostrador. */}
      <div className="h1">Liberados</div>
      <div className="mb-3 text-sm text-gray-600">Equipos listos para retiro</div>

      <AlertBanner tone="error">{err}</AlertBanner>

      <ListToolbar
        q={q}
        onQChange={setQ}
        searchPlaceholder="Filtrar por OS, cliente, equipo, serie, resolución"
        searchLabel="Filtrar liberados"
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
        rowActions={release || canDeliver ? rowActions : undefined}
        filters={filters}
        onFilterChange={setFilter}
        filterOptions={filterOptions}
        showFilters={filtersOpen}
        loading={loading}
        emptyText="No hay equipos listos para retiro."
        totalCount={rows.length}
      />
    </div>
  );
}
