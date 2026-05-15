// web/src/lib/constants.js
// Catálogos/constantes de dominio (única fuente de verdad).

// =========================
// Resolución de reparación
// =========================
export const RESOLUCION = {
  REPARADO: "reparado",
  NO_REPARADO: "no_reparado",
  NO_SE_ENCONTRO_FALLA: "no_se_encontro_falla",
  PRESUPUESTO_RECHAZADO: "presupuesto_rechazado",
  CAMBIO: "cambio",
};

export const RESOLUCION_LABELS = {
  [RESOLUCION.REPARADO]: "Reparado",
  [RESOLUCION.NO_REPARADO]: "No reparado",
  [RESOLUCION.NO_SE_ENCONTRO_FALLA]: "No se encontró falla",
  [RESOLUCION.PRESUPUESTO_RECHAZADO]: "Presupuesto rechazado",
  [RESOLUCION.CAMBIO]: "Cambio",
};

// Útil para armar selects sin repetir arrays en cada pantalla
export const RESOLUCION_OPTIONS = Object.entries(RESOLUCION_LABELS).map(
  ([value, label]) => ({ value, label })
);

export const resolutionLabel = (value) =>
  RESOLUCION_LABELS[value] ?? String(value ?? "");

// Estados operativos (flujo de trabajo)
export const ESTADO = {
  DIAGNOSTICADO: "Diagnosticado",
  REPARAR: "Reparar",
  CONTROLADO_SIN_DEFECTO: "Controlado Sin Defecto",
  REPARADO: "Reparado",
  LIBERADO: "Liberado",
  ENTREGADO: "Entregado",
  BAJA: "Baja",
  ALQUILADO: "Alquilado",
  VENDIDO_PENDIENTE_ENTREGA: "vendido_pendiente_entrega",
  VENDIDO_ENTREGADO: "vendido_entregado",
};

export const ESTADO_LABELS = {
  [ESTADO.DIAGNOSTICADO]: "Diagnosticado",
  [ESTADO.REPARAR]: "Reparar",
  [ESTADO.CONTROLADO_SIN_DEFECTO]: "Controlado sin defecto",
  [ESTADO.REPARADO]: "Reparado",
  [ESTADO.LIBERADO]: "Liberado",
  [ESTADO.ENTREGADO]: "Entregado",
  [ESTADO.BAJA]: "Baja",
  [ESTADO.ALQUILADO]: "Alquilado",
  [ESTADO.VENDIDO_PENDIENTE_ENTREGA]: "Vendido pendiente de entrega",
  [ESTADO.VENDIDO_ENTREGADO]: "Vendido entregado",
};

export const ESTADO_OPTIONS = Object.entries(ESTADO_LABELS).map(
  ([value, label]) => ({ value, label })
);

// (opcional) por si querés habilitar siguiente paso en UI
export const NEXT_STATE = {
  [ESTADO.DIAGNOSTICADO]: ESTADO.REPARAR,
  [ESTADO.REPARAR]: ESTADO.REPARADO,
  [ESTADO.REPARADO]: ESTADO.LIBERADO,
  [ESTADO.LIBERADO]: ESTADO.ENTREGADO,
};

// Labels legibles para estados que vienen del backend (snake_case)
export const ESTADO_VALUE_LABELS = {
  ingresado: "Ingresado",
  asignado: "Asignado",
  diagnosticado: "Diagnosticado",
  presupuestado: "Presupuestado",
  reparar: "Reparar",
  derivado: "Derivado",
  controlado_sin_defecto: "Controlado sin defecto",
  reparado: "Reparado",
  liberado: "Liberado",
  entregado: "Entregado",
  alquilado: "Alquilado",
  vendido_pendiente_entrega: "Vendido pendiente de entrega",
  vendido_entregado: "Vendido entregado",
  baja: "Baja",
};

export const estadoLabel = (value) => {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  if (ESTADO_LABELS[raw]) return ESTADO_LABELS[raw];
  const lower = raw.toLowerCase();
  if (ESTADO_VALUE_LABELS[lower]) return ESTADO_VALUE_LABELS[lower];
  if (raw.includes("_")) {
    const spaced = raw.replace(/_/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
  }
  return raw.charAt(0).toUpperCase() + raw.slice(1);
};


// =========================
// Motivos de ingreso
// =========================
export const MOTIVO = {
  REPARACION: "reparación",
  BAJA_ALQUILER: "baja alquiler",
  REPARACION_ALQUILER: "reparación alquiler",
  SERVICE_PREVENTIVO: "service preventivo",
  URGENTE_CONTROL: "urgente control",
  DEVOLUCION_DEMO: "devolución demo",
  COTIZACION_EQUIPO: "cotización de equipo",
  OTROS: "otros",
};

export const MOTIVO_LABELS = {
  [MOTIVO.REPARACION]: "Reparación",
  [MOTIVO.BAJA_ALQUILER]: "Baja alquiler",
  [MOTIVO.REPARACION_ALQUILER]: "Reparación alquiler",
  [MOTIVO.SERVICE_PREVENTIVO]: "Service preventivo",
  [MOTIVO.URGENTE_CONTROL]: "Urgente control",
  [MOTIVO.DEVOLUCION_DEMO]: "Devolución demo",
  [MOTIVO.COTIZACION_EQUIPO]: "Cotización de equipo",
  [MOTIVO.OTROS]: "Otros",
};

export const MOTIVO_OPTIONS = Object.entries(MOTIVO_LABELS).map(
  ([value, label]) => ({ value, label })
);

export const motivoLabel = (value) =>
  MOTIVO_LABELS[value] ?? String(value ?? "");

// =========================
// Métricas - Fecha mínima de corte (unificada)
// =========================
// Solo considerar ingresos con fecha_ingreso >= METRICAS_DESDE_MIN (inclusive)
export const METRICAS_DESDE_MIN = '2025-06-26';
export const clampDesdeMin = (s) => {
  if (!s) return METRICAS_DESDE_MIN;
  return s < METRICAS_DESDE_MIN ? METRICAS_DESDE_MIN : s;
};

