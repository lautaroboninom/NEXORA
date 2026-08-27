/**
 * La espera de una emisión de remito que corre en el servidor.
 *
 * Antes el POST de emisión se quedaba abierto hasta que Bejerman contestaba, y toda
 * la operación dependía de que esta pestaña siguiera despierta. No lo está: el
 * navegador congela las pestañas en segundo plano, y el operador se va a mirar la
 * ventana de impresión apenas se abre. El resultado era un cartel "Emitiendo" que se
 * quedaba una hora ahí aunque el remito ya hubiera salido.
 *
 * Ahora el POST sólo deja anotado el pedido y devuelve un `jobId`. Este módulo lo
 * sondea. Si el navegador nos duerme, el trabajo sigue igual del otro lado y el
 * primer sondeo al volver ya trae el resultado. Y como el `jobId` se guarda en
 * `sessionStorage`, cerrar o recargar la pestaña tampoco pierde la emisión.
 *
 * Vive aparte de `api.js` para poder testearlo con `node --test`: todo lo que toca
 * el mundo exterior entra por parámetro.
 */

export const REMITO_JOB_STORAGE_KEY = "nexora:emision-remito";

/** Tope de la espera. Generoso a propósito: el trabajo no se pierde, sólo se deja de mirar. */
export const REMITO_JOB_DEADLINE_MS = 900000;

function storage() {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch (_) {
    return null;
  }
}

export function rememberRemitoJob(jobId, orderIds = []) {
  const id = String(jobId || "").trim();
  if (!id) return;
  try {
    storage()?.setItem(
      REMITO_JOB_STORAGE_KEY,
      JSON.stringify({ jobId: id, orderIds, startedAt: Date.now() }),
    );
  } catch (_) {}
}

export function forgetRemitoJob() {
  try {
    storage()?.removeItem(REMITO_JOB_STORAGE_KEY);
  } catch (_) {}
}

/** La emisión que quedó a medias, si la hay. Sirve para retomarla al volver a entrar. */
export function pendingRemitoJob() {
  try {
    const raw = storage()?.getItem(REMITO_JOB_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    return data?.jobId ? data : null;
  } catch (_) {
    return null;
  }
}

function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error("cancelado"));
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener?.(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new Error("cancelado"));
      },
      { once: true },
    );
  });
}

/** Reconstruye un error con la misma forma que tiraba el POST sincrónico. */
export function remitoJobError(payload) {
  const data = payload || {};
  const detail = String(data.detail || "No se pudo emitir el remito.");
  const error = new Error(detail);
  error.status = Number(data.status) || 400;
  error.data = data;
  error.code = data.code || "";
  return error;
}

/**
 * Sondea hasta que la emisión termina. Devuelve el mismo objeto que antes devolvía
 * el POST, así que quien la llama no tiene que cambiar cómo lee el resultado.
 *
 * `fetchJob` se inyecta para poder testear sin red.
 */
export async function waitForRemitoJob(
  jobId,
  {
    fetchJob,
    signal,
    onProgress,
    deadlineMs = REMITO_JOB_DEADLINE_MS,
    pollMs = 1500,
    now = () => Date.now(),
  } = {},
) {
  const id = String(jobId || "").trim();
  if (!id) throw new Error("No se recibió el identificador de la emisión.");
  if (typeof fetchJob !== "function") throw new Error("Falta cómo consultar la emisión.");

  const startedAt = now();
  const elapsed = () => now() - startedAt;
  let attempt = 0;

  while (true) {
    if (signal?.aborted) {
      const cancelled = new Error("Se canceló la espera de la emisión. El remito puede haber salido igual en Bejerman.");
      cancelled.code = "REMITO_JOB_CANCELLED";
      throw cancelled;
    }
    attempt += 1;

    const estado = await fetchJob(id);
    const status = String(estado?.status || "pending");

    if (status === "done") {
      if (typeof onProgress === "function") {
        onProgress({ status: "listo", attempt, elapsedMs: elapsed() });
      }
      return estado?.result ?? {};
    }
    if (status === "error" || status === "unknown") {
      throw remitoJobError(estado?.error);
    }

    if (typeof onProgress === "function") {
      onProgress({
        status: status === "running" ? "emitiendo" : "en cola",
        attempt,
        elapsedMs: elapsed(),
        serverSeconds: Number(estado?.elapsedSeconds) || 0,
      });
    }

    if (deadlineMs > 0 && elapsed() >= deadlineMs) {
      const timedOut = new Error(
        "La emisión está tardando más de lo previsto. Sigue corriendo en el servidor: " +
          "revise el estado de la orden en unos minutos antes de reintentar.",
      );
      timedOut.code = "REMITO_JOB_TIMEOUT";
      timedOut.jobId = id;
      throw timedOut;
    }

    // Arranca sondeando seguido y afloja: los primeros segundos es cuando más
    // probable es que ya esté, y después no tiene sentido insistir tan rápido.
    await delay(Math.min(pollMs + attempt * 250, 5000), signal);
  }
}
