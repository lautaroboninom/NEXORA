// Tabla central de acciones sobre un equipo.
//
// Sigue el modelo de `release-flow.js`: acá vive la decisión de QUÉ acción aplica
// a una fila y CÓMO se llama; los handlers los aporta cada página. Así el menú de
// Equipos, el de la hoja de servicio y cualquier listado futuro ofrecen las mismas
// acciones con las mismas etiquetas sin copiar lógica.

import { canAny, PERMISSION_CODES } from "./permissions.js";

export const DEVICE_ACTION = {
  VER_INGRESO: "ver_ingreso",
  IMPRIMIR_QR: "imprimir_qr",
  EDITAR_DATOS: "editar_datos",
  VENTA_MG: "venta_mg",
  UNIFICAR: "unificar",
  DAR_BAJA: "dar_baja",
  DAR_ALTA: "dar_alta",
  SOLICITAR_BAJA: "solicitar_baja",
};

// Espejo de la lista OR que exige `IngresoSolicitarBajaView` en el backend
// (api/service/permission_policy.py). Si allá cambia, acá también.
export const DEVICE_BAJA_REQUEST_PERMISSIONS = [
  PERMISSION_CODES.ACTION_INGRESO_EDIT_BASICS,
  PERMISSION_CODES.ACTION_INGRESO_EDIT_DIAGNOSIS,
  PERMISSION_CODES.ACTION_INGRESO_EDIT_LOCATION,
  PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY,
  PERMISSION_CODES.ACTION_INGRESO_MANAGE_DERIVATIONS,
  PERMISSION_CODES.ACTION_INGRESO_REPAIR_TRANSITIONS,
  PERMISSION_CODES.ACTION_PRESUPUESTO_MANAGE,
];

// El backend rechaza `solicitar-baja` si el usuario ya tiene la baja directa,
// así que las dos variantes son mutuamente excluyentes por diseño.
export function canRequestDeviceBaja(user) {
  return canAny(user, DEVICE_BAJA_REQUEST_PERMISSIONS);
}

const text = (value) => String(value ?? "").trim();

export const lastIngresoEstadoOf = (row) =>
  text(row?.last_ingreso_estado || row?.estado).toLowerCase();

// Sin fallback a `row.id`: en una fila de equipo ese id es el del DEVICE, y
// confundirlo con el del ingreso haría operar sobre una OS ajena.
export const lastIngresoIdOf = (row) => {
  const id = Number(row?.last_ingreso_id ?? 0);
  return Number.isFinite(id) && id > 0 ? id : 0;
};

// Sólo se puede dar de baja un equipo propio MG: es el mismo corte que ya aplica
// "Marcar vendido", y el backend lo rechaza con 400 si no se cumple.
export const isOwnDeviceMg = (row) =>
  Boolean(row?.es_propietario_mg ?? row?.equipo?.es_propietario_mg);

export const deviceBarcodeOf = (row) =>
  text(row?.numero_serie) || text(row?.numero_interno);

const SIN_INGRESO = "El equipo no tiene ingresos registrados";

/**
 * Devuelve los descriptores de acción aplicables a una fila de equipo.
 * Función pura: no toca el DOM ni dispara nada.
 *
 * @param {object} row  fila de `GET /api/devices/`
 * @param {object} ctx
 *   canEdit          — action.devices_preventivos.manage
 *   canPrintBarcode  — imprimir QR
 *   canBajaAlta      — action.ingreso.baja_alta
 *   canRequestBaja   — resultado de canRequestDeviceBaja(user)
 *   barcodeBusy      — hay una etiqueta generándose para esta fila
 *   include          — subconjunto de claves a considerar (por defecto, todas)
 * @returns {Array<{key, label, tone, disabled, title}>}
 */
