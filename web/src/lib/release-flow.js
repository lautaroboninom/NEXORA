import { RESOLUCION, estadoLabel, resolutionLabel } from "./constants";
import { ingresoIdOf } from "./ui-helpers";

const CLOSED_STATES = new Set([
  "entregado",
  "alquilado",
  "baja",
  "vendido_pendiente_entrega",
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
  const isReleased = estado === "liberado";
  const needsResolution = !resolucion && !isReleased;
  const suggestedResolution = releaseResolutionSuggestion(row);

  let blockedReason = "";
  if (!id) {
    blockedReason = "No se pudo identificar la OS.";
  } else if (estado === "vendido_pendiente_entrega") {
    blockedReason = "Es una venta pendiente de entrega. Completá la entrega de venta desde la hoja de servicio.";
  } else if (CLOSED_STATES.has(estado)) {
    blockedReason = `El equipo ya está en estado ${estadoLabel(estado)}.`;
  } else if (needsResolution && !RELEASE_READY_STATES.has(estado)) {
    blockedReason = "Todavía falta cerrar la reparación o definir una resolución de salida.";
  }

  const canSubmit = Boolean(
    id &&
      !blockedReason &&
      (isReleased || resolucion || RELEASE_READY_STATES.has(estado))
  );

  const primaryLabel = isReleased ? "Reimprimir orden" : "Revisar salida";
  const statusText = blockedReason
    || (isReleased
      ? "La orden de salida ya fue emitida. Podés reimprimirla sin cambiar la resolución."
      : needsResolution
        ? `Se va a completar la resolución como ${resolutionLabel(suggestedResolution) || "pendiente"}.`
        : `Listo para liberar con resolución ${resolutionLabel(resolucion)}.`);

  return {
    id,
    estado,
    presupuesto,
    resolucion,
    suggestedResolution,
    isReleased,
    needsResolution,
    canSubmit,
    blockedReason,
    primaryLabel,
    statusText,
  };
}
