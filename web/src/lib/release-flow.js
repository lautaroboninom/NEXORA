// Extensiones explícitas: así este módulo también se puede importar desde los
// tests, que corren con `node --test` sin el resolutor de Vite.
import { RESOLUCION, estadoLabel, resolutionLabel } from "./constants.js";
import { ingresoIdOf } from "./ui-helpers.js";

const CLOSED_STATES = new Set([
  "entregado",
  "alquilado",
  "baja",
  "vendido_pendiente_entrega",
  "vendido_entregado",
]);

// Estos estados ocurren después de emitir la orden de salida. La orden debe
// seguir disponible para verla e imprimirla otra vez si se pierde el papel.
const REPRINTABLE_STATES = new Set([
  "liberado",
  "entregado",
  "alquilado",
  "baja",
  "vendido_entregado",
]);

const RELEASE_READY_STATES = new Set([
  "reparado",
  "controlado_sin_defecto",
  "no_se_repara",
]);

export function normalizeReleaseValue(value) {
  return String(value ?? "").trim().toLowerCase();
}

export function releaseResolutionSuggestion(row = {}) {
  const current = normalizeReleaseValue(row?.resolucion);
  if (current) return current;

  const estado = normalizeReleaseValue(row?.estado);
  const presupuesto = normalizeReleaseValue(row?.presupuesto_estado);

  if (estado === "controlado_sin_defecto") return RESOLUCION.NO_SE_ENCONTRO_FALLA;
  if (estado === "no_se_repara") return RESOLUCION.NO_REPARADO;
  if (presupuesto === "rechazado") return RESOLUCION.PRESUPUESTO_RECHAZADO;
  if (estado === "reparado") return RESOLUCION.REPARADO;
  return "";
}

export function getReleaseFlow(row = {}, { canManageResolution = false } = {}) {
  const id = ingresoIdOf(row);
  const estado = normalizeReleaseValue(row?.estado);
  const presupuesto = normalizeReleaseValue(row?.presupuesto_estado);
  const resolucion = normalizeReleaseValue(row?.resolucion);
  const isReprint = REPRINTABLE_STATES.has(estado);
  const isSoldDelivered = estado === "vendido_entregado";
  const needsResolution = !resolucion && !isReprint;
  const suggestedResolution = releaseResolutionSuggestion(row);

  let blockedReason = "";
  if (!id) {
    blockedReason = "No se pudo identificar la OS.";
  } else if (row?.derivacion_activa && !isReprint) {
    blockedReason = "El equipo sigue derivado. Marcá la derivación como devuelta antes de liberarlo.";
  } else if (estado === "vendido_pendiente_entrega") {
    blockedReason = "Es una venta pendiente de entrega. Completá la entrega de venta desde la hoja de servicio.";
  } else if (CLOSED_STATES.has(estado) && !isReprint) {
    blockedReason = `El equipo ya está en estado ${estadoLabel(estado)}.`;
  } else if (needsResolution && !RELEASE_READY_STATES.has(estado)) {
    blockedReason = "Todavía falta cerrar la reparación o definir una resolución de salida.";
  }

  const canSubmit = Boolean(
    id &&
      !blockedReason &&
      (isReprint || resolucion || RELEASE_READY_STATES.has(estado))
  );

  const primaryLabel = isSoldDelivered
    ? "Imprimir salida"
    : isReprint
      ? "Reimprimir orden"
      : "Revisar salida";
  const statusText = blockedReason
    || (isSoldDelivered
      ? "La venta ya fue entregada. Podés imprimir la orden de salida como comprobante del trabajo realizado antes de la venta, sin modificar la OS."
      : isReprint
      ? "La orden de salida ya fue emitida. Podés verla y reimprimirla sin cambiar la resolución."
      : needsResolution
        ? `Se va a completar la resolución como ${resolutionLabel(suggestedResolution) || "pendiente"}.`
        : `Listo para liberar con resolución ${resolutionLabel(resolucion)}.`);

  return {
    id,
    estado,
    presupuesto,
    resolucion,
    suggestedResolution,
    isReprint,
    isSoldDelivered,
    needsResolution,
    canSubmit,
    blockedReason,
    primaryLabel,
    statusText,
  };
}