export function getDeviceActions(row, ctx = {}) {
  const {
    canEdit = false,
    canPrintBarcode = false,
    canBajaAlta = false,
    canRequestBaja = false,
    barcodeBusy = false,
    include = null,
  } = ctx;

  const ingresoId = lastIngresoIdOf(row);
  const estado = lastIngresoEstadoOf(row);
  const esMg = isOwnDeviceMg(row);
  const barcode = deviceBarcodeOf(row);
  const enBaja = estado === "baja";

  const actions = [];

  actions.push({
    key: DEVICE_ACTION.VER_INGRESO,
    label: "Ver ingreso",
    tone: "default",
    disabled: !ingresoId,
    title: ingresoId ? "" : SIN_INGRESO,
  });

  if (canPrintBarcode) {
    actions.push({
      key: DEVICE_ACTION.IMPRIMIR_QR,
      label: barcodeBusy ? "Preparando QR..." : "Imprimir QR",
      tone: "default",
      disabled: !barcode || barcodeBusy,
      title: barcode ? "" : "El equipo no tiene N/S ni número interno",
    });
  }

  if (canEdit) {
    actions.push({
      key: DEVICE_ACTION.EDITAR_DATOS,
      label: "Editar datos",
      tone: "default",
      disabled: false,
      title: "",
    });
  }

  if (canEdit && esMg && text(row?.numero_interno)) {
    actions.push({
      key: DEVICE_ACTION.VENTA_MG,
      label: row?.mg_inactivo_venta ? "Reactivar MG" : "Marcar vendido",
      tone: "default",
      disabled: false,
      title: "",
    });
  }

  if (canEdit) {
    actions.push({
      key: DEVICE_ACTION.UNIFICAR,
      label: "Unificar",
      tone: "default",
      disabled: false,
      title: "",
    });
  }

  // Baja / alta / solicitud: sólo sobre equipos propios MG, y siempre sobre el
  // último ingreso, porque la baja es un estado del ingreso y no del equipo.
  if (esMg) {
    if (canBajaAlta && enBaja) {
      actions.push({
        key: DEVICE_ACTION.DAR_ALTA,
        label: "Dar ALTA al equipo",
        tone: "success",
        disabled: !ingresoId,
        title: ingresoId ? "" : SIN_INGRESO,
      });
    } else if (canBajaAlta) {
      actions.push({
        key: DEVICE_ACTION.DAR_BAJA,
        label: "Dar BAJA al equipo",
        tone: "danger",
        disabled: !ingresoId,
        title: ingresoId ? "" : SIN_INGRESO,
      });
    } else if (canRequestBaja && !enBaja) {
      actions.push({
        key: DEVICE_ACTION.SOLICITAR_BAJA,
        label: "Solicitar BAJA",
        tone: "danger",
        disabled: !ingresoId,
        title: ingresoId ? "" : SIN_INGRESO,
      });
    }
  }

  if (!Array.isArray(include)) return actions;
  const allowed = new Set(include);
  return actions.filter((action) => allowed.has(action.key));
}

// El endpoint de baja/alta responde con el estado del job de stock de Bejerman.
// Reportarlo importa: la baja queda registrada en NEXORA aunque el movimiento a
// Desguace falle, y en ese caso alguien tiene que ir a mirar la cola.
export function describeBejermanStockJob(job, accionLabel = "Baja") {
  const status = text(job?.status).toLowerCase();
  const detail = text(job?.last_error || job?.detail);
  if (status === "succeeded") {
    return `${accionLabel} registrada. La sincronización Bejerman fue completada.`;
  }
  if (status === "blocked") {
    return `${accionLabel} registrada. La sincronización Bejerman quedó bloqueada${
      detail ? `: ${detail}` : "."
    } Revisá la cola Bejerman.`;
  }
  if (status === "not_queued") {
    return `${accionLabel} registrada. No se pudo encolar la sincronización Bejerman${
      detail ? `: ${detail}` : "."
    }`;
  }
  if (status) {
    return `${accionLabel} registrada. La sincronización Bejerman quedó en cola.`;
  }
  return `${accionLabel} registrada.`;
}
