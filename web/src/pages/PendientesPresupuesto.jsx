// web/src/pages/PendientesPresupuesto.jsx
import { useEffect, useMemo, useState } from "react";
import { getPendientesPresupuesto } from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, resolveFechaIngreso, catalogEquipmentLabel } from "../lib/ui-helpers";
import StatusChip from "../components/StatusChip.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import useQueryState from "../hooks/useQueryState";


// Ajust si tu backend usa otra ruta
const ENDPOINT = "/api/presupuestos/pendientes/";

export default function PendientesPresupuesto() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const navigate = useNavigate();

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
    if (!needle) return rows;
    return rows.filter((row) => {
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
      <div className="h1 mb-3">Pendientes de Presupuesto</div>

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
          placeholder="Filtrar por OS, cliente, equipo, NS, estado, presupuesto"
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar pendientes de presupuesto"
        />
        <button
          className="btn"
          onClick={load}
          title="Recargar lista"
          disabled={loading}
          aria-busy={loading ? "true" : "false"}
        >
          Recargar
        </button>
      </div>

      {loading ? (
        "Cargando..."
      ) : filtered.length === 0 ? (
        <div className="text-sm text-gray-500">No hay pendientes que coincidan con el filtro.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th scope="col" className="p-2">OS</th>
                <th scope="col" className="p-2">Cliente</th>
                <th scope="col" className="p-2">Equipo</th>
                <th scope="col" className="p-2">Estado</th>
                <th scope="col" className="p-2">N/S</th>
                <th scope="col" className="p-2">Fecha ingreso</th>
                <th scope="col" className="p-2">Fecha servicio</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => {
                const moneda = row?.presupuesto_moneda ?? "ARS";
                const monto = row?.presupuesto_monto ?? row?.presupuesto_total ?? null;
                const presuLabel =
                  row?.presupuesto_estado ??
                  (row?.presupuestado ? "Presupuestado" : "Pendiente");

                return (
                  <tr
                    key={ingresoIdOf(row)}
                    onClick={() => go(row)}
                    onKeyDown={(e) => onRowKeyDown(e, row)}
                    className="hover:bg-gray-50 cursor-pointer"
                    role="link"
                    tabIndex={0}
                    aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                    data-testid={`row-${ingresoIdOf(row)}`}
                  >
                    <td className="p-2 underline">{formatOS(row)}</td>
                    <td className="p-2">{row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}</td>
                    <td className="p-2">{catalogEquipmentLabel(row) ?? "-"}</td>
                    <td className="p-2"><StatusChip value={row?.estado} title="Estado del equipo" /></td>
                    <td className="p-2"><DeviceIdentifier row={row} /></td>
                    <td className="p-2 whitespace-nowrap">{formatDateOnly(resolveFechaIngreso(row))}</td>
                    <td className="p-2 whitespace-nowrap">{formatDateOnly(row?.fecha_servicio)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="text-xs text-gray-500 mt-2">
            Mostrando {filtered.length} de {rows.length}.
          </div>
        </div>
      )}
    </div>
  );
}

