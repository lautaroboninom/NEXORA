import { useEffect, useMemo, useState } from "react";
import api, { postDerivacionDevuelto } from "../lib/api";
import { ingresoIdOf, formatOS, formatDateOnly } from "../lib/ui-helpers";
import { catalogEquipmentLabel } from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";

export default function Derivados() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [fechaMap, setFechaMap] = useState({}); // {ingreso_id: 'YYYY-MM-DD'}

  const load = async () => {
    try {
      setErr(""); setLoading(true);
      const data = await api.get("/api/ingresos/derivados/");
      setRows(Array.isArray(data) ? data : []);
      // preset fechas por fila (hoy)
      const today = new Date().toISOString().slice(0,10);
      const m = {};
      (Array.isArray(data) ? data : []).forEach(r => { m[ingresoIdOf(r)] = today; });
      setFechaMap(m);
    } catch (e) {
      setErr(e?.message || "No se pudo cargar la lista de derivados");
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const sorted = useMemo(() => {
    // ms recientes primero por fecha_deriv (tratar YYYY-MM-DD como local)
    const toDate = (v) => (v && typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) ? new Date(`${v}T00:00:00`) : (v ? new Date(v) : new Date(0));
    return [...rows].sort((a,b) => toDate(b?.fecha_deriv) - toDate(a?.fecha_deriv));
  }, [rows]);

  const onDevuelto = async (row) => {
    const ingresoId = ingresoIdOf(row);
    const f = fechaMap[ingresoId] || null;
    try {
      await postDerivacionDevuelto(ingresoId, (row?.deriv_id ?? row?.id), { fecha_entrega: f });
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo marcar como devuelto");
    }
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Derivados</div>

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">{err}</div>
      )}

      {loading ? (
        "Cargando..."
      ) : sorted.length === 0 ? (
        <div className="text-sm text-gray-500">No hay equipos derivados.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="p-2">OS</th>
                <th className="p-2">Cliente</th>
                <th className="p-2">Proveedor</th>
                <th className="p-2">Equipo</th>
                <th className="p-2">Serie</th>
                <th className="p-2">Fecha derivacin</th>
                <th className="p-2 text-right">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((row) => (
                <tr key={ingresoIdOf(row)} className="border-t">
                  <td className="p-2 underline">{formatOS(row)}</td>
                  <td className="p-2">{row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? '-'}</td>
                  <td className="p-2">{row?.proveedor ?? '-'}</td>
                  <td className="p-2">{catalogEquipmentLabel(row)}</td>
                  <td className="p-2"><DeviceIdentifier row={row} /></td>
                  <td className="p-2 whitespace-nowrap">{row?.fecha_deriv ? formatDateOnly(row.fecha_deriv) : '-'}</td>
                  <td className="p-2 text-right">
                    <div className="flex items-center gap-2 justify-end">
                      <input
                        type="date"
                        value={fechaMap[ingresoIdOf(row)] || ''}
                        onChange={(e) => setFechaMap((m) => ({ ...m, [ingresoIdOf(row)]: e.target.value }))}
                        className="border rounded p-1"
                        aria-label="Fecha devolucin"
                      />
                      <button className="btn" onClick={() => onDevuelto(row)}>
                        Devuelto
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
