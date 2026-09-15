// Extensiones explícitas: así este módulo también se puede importar desde los
// tests, que corren con `node --test` sin el resolutor de Vite.
import { RESOLUCION, estadoLabel, resolutionLabel } from "./constants.js";
import { MG_CODE_RE, ingresoIdOf } from "./ui-helpers.js";

const CLOSED_STATES = new Set([
  "entregado",
  "alquilado",
  "baja",
  "vendido_pendiente_entrega",
  "vendido_entregado",
]);

// Estados en los que este modal sólo imprime: el backend sirve el PDF y no toca la OS
// (`ESTADOS_SALIDA_YA_EMITIDA` en reportes_views.py). En casi todos es una reimpresión de
// una orden ya emitida, que debe seguir disponible si se pierde el papel.
//
// `vendido_pendiente_entrega` entra acá aunque la orden pueda no haberse emitido nunca: la
// venta se registra desde la hoja de servicio y saltea `liberado`, así que el equipo sale
// con el RT de comercial y no con el RSS del taller. Liberarlo emitiría un remito y movería
// stock por un equipo que el RT ya sacó del patrimonio; imprimir el comprobante del trabajo
// hecho antes de la venta, en cambio, es válido y es lo que el operador necesita en el
// mostrador.
const PRINT_ONLY_STATES = new Set([
  "liberado",
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

// Motivos en los que el equipo no es del cliente (parque de alquiler, demos) o no hay
// reparación que cobrar. Espejo de MOTIVOS_SIN_PRESUPUESTO en service/release_gate.py;
// el enum convivió con y sin tilde, así que se aceptan las dos escrituras.
const MOTIVOS_SIN_PRESUPUESTO = new Set([
  "baja alquiler",
  "reparación alquiler",
  "reparacion alquiler",
  "devolución demo",
  "devolucion demo",
  "cotización de equipo",
  "cotizacion de equipo",
]);

/** ¿A esta OS hay que presupuestarle el trabajo antes de entregarla?
 *
 * Espejo de `ingreso_requiere_presupuesto()` en service/release_gate.py. Lo que no se le
 * cobra a nadie -garantías, equipos nuestros, alquileres y demos- se repara sin
 * presupuestar; el equipo de un cliente, no.
 */
export function requierePresupuestoAprobado(row = {}) {
  const flag = (value) => typeof value === "string"
    ? ["1", "true", "t", "sí", "si", "yes"].includes(normalizeReleaseValue(value))
    : Boolean(value);
  if ([row?.garantia_fabrica, row?.garantia, row?.garantia_reparacion].some(flag)) return false;
  if (MOTIVOS_SIN_PRESUPUESTO.has(normalizeReleaseValue(row?.motivo))) return false;
  // Usar los mismos campos que la API: una serie con aspecto de MG no acredita propiedad.
  if (normalizeReleaseValue(row?.mg_estado) !== "inactivo_venta"
    && MG_CODE_RE.test(String(row?.numero_interno ?? "").trim())) return false;
  return true;
}

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
  const isReprint = PRINT_ONLY_STATES.has(estado);
  const isSalePending = estado === "vendido_pendiente_entrega";
  const isSoldDelivered = estado === "vendido_entregado";
  const needsResolution = !resolucion && !isReprint;
  const suggestedResolution = releaseResolutionSuggestion(row);

  let blockedReason = "";
  if (!id) {
    blockedReason = "No se pudo identificar la OS.";
  } else if (row?.derivacion_activa && !isReprint) {
    blockedReason = "El equipo sigue derivado. Marcá la derivación como devuelta antes de liberarlo.";
  } else if (CLOSED_STATES.has(estado) && !isReprint) {
    blockedReason = `El equipo ya está en estado ${estadoLabel(estado)}.`;
  } else if (["presupuestado", "emitido"].includes(presupuesto) && !isReprint) {
    // Mismo corte que el backend (RemitoSalidaPdfView): un presupuesto emitido y
    // todavía sin respuesta del cliente no se libera. Acá se adelanta el 409 para
    // que el operador lo vea antes de apretar, tanto en el modal de una OS como en
    // el lote, que comparte este mismo flujo.
    blockedReason = "El presupuesto está emitido y todavía sin aprobar. Aprobalo o rechazalo antes de liberar.";
  } else if (
    !["aprobado", "rechazado", "no_aplica"].includes(presupuesto)
    && ["reparado", "controlado_sin_defecto"].includes(estado)
    && !isReprint
    && requierePresupuestoAprobado(row)
  ) {
    // Y el que ni siquiera se emitió tampoco: se intervino el equipo de un cliente, sin
    // garantía, y no hay nada aprobado que facturar (reparación o revisión técnica).
    // Se destraba emitiendo y aprobando el presupuesto, o marcándolo "No aplica" si el
    // trabajo no se cobra.
    blockedReason = "Esta orden no tiene presupuesto y el equipo es de un cliente. Emitilo y hacelo aprobar, o marcá el presupuesto como “No aplica” si no se cobra.";
  } else if (needsResolution && !RELEASE_READY_STATES.has(estado)) {
    blockedReason = "Todavía falta cerrar la reparación o definir una resolución de salida.";
  }

  const canSubmit = Boolean(
    id &&
      !blockedReason &&
      (isReprint || resolucion || RELEASE_READY_STATES.has(estado))
  );

  const primaryLabel = isSalePending || isSoldDelivered
    ? "Imprimir salida"
    : isReprint
      ? "Reimprimir orden"
      : "Revisar salida";
  const statusText = blockedReason
    || (isSalePending
      ? "La venta ya está registrada y la entrega sigue pendiente. Podés imprimir la orden de salida como comprobante del trabajo, sin modificar la OS. El equipo sale con el remito RT de comercial: la OS se cierra con “Marcar venta entregada” en la hoja de servicio."
      : isSoldDelivered
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
    isSalePending,
    isSoldDelivered,
    needsResolution,
    canSubmit,
    blockedReason,
    primaryLabel,
    statusText,
  };
}
