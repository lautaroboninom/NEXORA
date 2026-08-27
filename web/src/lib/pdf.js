import { fetchWithAuth } from "./api";
import {
  buildPdfWaitPageHtml,
  escapeHtml,
  PDF_REPRINT_STEPS,
  PDF_WAIT_STEPS,
  waitPageUpdateFrom,
} from "./pdf-wait-page";
import { isRegisteredNoPdfCode, registeredNoPdfMessage } from "./remitos";

export { PDF_REPRINT_STEPS, PDF_WAIT_STEPS };

export function openPdfBlob(blob, reservedWindow = null) {
  const url = URL.createObjectURL(blob);
  let opened = false;

  try {
    if (reservedWindow && !reservedWindow.closed) {
      reservedWindow.location.href = url;
      opened = true;
    } else {
      opened = openDetachedWindow(url);
    }
  } catch (_) {
    opened = false;
  }

  setTimeout(() => URL.revokeObjectURL(url), 60_000);
  return opened;
}
export function openDetachedWindow(url) {
  const href = String(url || "").trim();
  if (!href) return false;
  try {
    const target = window.open("", "_blank");
    if (!target) return false;
    try {
      target.opener = null;
    } catch (_) {}
    target.location.href = href;
    return true;
  } catch (_) {
    return false;
  }
}

function delay(ms, signal) {
  const wait = Math.max(0, Number(ms) || 0);
  if (!signal) return new Promise((resolve) => window.setTimeout(resolve, wait));
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = window.setTimeout(finish, wait);
    function finish() {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
    signal.addEventListener("abort", finish, { once: true });
  });
}

// Tope por defecto de la espera del PDF. Antes no había ninguno: mientras la API
// respondiera 202, el modal giraba para siempre y no se podía ni cancelar.
export const PDF_WAIT_DEADLINE_MS = 120000;

export const PDF_WAIT_TIMEOUT = "PDF_WAIT_TIMEOUT";
export const PDF_WAIT_CANCELLED = "PDF_WAIT_CANCELLED";

/**
 * ¿El documento está bien y sólo falta el PDF?
 * Sirve para no pintar de rojo un remito que se emitió perfecto en Bejerman.
 */
export function isPendingPdfError(error) {
  if (!error) return false;
  if (error.pending) return true;
  return [PDF_WAIT_TIMEOUT, PDF_WAIT_CANCELLED].includes(error.code);
}

function pendingWaitError(message, code, extra = {}) {
  const error = new Error(message);
  error.code = code;
  error.pending = true;
  error.terminal = true;
  Object.assign(error, extra);
  return error;
}

function hasOnlyPdfPadding(bytes, end) {
  if (!bytes || end <= 0) return true;
  let index = 0;
  if (end >= 3 && bytes[0] === 239 && bytes[1] === 187 && bytes[2] === 191) {
    index = 3;
  }
  for (; index < end; index += 1) {
    const value = bytes[index];
    if (value !== 9 && value !== 10 && value !== 12 && value !== 13 && value !== 32) {
      return false;
    }
  }
  return true;
}

function pdfBytesFromResponse(bytes) {
  const marker = [37, 80, 68, 70, 45];
  const hasMarkerAt = (offset) =>
    bytes?.length >= offset + marker.length &&
    marker.every((value, index) => bytes[offset + index] === value);

  if (hasMarkerAt(0)) return bytes;
  const limit = Math.min(Math.max((bytes?.length || 0) - marker.length, 0), 16);
  for (let offset = 1; offset <= limit; offset += 1) {
    if (hasMarkerAt(offset) && hasOnlyPdfPadding(bytes, offset)) {
      return bytes.subarray(offset);
    }
  }
  return null;
}

function cacheBustedUrl(url) {
  const href = String(url || "").trim();
  if (!href) return "";
  const separator = href.includes("?") ? "&" : "?";
  return `${href}${separator}t=${Date.now()}`;
}

async function readResponsePayload(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json().catch(() => ({}));
  }
  const text = await response.text().catch(() => "");
  const trimmed = String(text || "").trimStart().toLowerCase();
  return {
    detail: text,
    html: contentType.toLowerCase().includes("html") || trimmed.startsWith("<!doctype") || trimmed.startsWith("<html"),
  };
}

