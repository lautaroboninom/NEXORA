import { fetchWithAuth } from "./api";
import { isRegisteredNoPdfCode, registeredNoPdfMessage } from "./remitos";

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

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, Math.max(0, Number(ms) || 0)));
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
  const retryAfter = Number(response.headers.get("Retry-After") || 0);
  if (retryAfter > 0) return retryAfter * 1000;
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
  onProgress,
} = {}) {
  const href = String(pdfUrl || "").trim();
  if (!href) throw new Error(`No se recibió la URL del PDF del ${label}.`);

  let attempt = 0;

  while (true) {
    attempt += 1;
    notifyProgress(onProgress, {
      state: "fetching",
      attempt,
      status: attempt === 1 ? `Buscando PDF del ${label}...` : `Esperando PDF del ${label}...`,
      detail: "Consultando Bejerman sin abrir una nueva ventana.",
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

      if (response.ok) {
        const buffer = await response.arrayBuffer();
        const bytes = pdfBytesFromResponse(new Uint8Array(buffer));
        if (!bytes) {
          const error = new Error(`No se pudo obtener un PDF válido del ${label}. Probá de nuevo en unos minutos.`);
          error.terminal = true;
          throw error;
        }
        notifyProgress(onProgress, {
          state: "ready",
          attempt,
          status: `PDF del ${label} listo.`,
          detail: "Abriendo impresión.",
        });
        return new Blob([bytes], { type: response.headers.get("content-type") || "application/pdf" });
      }

      const payload = await readResponsePayload(response);
      if (response.status === 202) {
        const waitMs = retryDelayFrom(response, payload, attempt);
        notifyProgress(onProgress, {
          state: "pending",
          attempt,
          delayMs: waitMs,
          status: `${label} emitido. Bejerman todavía está preparando el PDF.`,
          detail: payload?.detail || "NEXORA volverá a consultar automáticamente.",
        });
        await delay(waitMs);
        continue;
      }

      if (isTransientPdfLookupMiss(response, payload)) {
        const waitMs = retryDelayFrom(response, payload, attempt);
        notifyProgress(onProgress, {
          state: "pending",
          attempt,
          delayMs: waitMs,
          status: `${label} emitido. Bejerman todavía no lo devuelve en la consulta.`,
          detail: payload?.detail || "NEXORA volverá a consultar automáticamente.",
        });
        await delay(waitMs);
        continue;
      }

      const error = new Error(responseErrorMessage(response, payload, label));
      error.terminal = true;
      throw error;
    } catch (error) {
      if (timeout) window.clearTimeout(timeout);
      if (error?.terminal) {
        throw error;
      }
      const waitMs = Math.min(900 + attempt * 250, 2000);
      notifyProgress(onProgress, {
        state: "retrying",
        attempt,
        delayMs: waitMs,
        status: `La consulta del PDF del ${label} tardó demasiado.`,
        detail: "NEXORA volverá a consultar automáticamente.",
      });
      await delay(waitMs);
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

export function reservePdfWindow({
  title = "REMITO",
  message = "Preparando PDF...",
  fallbackMessage = "La operación está tardando más de lo habitual. Puede cerrar esta pestaña y volver a intentar desde NEXORA.",
  fallbackAfterMs = 20000,
} = {}) {
  let target = null;
  const safeTitle = escapeHtml(title);
  const safeMessage = escapeHtml(message);
  const safeFallback = escapeHtml(fallbackMessage);
  const safeFallbackAfterMs = Math.max(5000, Number(fallbackAfterMs) || 20000);

  try {
    target = window.open("", "_blank");
    if (target) {
      try {
        target.opener = null;
      } catch (_) {}
      try {
        target.document.write(`<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>${safeTitle}</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: system-ui, sans-serif; color: #111827; background: #f9fafb; }
    main { width: min(520px, calc(100vw - 32px)); text-align: center; }
    span { display: inline-block; width: 28px; height: 28px; margin-bottom: 12px; border: 3px solid #bae6fd; border-top-color: #0369a1; border-radius: 999px; animation: spin 0.8s linear infinite; }
    p { margin: 0; font-size: 14px; }
    .fallback { display: none; margin-top: 14px; color: #4b5563; line-height: 1.45; }
    .actions { display: none; justify-content: center; gap: 10px; margin-top: 16px; }
    .visible { display: flex; }
    .fallback.visible { display: block; }
    button { border: 1px solid #9ca3af; border-radius: 8px; background: #111827; color: white; padding: 8px 12px; font: inherit; cursor: pointer; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <main>
    <span></span>
    <p>${safeMessage}</p>
    <p id="fallback" class="fallback">${safeFallback}</p>
    <div id="actions" class="actions">
      <button type="button" onclick="window.close()">Cerrar pestaña</button>
    </div>
  </main>
  <script>
    window.setTimeout(function () {
      var fallback = document.getElementById("fallback");
      var actions = document.getElementById("actions");
      if (fallback) fallback.classList.add("visible");
      if (actions) actions.classList.add("visible");
    }, ${safeFallbackAfterMs});
  </script>
</body>
</html>`);
        target.document.close();
      } catch (_) {}
    }
  } catch (_) {
    target = null;
  }

  return {
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
