import { formatDateTime as formatDateTimeHelper } from "../../../lib/ui-helpers";

const roleLabel = (value) => {
  if (!value) return "-";
  const text = String(value).trim().replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

const fieldLabels = {
  asignado_a: "Asignado a",
  alquiler_a: "Alquilado a",
  alquiler_fecha: "Fecha de alquiler",
  alquiler_remito: "Remito de alquiler",
  faja_garantia: "Faja de garantía",
  ubicacion_id: "Ubicación",
};

const entityLabels = {
  devices: "Equipo",
  ingresos: "Ingreso",
  ingreso_accesorios: "Accesorios",
  ingreso_alquiler_accesorios: "Accesorios de alquiler",
  quote_items: "Ítems de presupuesto",
  quotes: "Presupuesto",
};

const fieldLabel = (value) => fieldLabels[String(value || "").trim()] || value || "-";
const entityLabel = (value) => entityLabels[String(value || "").trim()] || value || "-";
const historyValue = (value) => (value === null || value === undefined || value === "" ? "-" : value);

export default function HistorialTab({ hErr, hLoading, hist }) {
  return (
    <div className="border rounded p-4">
      <h2 className="font-semibold mb-2">Historial de cambios</h2>
      {hErr && <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">{hErr}</div>}
      {hLoading ? (
        <div className="text-sm text-gray-500">Cargando...</div>
      ) : (
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left">
              <th className="p-2">Fecha</th>
              <th className="p-2">Usuario</th>
              <th className="p-2">Rol</th>
              <th className="p-2">Entidad</th>
              <th className="p-2">Campo</th>
              <th className="p-2">Antes</th>
              <th className="p-2">Después</th>
            </tr>
          </thead>
          <tbody>
            {(hist || []).length === 0 ? (
              <tr>
                <td className="p-2 text-gray-500" colSpan={7}>
                  No hay cambios registrados.
                </td>
              </tr>
            ) : (
              hist.map((row, idx) => (
                <tr key={idx} className="border-t">
                  <td className="p-2 whitespace-nowrap">{formatDateTimeHelper(row.ts)}</td>
                  <td className="p-2">{row.user_nombre || row.user_id || "-"}</td>
                  <td className="p-2 whitespace-nowrap">{roleLabel(row.user_role)}</td>
                  <td className="p-2">{entityLabel(row.table_name)}</td>
                  <td className="p-2">{fieldLabel(row.column_name)}</td>
                  <td className="p-2">{historyValue(row.old_value)}</td>
                  <td className="p-2">{historyValue(row.new_value)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
