import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { buscarAccesorioPorRef } from "../lib/api";
import { formatDateOnly as formatDateOnlyHelper, formatOS as formatOSHelper, resolveFechaIngreso } from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";

export default function BuscarAccesorio() {
  const [sp] = useSearchParams();
  const ref = (sp.get("ref") || "").trim();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const nav = useNavigate();

  useEffect(() => {
    (async () => {
      setLoading(true); setErr("");
      try {
        const data = await buscarAccesorioPorRef(ref);
        setRows(Array.isArray(data) ? data : []);
      } catch (e) {
        setErr(e?.message || "Error cargando resultados");
      } finally { setLoading(false); }
    })();
  }, [ref]);

  const titulo = ref ? `Servicios con referencia: ${ref}` : "Bsqueda por referencia de accesorio";

  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      <h1 className="text-2xl font-bold">{titulo}</h1>
      {err && <div className="bg-red-100 text-red-700 border border-red-300 p-2 rounded">{err}</div>}
      {loading ? "Cargando..." :
        rows.length === 0 ? <div className="text-sm text-gray-500">No se encontraron servicios con esa referencia.</div> :
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="p-2">OS</th>
                <th className="p-2">Accesorio</th>
                <th className="p-2">Referencia</th>
                <th className="p-2">Cliente</th>
                <th className="p-2">Equipo</th>
                <th className="p-2">Serie</th>
                <th className="p-2">Fecha ingreso</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const ingresoId = r?.id ?? r?.ingreso_id;
                const equipo = [r?.marca, r?.modelo].filter(Boolean).join(" ");
                return (
                  <tr
                    key={`${ingresoId}-${r?.accesorio_nombre}-${r?.referencia}`}
                    className="border-t hover:bg-gray-50 cursor-pointer"
                    onClick={() => nav(`/ingresos/${ingresoId}`)}
                    title="Ir a la hoja de servicio"
                  >
                    <td className="p-2 underline">{formatOSHelper(r, ingresoId)}</td>
                    <td className="p-2">{r?.accesorio_nombre || "-"}</td>
                    <td className="p-2">{r?.referencia || "-"}</td>
                    <td className="p-2">{r?.razon_social || "-"}</td>
                    <td className="p-2">{equipo || "-"}</td>
                    <td className="p-2"><DeviceIdentifier row={r} /></td>
                    <td className="p-2 whitespace-nowrap">{formatDateOnlyHelper(resolveFechaIngreso(r))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="text-xs text-gray-500 mt-2">Mostrando {rows.length} resultado(s).</div>
        </div>}
    </div>
  );
}


