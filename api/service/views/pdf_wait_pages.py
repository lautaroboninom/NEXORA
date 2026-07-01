from __future__ import annotations

import json
from html import escape


def render_bejerman_pdf_wait_page(
    *,
    pdf_url: str,
    document_name: str = "remito",
    title: str = "Remito Bejerman",
    fallback_detail: str = "Puede reintentar o volver a la pantalla de NEXORA.",
) -> str:
    clean_pdf_url = str(pdf_url or "").strip()
    clean_document_name = str(document_name or "remito").strip() or "remito"
    clean_title = str(title or "Remito Bejerman").strip() or "Remito Bejerman"
    clean_fallback_detail = str(fallback_detail or "").strip() or "Puede reintentar o volver a la pantalla de NEXORA."
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(clean_title)}</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; color: #111827; }}
    main {{ width: min(680px, calc(100vw - 32px)); padding: 32px; background: white; border: 1px solid #d1d5db; border-radius: 8px; box-shadow: 0 18px 50px rgba(17, 24, 39, .08); text-align: center; }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    p {{ line-height: 1.55; }}
    .spinner {{ display: inline-block; width: 30px; height: 30px; margin-bottom: 14px; border: 3px solid #bae6fd; border-top-color: #0369a1; border-radius: 999px; animation: spin .8s linear infinite; }}
    .detail {{ margin-top: 8px; color: #4b5563; font-size: 14px; }}
    .actions {{ display: none; gap: 12px; flex-wrap: wrap; margin-top: 20px; }}
    .actions.visible {{ display: flex; justify-content: center; }}
    button, a {{ border: 1px solid #9ca3af; border-radius: 8px; background: #111827; color: white; padding: 10px 14px; font: inherit; text-decoration: none; cursor: pointer; }}
    a {{ background: white; color: #111827; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <main>
    <span class="spinner" aria-hidden="true"></span>
    <h1>Preparando {escape(clean_document_name)}</h1>
    <p id="status" aria-live="polite">Esperando el PDF emitido por Bejerman...</p>
    <p id="detail" class="detail">Esta pestaña se reemplazará por el PDF cuando esté disponible.</p>
    <div id="actions" class="actions">
      <button id="retry" type="button">Reintentar</button>
      <a href="{escape(clean_pdf_url)}" target="_self">Abrir PDF directo</a>
      <button id="close" type="button">Cerrar pestaña</button>
    </div>
  </main>
  <script>
    const pdfUrl = {json.dumps(clean_pdf_url)};
    const documentName = {json.dumps(clean_document_name)};
    const fallbackDetail = {json.dumps(clean_fallback_detail)};
    const statusEl = document.getElementById('status');
    const detailEl = document.getElementById('detail');
    const actionsEl = document.getElementById('actions');
    const retryEl = document.getElementById('retry');
    const closeEl = document.getElementById('close');
    let attempts = 0;
    const lookupMissRetryLimit = 20;
    const REMITO_DOCUMENT_NOT_FOUND = 'REMITO_DOCUMENT_NOT_FOUND';

    function hasOnlyPdfPadding(bytes, end) {{
      let index = 0;
      if (end >= 3 && bytes[0] === 239 && bytes[1] === 187 && bytes[2] === 191) {{
        index = 3;
      }}
      for (; index < end; index += 1) {{
        const value = bytes[index];
        if (value !== 9 && value !== 10 && value !== 12 && value !== 13 && value !== 32) return false;
      }}
      return true;
    }}

    function pdfBytesFromResponse(bytes) {{
      const marker = [37, 80, 68, 70, 45];
      const hasMarkerAt = (offset) =>
        bytes.length >= offset + marker.length &&
        marker.every((value, index) => bytes[offset + index] === value);
      if (hasMarkerAt(0)) return bytes;
      const limit = Math.min(Math.max(bytes.length - marker.length, 0), 16);
      for (let offset = 1; offset <= limit; offset += 1) {{
        if (hasMarkerAt(offset) && hasOnlyPdfPadding(bytes, offset)) return bytes.subarray(offset);
      }}
      return null;
    }}

    function showActions(visible) {{
      actionsEl.classList.toggle('visible', visible);
    }}

    function fail(message, detail) {{
      statusEl.textContent = message;
      detailEl.textContent = detail || fallbackDetail;
      showActions(true);
    }}

    function retryDelayFrom(response, payload) {{
      const retryAfter = Number(response.headers.get('Retry-After') || 0);
      if (retryAfter > 0) return retryAfter * 1000;
      const retryAfterMs = Number(payload && payload.retry_after_ms || 0);
      if (retryAfterMs > 0) return retryAfterMs;
      return Math.min(900 + attempts * 250, 2000);
    }}

    function normalizeLookupText(value) {{
      return String(value || '')
        .normalize('NFD')
        .replace(/[\\u0300-\\u036f]/g, '')
        .toLowerCase();
    }}

    function shouldRetryLookupMiss(response, payload) {{
      if (![404, 502, 503, 504].includes(response.status) || attempts >= lookupMissRetryLimit) return false;
      const text = normalizeLookupText(String((payload && payload.code) || '') + ' ' + String((payload && payload.detail) || ''));
      return text.includes(REMITO_DOCUMENT_NOT_FOUND.toLowerCase())
        || text.includes('no se encontro el comprobante asociado')
        || text.includes('no se encontro el pdf del comprobante')
        || text.includes('no se encontro el archivo pdf')
        || text.includes('no se encontro en bejerman un remito');
    }}

    async function readResponsePayload(response) {{
      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {{
        return await response.json().catch(() => ({{}}));
      }}
      const text = await response.text().catch(() => '');
      return {{ detail: text }};
    }}

    function scheduleNext(delayMs) {{
      window.setTimeout(pollPdf, Math.max(1000, Number(delayMs) || 1500));
    }}

    async function pollPdf() {{
      attempts += 1;
      showActions(false);
      statusEl.textContent = attempts === 1
        ? 'Buscando el PDF del ' + documentName + ' emitido por Bejerman...'
        : 'El ' + documentName + ' ya fue emitido. Esperando PDF...';
      detailEl.textContent = 'Consultando Bejerman sin bloquear NEXORA.';

      try {{
        const separator = pdfUrl.includes('?') ? '&' : '?';
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 25000);
        const response = await fetch(pdfUrl + separator + 't=' + Date.now(), {{
          cache: 'no-store',
          credentials: 'same-origin',
          signal: controller.signal,
        }});
        window.clearTimeout(timeout);

        if (response.ok) {{
          const buffer = await response.arrayBuffer();
          const bytes = pdfBytesFromResponse(new Uint8Array(buffer));
          if (bytes) {{
            const blob = new Blob([bytes], {{ type: 'application/pdf' }});
            window.location.replace(URL.createObjectURL(blob));
            return;
          }}
          fail('NEXORA recibió una respuesta que no es un PDF válido.', 'Reintente la impresión desde esta pestaña.');
          return;
        }}

        const payload = await readResponsePayload(response);
        const detail = payload && payload.detail ? String(payload.detail) : '';

        if (response.status === 202) {{
          statusEl.textContent = 'El ' + documentName + ' está emitido. Bejerman todavía está preparando el PDF.';
          detailEl.textContent = detail || 'Esperando la disponibilidad del archivo para imprimir.';
          scheduleNext(retryDelayFrom(response, payload));
          return;
        }}
        if (shouldRetryLookupMiss(response, payload)) {{
          statusEl.textContent = 'Bejerman todavía no devolvió el ' + documentName + ' en la consulta.';
          detailEl.textContent = 'NEXORA volverá a buscar el remito automáticamente.';
          scheduleNext(Math.min(1200 + attempts * 350, 3000));
          return;
        }}
        if (response.status === 401 || response.status === 403) {{
          fail('Tu sesión no tiene permiso para ver este remito.', 'Inicie sesión nuevamente o solicite permisos de impresión.');
          return;
        }}
        if (response.status === 409) {{
          fail(detail || 'El remito todavía no está emitido o no tiene referencia de comprobante.');
          return;
        }}
        fail(detail || 'No se pudo obtener el PDF del remito.', 'Reintente la impresión. Si el problema persiste, revise el estado de Bejerman.');
        return;
      }} catch (error) {{
        detailEl.textContent = error && error.name === 'AbortError'
          ? 'La consulta del PDF tardó demasiado. Se reintentará automáticamente.'
          : 'No se pudo consultar el PDF en este intento. Se reintentará automáticamente.';
      }}

      scheduleNext(Math.min(900 + attempts * 250, 2000));
    }}

    retryEl.addEventListener('click', () => {{
      attempts = 0;
      void pollPdf();
    }});
    closeEl.addEventListener('click', () => window.close());
    void pollPdf();
  </script>
</body>
</html>"""
