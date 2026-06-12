import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { formatDateTime as formatDateTimeHelper } from "../../../lib/ui-helpers";
import { getDerivacionesPorIngreso, postDerivacionDevuelto } from "../../../lib/api";
import { estadoLabel } from "../../../lib/constants";

export default function DerivacionesTab({ id, canManage = false, setErr, refreshIngreso }) {
  const [derivs, setDerivs] = useState([]);
  const [fechaDevStr, setFechaDevStr] = useState(() => new Date().toISOString().slice(0,10));
  const [savingDev, setSavingDev] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const rows = await getDerivacionesPorIngreso(id);
        setDerivs(Array.isArray(rows) ? rows : []);
      } catch (e) {
        if (typeof setErr === "function") setErr(e?.message || "No se pudieron cargar las derivaciones");
        setDerivs([]);
      }
    })();
  }, [id, setErr]);

  const hayDerivAbierta = Array.isArray(derivs) && derivs.find(d => !d.fecha_entrega);
  return (
    <div className="border rounded p-4"> 
      <div className="flex items-center gap-3 mb-3">
        <h2 className="font-semibold">Derivaciones</h2>
        {canManage && (
          <div className="ml-auto flex items-center gap-2">
            <Link to={`/ingresos/${id}/derivar`} className="bg-neutral-800 text-white px-3 py-2 rounded">Derivar a externo</Link>
            {hayDerivAbierta && (
            <>
              <input
                type="date"
                className="border rounded p-2"
                value={fechaDevStr}
                onChange={(e) => setFechaDevStr(e.target.value)}
                aria-label="Fecha de devolución"
              />
              <button
                className="bg-green-700 text-white px-3 py-2 rounded disabled:opacity-60"
                disabled={savingDev}
                onClick={async () => {
                  try {
                    const abierta = derivs.find(d => !d.fecha_entrega);
                    if (!abierta) return;
                    setSavingDev(true);
                    await postDerivacionDevuelto(id, abierta.id, { fecha_entrega: fechaDevStr || null });
                    try { setDerivs(await getDerivacionesPorIngreso(id)); } catch (_) {}
                    if (typeof refreshIngreso === "function") await refreshIngreso();
                  } catch (e) {
                    if (typeof setErr === "function") setErr(e?.message || "No se pudo marcar como devuelto");
                  } finally {
                    setSavingDev(false);
                  }
                }}
                type="button"
              >
                {savingDev ? "Guardando..." : "Devuelto"}
              </button>
            </>
            )}
          </div>
        )}
      </div>

      {(!derivs || derivs.length === 0) ? (
        <div className="text-sm text-gray-500">No hay derivaciones.</div>
      ) : (
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left">
              <th className="p-2">Proveedor</th>
              <th className="p-2">Remito</th>
              <th className="p-2">Fecha derivación</th>
              <th className="p-2">Fecha entrega</th>
              <th className="p-2">Estado</th>
              <th className="p-2">Comentarios</th>
            </tr>
          </thead>
          <tbody>
            {derivs.map((d) => (
              <tr key={d.id} className="border-t">
                <td className="p-2">{d.proveedor || "-"}</td>
                <td className="p-2">{d.remit_deriv || "-"}</td>
                <td className="p-2 whitespace-nowrap">{d.fecha_deriv ? formatDateTimeHelper(d.fecha_deriv) : "-"}</td>
                <td className="p-2 whitespace-nowrap">{d.fecha_entrega ? formatDateTimeHelper(d.fecha_entrega) : "-"}</td>
                <td className="p-2">{estadoLabel(d.estado) || "-"}</td>
                <td className="p-2">{d.comentarios || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