function retryDelayFrom(response, payload, attempt) {
  const retryAfterHeader = String(response.headers.get("Retry-After") || "").trim();
  const retryAfterSeconds = Number(retryAfterHeader);
  if (retryAfterSeconds > 0) return retryAfterSeconds * 1000;
  const retryAfterDate = Date.parse(retryAfterHeader);
  if (Number.isFinite(retryAfterDate) && retryAfterDate > Date.now()) return retryAfterDate - Date.now();
  const retryAfterMs = Number(payload?.retry_after_ms || 0);
  if (retryAfterMs > 0) return retryAfterMs;
  return Math.min(900 + attempt * 250, 2000);
}

function normalizeLookupText(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function isTransientPdfLookupMiss(response, payload) {
  if (![404, 502, 503, 504].includes(Number(response?.status))) return false;
  const text = normalizeLookupText(`${payload?.code || ""} ${payload?.detail || ""}`);
  return (
    text.includes("remito_document_not_found") ||
    text.includes("no se encontro el comprobante asociado") ||
    text.includes("no se encontro el pdf del comprobante") ||
    text.includes("no se encontro en bejerman un remito")
  );
}

function notifyProgress(onProgress, payload) {
  if (typeof onProgress !== "function") return;
  try {
    onProgress(payload);
  } catch (_) {}
}

/**
 * La pestaña de impresión ya está abierta y en blanco desde el click: sin esto
 * el operador ve un spinner mudo mientras NEXORA sí sabe en qué etapa está.
 */
function mirrorProgress(reservedWindow, payload) {
  if (!reservedWindow || typeof reservedWindow.update !== "function") return;
  reservedWindow.update(waitPageUpdateFrom(payload));
}

function responseErrorMessage(response, payload, label) {
  const detail = payload?.detail ? String(payload.detail) : "";
  const code = payload?.code ? String(payload.code) : "";
  if (response.status === 401 || response.status === 403) {
    return `La sesión no tiene permiso para ver este ${label}.`;
  }
  if (isRegisteredNoPdfCode(code)) {
    return detail || registeredNoPdfMessage(label);
  }
  if (response.status === 409) {
    return detail || `El ${label} todavía no está emitido o no tiene referencia de comprobante.`;
  }
  if (payload?.html && [502, 503, 504].includes(Number(response.status))) {
    return `Bejerman no pudo devolver el PDF del ${label} en este momento. Probá de nuevo en unos minutos.`;
  }
  if (payload?.html) {
    return `No se pudo obtener el PDF del ${label}.`;
  }
  return detail || `No se pudo obtener el PDF del ${label}.`;
}

export async function waitForPdfBlob(pdfUrl, {
  label = "PDF",
  fetchTimeoutMs = 10000,
  deadlineMs = PDF_WAIT_DEADLINE_MS,
  signal,
  onProgress,
  reservedWindow = null,
} = {}) {
  const href = String(pdfUrl || "").trim();
  if (!href) throw new Error(`No se recibió la URL del PDF del ${label}.`);

  const report = (payload) => {
    notifyProgress(onProgress, payload);
    mirrorProgress(reservedWindow, payload);
  };

  const startedAt = Date.now();
  const elapsed = () => Date.now() - startedAt;
  const cancelled = () =>
    pendingWaitError(
      `Se canceló la espera del PDF del ${label}. El ${label} ya está emitido en Bejerman.`,
      PDF_WAIT_CANCELLED,
      { elapsedMs: elapsed() },
    );
  const timedOut = (documentKey) =>
    pendingWaitError(
      `Bejerman todavía no devolvió el PDF del ${label}. El ${label} ya está emitido; se puede reintentar o reimprimirlo más tarde.`,
      PDF_WAIT_TIMEOUT,
      { elapsedMs: elapsed(), documentKey },
    );

  let attempt = 0;
  let documentKey = "";

  while (true) {
    if (signal?.aborted) throw cancelled();
    if (deadlineMs > 0 && elapsed() >= deadlineMs) throw timedOut(documentKey);

    attempt += 1;
    report({
      state: "fetching",
      attempt,
      elapsedMs: elapsed(),
      status: attempt === 1 ? `Buscando PDF del ${label}...` : `Esperando PDF del ${label}...`,
      detail: "NEXORA está comprobando el PDF sin abrir una nueva ventana.",
    });

    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timeout = controller ? window.setTimeout(() => controller.abort(), fetchTimeoutMs) : null;

    try {
      const response = await fetchWithAuth(cacheBustedUrl(href), {
        method: "GET",
        cache: "no-store",
        skipBejermanActivity: true,
        ...(controller ? { signal: controller.signal } : {}),
      });
      if (timeout) window.clearTimeout(timeout);

      if (response.status === 202) {
        const payload = await readResponsePayload(response);
        documentKey = payload?.document_key || documentKey;
        const waitMs = retryDelayFrom(response, payload, attempt);
        report({
          state: "pending",
          attempt,
          delayMs: waitMs,
          elapsedMs: elapsed(),
          // El backend manda en qué etapa está; usarlo es la diferencia entre
          // "algo pasa" y un spinner mudo que parece colgado.
          pdfState: payload?.pdf_state || "",
          stageLabel: payload?.stage_label || "",
          workerAlive: payload?.worker_alive,
          documentKey,
          status: payload?.stage_label
            ? `${label} emitido. ${payload.stage_label}.`
            : `${label} emitido. NEXORA está preparando el PDF.`,
          detail: payload?.detail || "NEXORA vuelve a comprobarlo automáticamente.",
        });
        await delay(waitMs, signal);
        continue;
      }

      if (response.status === 200) {
        const buffer = await response.arrayBuffer();
        const bytes = pdfBytesFromResponse(new Uint8Array(buffer));
        if (!bytes) {
          const error = new Error(`No se pudo obtener un PDF válido del ${label}. Probá de nuevo en unos minutos.`);
          error.terminal = true;
          throw error;
        }
        report({
          state: "ready",
          attempt,
          elapsedMs: elapsed(),
          status: `PDF del ${label} listo.`,
          detail: "Abriendo impresión.",
        });
        return new Blob([bytes], { type: "application/pdf" });
      }

      const payload = await readResponsePayload(response);
      documentKey = payload?.document_key || documentKey;
      if (isTransientPdfLookupMiss(response, payload)) {
        const waitMs = retryDelayFrom(response, payload, attempt);
        report({
          state: "pending",
          attempt,
          delayMs: waitMs,
          elapsedMs: elapsed(),
          documentKey,
          status: `${label} emitido. NEXORA todavía no tiene el PDF disponible.`,
          detail: payload?.detail || "NEXORA vuelve a comprobarlo automáticamente.",
        });
        await delay(waitMs, signal);
        continue;
      }

      const error = new Error(responseErrorMessage(response, payload, label));
      error.terminal = true;
      error.code = payload?.code || "";
      error.documentKey = documentKey;
      // El comprobante está emitido y sólo falta el archivo: es reintentable,
      // no un error del remito.
      error.pending = ["BEJERMAN_PDF_WORKER_STALLED", "BEJERMAN_PDF_NOT_PUBLISHED"].includes(error.code);
      throw error;
    } catch (error) {
      if (timeout) window.clearTimeout(timeout);
      if (error?.terminal) {
        throw error;
      }
      if (signal?.aborted) throw cancelled();
      const waitMs = Math.min(900 + attempt * 250, 2000);
      report({
        state: "retrying",
        attempt,
        delayMs: waitMs,
        elapsedMs: elapsed(),
        documentKey,
        status: `La consulta del PDF del ${label} tardó demasiado.`,
        detail: "NEXORA vuelve a comprobarlo automáticamente.",
      });
      await delay(waitMs, signal);
    }
  }
}

export function openPrintablePdf(blob, {
  title = "PDF",
  documentLabel = title,
  printDelayMs = 700,
  autoRevokeMs = 300000,
  reservedWindow = null,
} = {}) {
  if (!(blob instanceof Blob)) return { opened: false, reason: "invalid_blob" };

  const url = URL.createObjectURL(blob);
  const revoke = () => {
    try {
      URL.revokeObjectURL(url);
    } catch (_) {}
  };
  let target = null;

  try {
    target = reservedWindow && !reservedWindow.closed ? reservedWindow : window.open("", "_blank");
    if (!target) {
      revoke();
      return { opened: false, reason: "blocked" };
    }
    try {
      target.opener = null;
    } catch (_) {}

    const safeTitle = escapeHtml(title);
    const safeLabel = escapeHtml(documentLabel || title);
    const safeUrl = escapeHtml(url);
    const safePrintDelayMs = Math.max(100, Number(printDelayMs) || 700);

    target.document.write(`<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${safeTitle}</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; height: 100vh; display: grid; grid-template-rows: auto 1fr; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #111827; background: #f3f4f6; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border-bottom: 1px solid #d1d5db; background: white; }
    strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    button, a { border: 1px solid #9ca3af; border-radius: 8px; background: #111827; color: white; padding: 8px 12px; font: inherit; font-size: 13px; text-decoration: none; cursor: pointer; }
    a { background: white; color: #111827; }
    iframe { width: 100%; height: 100%; border: 0; background: white; }
    @media print {
      header { display: none; }
      body { display: block; background: white; }
      iframe { height: 100vh; }
    }
  </style>
</head>
<body>
  <header>
    <strong>${safeLabel}</strong>
    <div class="actions">
      <button id="print" type="button">Imprimir</button>
      <a href="${safeUrl}" target="_self">Abrir PDF</a>
      <button id="close" type="button">Cerrar</button>
    </div>
  </header>
  <iframe id="pdf-frame" title="${safeLabel}" src="${safeUrl}"></iframe>
  <script>
    const frame = document.getElementById("pdf-frame");
    const printButton = document.getElementById("print");
    const closeButton = document.getElementById("close");
    function printPdf() {
      try {
        frame.contentWindow.focus();
        frame.contentWindow.print();
      } catch (error) {
        try { window.print(); } catch (_) {}
      }
    }
    printButton.addEventListener("click", printPdf);
    closeButton.addEventListener("click", () => window.close());
    frame.addEventListener("load", () => window.setTimeout(printPdf, ${safePrintDelayMs}));
  </script>
</body>
</html>`);
    target.document.close();
    window.setTimeout(revoke, Math.max(60000, Number(autoRevokeMs) || 300000));
    return { opened: true, reason: "", revoke };
  } catch (_) {
    try {
      if (target && !target.closed) {
        target.location.href = url;
        window.setTimeout(revoke, Math.max(60000, Number(autoRevokeMs) || 300000));
        return { opened: true, reason: "fallback_url", revoke };
      }
    } catch (_) {}
    revoke();
    return { opened: false, reason: "error" };
  }
}

/** "RT - NEXORA" es el nombre de la pestaña; el título del cartel es sólo el comprobante. */
function headingFromTitle(title) {
  const clean = String(title || "").replace(/\s*[-–]\s*NEXORA\s*$/i, "").trim();
  return clean ? `Preparando ${clean}` : "Preparando la impresión";
}

/**
 * Abre ya la pestaña donde va a salir el PDF y la deja contando en qué anda.
 *
 * `heading`/`message`/`detail` son los mismos textos que muestra el modal de
 * progreso de NEXORA, y `update()` deja que el flujo que emite el comprobante
 * los vaya corrigiendo con lo que realmente está pasando del otro lado.
 */
export function reservePdfWindow({
  title = "REMITO",
  heading = "",
  message = "NEXORA está preparando el PDF.",
  detail = "Esta pestaña muestra el PDF y abre la impresión cuando el archivo esté listo.",
  fallbackMessage = "La operación está tardando más de lo habitual. Puede cerrar esta pestaña y volver a intentar desde NEXORA.",
  fallbackAfterMs = 20000,
  steps = PDF_WAIT_STEPS,
} = {}) {
  let target = null;

  try {
    target = window.open("", "_blank");
    if (target) {
      try {
        target.opener = null;
      } catch (_) {}
      try {
        target.document.write(
          buildPdfWaitPageHtml({
            title,
            heading: heading || headingFromTitle(title),
            status: message,
            detail,
            fallbackMessage,
            fallbackAfterMs,
            steps,
          }),
        );
        target.document.close();
      } catch (_) {}
    }
  } catch (_) {
    target = null;
  }

  return {
    /** Empuja el estado real a la pestaña; devuelve false si ya la cerraron. */
    update(patch) {
      try {
        if (target && !target.closed && target.NEXORA_PDF_WAIT) {
          target.NEXORA_PDF_WAIT.update(patch || {});
          return true;
        }
      } catch (_) {}
      return false;
    },
    open(blob) {
      return openPdfBlob(blob, target);
    },
    openPrintable(blob, options = {}) {
      return openPrintablePdf(blob, { ...options, reservedWindow: target });
    },
    openUrl(url) {
      const href = String(url || "").trim();
      if (!href) return false;
      try {
        if (target && !target.closed) {
          target.location.href = href;
          return true;
        }
        return openDetachedWindow(href);
      } catch (_) {
        return false;
      }
    },
    close() {
      try {
        if (target && !target.closed) target.close();
      } catch (_) {}
    },
  };
}
