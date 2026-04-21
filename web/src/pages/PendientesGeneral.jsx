import { useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, resolveFechaIngreso, resolveFechaCreacion, catalogEquipmentLabel, nsPreferInternoOf, isMotivoCotizacionEquipo } from "../lib/ui-helpers";
import StatusChip from "../components/StatusChip.jsx";
import useQueryState from "../hooks/useQueryState";

// Ajust si tu backend usa otro endpoint
const ENDPOINT = "/api/ingresos/pendientes/";

export default function PendientesGeneral() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
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

  // helper: detectar motivo urgente control
  const isUrgente = (row) => (row?.motivo || "").toLowerCase() === "urgente control";

  // Aplico filtro y luego ordeno: devueltos primero, urgentes despus, luego por fecha_creacion asc
  const filteredAndSorted = useMemo(() => {
    const needle = norm(q);
    const base = needle
      ? rows.filter((row) => {
          const campos = [
            formatOS(row),
            row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
            row?.marca ?? row?.equipo?.marca,
            catalogEquipmentLabel(row),
            tipoEquipoOf(row),
            row?.estado,
            row?.presupuesto_estado,
            row?.numero_serie,
            row?.numero_interno,
          ];
          return campos.some((c) => norm(c).includes(needle));
        })
      : rows;

    return [...base].sort((a, b) => {
      // 1) Devueltos de derivacin primero
      const ad = a?.derivado_devuelto ? 1 : 0;
      const bd = b?.derivado_devuelto ? 1 : 0;
      if (ad !== bd) return bd - ad;

      // 2) Urgentes despus
      const au = isUrgente(a) ? 1 : 0;
      const bu = isUrgente(b) ? 1 : 0;
      if (au !== bu) return bu - au;

      // 3) Antiguedad: mas viejos primero
      const rawA = resolveFechaCreacion(a);
      const rawB = resolveFechaCreacion(b);
      const dtA = rawA ? new Date(rawA).getTime() : Number.POSITIVE_INFINITY;
      const dtB = rawB ? new Date(rawB).getTime() : Number.POSITIVE_INFINITY;
      return dtA - dtB;
    });
  }, [rows, q]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  const onRowKeyDown = (e, row) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      go(row);
    }
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Pendientes general</div>

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">
          {err}
        </div>
      )}

      <div className="flex items-center gap-2 mb-3">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filtrar por OS, cliente, equipo, estado, serie..."
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar pendientes"
        />
        <button className="btn" onClick={load} title="Recargar lista">
          Recargar
        </button>
      </div>

      {loading ? (
        "Cargando..."
      ) : filteredAndSorted.length === 0 ? (
        <div className="text-sm text-gray-500">No hay pendientes que coincidan con el filtro.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="p-2">OS</th>
                <th className="p-2">Cliente</th>
                <th className="p-2">Equipo</th>
                <th className="p-2">Estado</th>
                <th className="p-2">Presupuesto estado</th>
                <th className="p-2">N/S o MG</th>
                <th className="p-2">Fecha ingreso</th>
              </tr>
            </thead>
            <tbody>
              {filteredAndSorted.map((row) => {
                const urgente = isUrgente(row);
                const devuelto = !!row?.derivado_devuelto;
                const esCotizacion = isMotivoCotizacionEquipo(row?.motivo);
                const estadoTxt = String(row?.estado || "").toLowerCase();
                const presupuestoTxt = String(row?.presupuesto_estado || "").toLowerCase();
                const showPresupuestoEstado = !(presupuestoTxt === "pendiente" && estadoTxt !== "diagnosticado");
                const presupuestoValue = showPresupuestoEstado ? row?.presupuesto_estado : "";
                const rowCls = [
                  "hover:bg-gray-50 cursor-pointer",
                  urgente && "text-red-600 font-semibold",
                  devuelto && "text-blue-700 font-semibold",
                ]
                  .filter(Boolean)
                  .join(" ");

                return (
                  <tr
                    key={ingresoIdOf(row)}
                    onClick={() => go(row)}
                    onKeyDown={(e) => onRowKeyDown(e, row)}
                    className={rowCls}
                    role="link"
                    tabIndex={0}
                    aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                    data-testid={`row-${ingresoIdOf(row)}`}
                  >
                    <td className="p-2 underline">
                      <span>{formatOS(row)}</span>
                      {devuelto && (
                        <span className="ml-2 inline-block px-2 py-0.5 text-[10px] rounded bg-blue-100 text-blue-700 align-middle">
                          DERIVADO DEVUELTO
                        </span>
                      )}
                      {urgente && (
                        <span className="ml-2 inline-block px-2 py-0.5 text-[10px] rounded bg-red-100 text-red-700 align-middle">
                          URGENTE
                        </span>
                      )}
                      {esCotizacion && (
                        <span className="ml-2 inline-block px-2 py-0.5 text-[10px] rounded bg-amber-100 text-amber-800 align-middle">
                          COTIZACIÓN EQUIPO
                        </span>
                      )}
                    </td>
                    <td className="p-2">
                      {row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}
                    </td>
                    <td className="p-2">{catalogEquipmentLabel(row)}</td>
                    <td className="p-2">
                      <StatusChip value={row?.estado} />
                    </td>
                    <td className="p-2">
                      <StatusChip value={presupuestoValue} />
                    </td>
                    <td className="p-2">{nsPreferInternoOf(row)}</td>
                    <td className="p-2 whitespace-nowrap">{formatDateOnly(resolveFechaIngreso(row))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="text-xs text-gray-500 mt-2">
            Mostrando {filteredAndSorted.length} de {rows.length}.
          </div>
        </div>
      )}
    </div>
  );
}

