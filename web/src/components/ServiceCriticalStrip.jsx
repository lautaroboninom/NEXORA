import StatusChip from "./StatusChip.jsx";
import {
  deviceIdentifierPartsOf,
  formatDateOnly,
  formatOS,
  isMgInactiveBySale,
  modeloSerieVarianteOf,
  parseDateLocal,
  resolveFechaCreacion,
  resolveFechaIngreso,
  tipoEquipoOf,
} from "../lib/ui-helpers";

function ageDays(data) {
  const raw = resolveFechaIngreso(data) || resolveFechaCreacion(data);
  const date = parseDateLocal(raw);
  if (!date || Number.isNaN(date.getTime())) return null;
  const start = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.max(0, Math.floor((today - start) / 86400000));
}

function Field({ label, children, title, className = "", truncateValue = true }) {
  return (
    <div className={`min-w-0 ${className}`}>
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-sm font-medium text-gray-900 ${truncateValue ? "truncate" : "min-w-0"}`} title={title}>
        {children || "-"}
      </div>
    </div>
  );
}

function propiedadEquipoLabel(data) {
  const ownershipFlag =
    data?.es_cliente_mg_owner ??
    data?.es_propietario_mg ??
    data?.equipo?.es_cliente_mg_owner ??
    data?.equipo?.es_propietario_mg;
  if (ownershipFlag == null) return "-";
  if (ownershipFlag) return "MG BIO";
  const tieneMg = Boolean(String(data?.numero_interno || data?.equipo?.numero_interno || "").trim());
  return tieneMg && isMgInactiveBySale(data) ? "Cliente (Ex MG)" : "Cliente";
}

function identifierLine(data) {
  const parts = deviceIdentifierPartsOf(data, "-");
  const items = [parts.primary];
  if (parts.secondary) items.push(parts.secondary);
  if (parts.numeroAlternativo) items.push(`N° alternativo: ${parts.numeroAlternativo}`);
  return items.filter(Boolean).join(" · ");
}

export default function ServiceCriticalStrip({ data, isCotizacion = false }) {
  if (!data) return null;

  const days = ageDays(data);
  const alerts = [];
  if (!data?.asignado_a && !data?.asignado_a_nombre) alerts.push("Sin técnico");
  if (String(data?.estado || "").toLowerCase() === "liberado") alerts.push("Listo para entrega");
  if (String(data?.presupuesto_estado || "").toLowerCase() === "presupuestado") alerts.push("Pendiente de aprobación");
  const tipoEquipo = tipoEquipoOf(data, "-");
  const marca = (data?.marca || data?.equipo?.marca || "-").toString().trim() || "-";
  const modelo = modeloSerieVarianteOf(data, "-");
  const propiedad = propiedadEquipoLabel(data);
  const identificador = identifierLine(data);
  if (data?.garantia_reparacion || data?.garantia) alerts.push("Garantía");
  if (days != null && days >= 16) alerts.push("16+ días");

  return (
    <div className="sticky top-0 z-20 mb-4 rounded border bg-white/95 p-3 shadow-sm backdrop-blur">
      <div className="grid grid-cols-2 md:grid-cols-5 xl:grid-cols-12 gap-3">
        <Field label="OS" className="xl:col-span-1">{formatOS(data)}</Field>
        <Field label="Cliente" className="xl:col-span-1">{data?.razon_social || data?.cliente || "-"}</Field>
        <Field label="Tipo de equipo" title={tipoEquipo} className="xl:col-span-2">{tipoEquipo}</Field>
        <Field label="Marca" title={marca} className="xl:col-span-1">{marca}</Field>
        <Field label="Modelo" title={modelo} className="xl:col-span-1">{modelo}</Field>
        <Field label="N/S o MG" title={identificador} className="xl:col-span-2">{identificador}</Field>
        <Field label="Estado" className="xl:col-span-1"><StatusChip value={data?.estado} /></Field>
        <Field label={isCotizacion ? "Cotización" : "Presupuesto"}>
          <StatusChip value={data?.presupuesto_estado} />
        </Field>
        <Field label="Técnico">{data?.asignado_a_nombre || data?.tecnico || "-"}</Field>
        <Field label="Ubicación">{data?.ubicacion_nombre || "-"}</Field>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-gray-600">
        <span>Ingreso: {formatDateOnly(resolveFechaIngreso(data))}</span>
        <span>Antigüedad: {days == null ? "-" : `${days} días`}</span>
        <span className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700 ring-1 ring-inset ring-gray-200">
          Propiedad: {propiedad}
        </span>
        <span>Garantía reparación: {data?.garantia_reparacion ? "Sí" : "No"}</span>
        <span>Garantía fábrica: {data?.garantia ? "Sí" : "No"}</span>
        {alerts.map((alert) => (
          <span key={alert} className="rounded-full bg-amber-50 px-2 py-0.5 font-medium text-amber-800 ring-1 ring-inset ring-amber-200">
            {alert}
          </span>
        ))}
      </div>
    </div>
  );
}
