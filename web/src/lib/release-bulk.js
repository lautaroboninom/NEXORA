import { getReleaseFlow } from "./release-flow.js";
import { ingresoIdOf } from "./ui-helpers.js";

export const BULK_RELEASE_HEADER_EMITIDOS = "X-Nexora-Remitos-Emitidos";
export const BULK_RELEASE_HEADER_FALLIDOS = "X-Nexora-Remitos-Fallidos";

// El backend topea el lote en 100 ingresos (_parse_ingreso_ids).
export const BULK_RELEASE_MAX_IDS = 100;

function headerValue(headers, name) {
  if (!headers) return "";
  if (typeof headers.get === "function") return headers.get(name) || "";
  return headers[name] ?? headers[name.toLowerCase()] ?? "";
}

/**
 * Separa las filas elegidas entre las que se pueden liberar y las que no.
 *
 * La lógica de "¿esta OS se puede liberar?" ya vive en getReleaseFlow, la misma
 * que usa el modal de una sola OS: acá sólo se recorre la selección para poder
 * avisar antes de tocar nada. Liberar no tiene vuelta atrás (cambia el estado,
 * notifica y encola movimientos de stock), así que conviene mostrar el detalle
 * antes de mandar el lote.
 */
export function classifyReleaseSelection(rows, { canManageResolution = false } = {}) {
  const ready = [];
  const blocked = [];
  const seen = new Set();

  for (const row of rows || []) {
    const id = ingresoIdOf(row);
    if (!id) {
      blocked.push({ id: null, row, reason: "No se pudo identificar la OS." });
      continue;
    }
    if (seen.has(id)) continue;
    seen.add(id);

    const flow = getReleaseFlow(row, { canManageResolution });
    if (flow.isReprint) {
      blocked.push({ id, row, flow, reason: "La orden de salida ya fue emitida." });
      continue;
    }
    if (flow.blockedReason) {
      blocked.push({ id, row, flow, reason: flow.blockedReason });
      continue;
    }
    if (!flow.canSubmit) {
      blocked.push({
        id,
        row,
        flow,
        reason: "Todavía falta cerrar la reparación o definir una resolución de salida.",
      });
      continue;
    }
    ready.push({ id, row, flow });
  }

  return { ready, blocked };
}

/** Lee los headers con los que el backend informa el resultado del lote. */
export function parseBulkReleaseHeaders(headers) {
  const emitidos = String(headerValue(headers, BULK_RELEASE_HEADER_EMITIDOS) || "")
    .split(",")
    .map((value) => Number(String(value).trim()))
    .filter((value) => Number.isFinite(value) && value > 0);

  let fallidos = [];
  const raw = headerValue(headers, BULK_RELEASE_HEADER_FALLIDOS);
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) fallidos = parsed;
    } catch (_) {
      fallidos = [];
    }
  }

  return { emitidos, fallidos };
}

/**
 * Cuando el lote entero falla, el backend responde 409 con el detalle por OS en
 * el cuerpo en vez de los headers.
 */
export function bulkReleaseFailuresFromError(error) {
  const fallidos = error?.data?.fallidos;
  return Array.isArray(fallidos) ? fallidos : [];
}

export function summarizeBulkRelease({ requested = 0, emitidos = [], fallidos = [], excluidos = 0 } = {}) {
  const partes = [`Elegidas: ${requested}.`, `Liberadas: ${emitidos.length}.`];
  if (excluidos > 0) partes.push(`Excluidas antes de enviar: ${excluidos}.`);
  if (fallidos.length > 0) partes.push(`Fallaron: ${fallidos.length}.`);
  return partes.join(" ");
}

/** Ruta del PDF: con una sola OS conviene el endpoint individual. */
export function bulkReleasePath(ids, { skipErrors = true } = {}) {
  const list = (ids || []).filter(Boolean);
  if (list.length === 0) return "";
  if (list.length === 1) return `/api/ingresos/${encodeURIComponent(list[0])}/remito/`;
  const params = new URLSearchParams({ ids: list.join(",") });
  if (skipErrors) params.set("skip_errors", "1");
  return `/api/ingresos/remitos-salida/?${params.toString()}`;
}
