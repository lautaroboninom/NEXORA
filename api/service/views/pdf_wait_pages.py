from __future__ import annotations

import json
from html import escape


def _js(value) -> str:
    """Literal JS/JSON seguro dentro de un `<script>`.

    `json.dumps` solo no alcanza: un `</script>` adentro de la cadena cierra la
    etiqueta antes de que el JS llegue a parsearse. `<\\/` sigue siendo JSON
    válido y el parser de HTML ya no ve el cierre.
    """

    return json.dumps(value).replace("</", "<\\/")


# Misma paleta y medidas que el modal de progreso de NEXORA (Tailwind sky/gray)
# y que la pestaña que reserva el front en `web/src/lib/pdf-wait-page.js`: el
# operador ve la misma cara venga por donde venga la impresión.
_WAIT_PAGE_CSS = """
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #030712; background: #f3f4f6; }
    main { width: min(480px, 100%); background: #fff; border: 1px solid #bae6fd; border-radius: 8px;
      padding: 20px; box-shadow: 0 20px 25px -5px rgba(2, 6, 23, .12), 0 8px 10px -6px rgba(2, 6, 23, .08); }
    .fila { display: flex; align-items: flex-start; gap: 16px; }
    .cuerpo { min-width: 0; flex: 1 1 auto; }
    .icono { flex: 0 0 auto; width: 44px; height: 44px; border-radius: 999px; display: grid; place-items: center;
      background: #f0f9ff; color: #0369a1; }
    .icono.error { background: #fef2f2; color: #b91c1c; }
    .marca { font-size: 22px; line-height: 1; }
    .spinner { width: 24px; height: 24px; border: 2px solid #bae6fd; border-top-color: #0369a1;
      border-radius: 999px; animation: girar .8s linear infinite; }
    h1 { margin: 0; font-size: 16px; font-weight: 600; color: #030712; }
    .linea-estado { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-top: 4px; }
    #estado { margin: 0; min-width: 0; font-size: 14px; font-weight: 500; color: #075985; }
    #estado.error { color: #b91c1c; }
    .reloj { flex: 0 0 auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
      font-variant-numeric: tabular-nums; color: #6b7280; }
    .detalle { margin: 8px 0 0; font-size: 14px; line-height: 1.5; color: #4b5563; }
    .pasos { list-style: none; margin: 16px 0 0; padding: 12px 0 0; border-top: 1px solid #f3f4f6;
      display: grid; gap: 10px; }
    .pasos li { display: flex; align-items: flex-start; gap: 8px; font-size: 13px; color: #9ca3af; }
    .pasos .texto { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .pasos .etapa { font-size: 12px; font-weight: 400; color: #6b7280; }
    .pasos .etapa[hidden] { display: none; }
    .punto { flex: 0 0 auto; width: 14px; height: 14px; margin-top: 2px; border-radius: 999px;
      border: 2px solid #e5e7eb; background: #fff; }
    .pasos li.activo { color: #075985; font-weight: 500; }
    .pasos li.activo .punto { border-color: #0284c7; background: #e0f2fe; }
    .pasos li.hecho { color: #4b5563; }
    .pasos li.hecho .punto { border-color: #10b981; background: #10b981; }
    .acciones { display: none; flex-wrap: wrap; gap: 8px; justify-content: flex-end; margin-top: 16px; }
    .acciones.visible { display: flex; }
    button, .acciones a { border: 1px solid #d1d5db; border-radius: 6px; background: #fff; color: #374151;
      padding: 6px 12px; font: inherit; font-size: 13px; font-weight: 500; text-decoration: none; cursor: pointer; }
    button:hover, .acciones a:hover { background: #f9fafb; }
    #retry { border-color: #0284c7; background: #0284c7; color: #fff; }
    #retry:hover { background: #0369a1; }
    @keyframes girar { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .spinner { animation-duration: 2.4s; } }
    .visor { position: fixed; inset: 0; z-index: 9999; display: grid; grid-template-rows: auto 1fr; background: #f3f4f6; }
    .visor[hidden] { display: none; }
    .visor header { display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 8px 12px; background: #fff; border-bottom: 1px solid #e5e7eb; }
    .visor strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
    .visor .barra { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .visor iframe { width: 100%; height: 100%; border: 0; background: #fff; }
    @media print {
      .visor { position: static; display: block; }
      .visor header { display: none; }
      main { display: none; }
    }
"""


# Sin interpolación: todo lo variable entra por el `<script type="application/json">`.
_WAIT_PAGE_SCRIPT = """
    var config = JSON.parse(document.getElementById('nexora-espera').textContent || '{}');
    const pdfUrl = String(config.pdfUrl || '');
    const documentName = String(config.documentName || 'remito');
    const fallbackDetail = String(config.fallbackDetail || '');
    const inicio = Date.now();
    const iconoEl = document.getElementById('icono');
    const spinnerEl = document.getElementById('spinner');
    const marcaEl = document.getElementById('marca');
    const statusEl = document.getElementById('status');
    const detailEl = document.getElementById('detail');
    const relojEl = document.getElementById('reloj');
    const pasosEl = document.getElementById('pasos');
    const actionsEl = document.getElementById('actions');
    const retryEl = document.getElementById('retry');
    const openPdfEl = document.getElementById('open-pdf');
    const closeEl = document.getElementById('close');
    const visorEl = document.getElementById('visor');
    const visorLabelEl = document.getElementById('visor-label');
    const printEl = document.getElementById('print');
    const closeVisorEl = document.getElementById('close-visor');
    let attempts = 0;
    let pdfBlobUrl = '';
    const REMITO_DOCUMENT_NOT_FOUND = 'REMITO_DOCUMENT_NOT_FOUND';
    // Le da al visor del navegador tiempo de pintar el PDF antes de pedirle el
    // diálogo: es el mismo margen que usa `openPrintablePdf` en el front.
    const PRINT_DELAY_MS = 700;

    function pintarReloj() {
      const total = Math.max(0, Math.floor((Date.now() - inicio) / 1000));
      const segundos = String(total % 60).padStart(2, '0');
      relojEl.textContent = Math.floor(total / 60) + ':' + segundos;
    }
    pintarReloj();
    window.setInterval(pintarReloj, 1000);

    // Los pasos son el "qué está pasando detrás": el rótulo grande dice el
    // estado y `etapa` trae la etapa exacta que reporta el backend.
    function marcarPaso(id, etapa) {
      const items = pasosEl.children;
      let objetivo = -1;
      for (let i = 0; i < items.length; i += 1) {
        if (items[i].getAttribute('data-paso') === id) objetivo = i;
      }
      if (objetivo < 0) return;
      for (let i = 0; i < items.length; i += 1) {
        const item = items[i];
        item.className = i < objetivo ? 'hecho' : (i === objetivo ? 'activo' : '');
        const etapaEl = item.querySelector('.etapa');
        if (!etapaEl) continue;
        if (i === objetivo && etapa) {
          etapaEl.textContent = etapa;
          etapaEl.hidden = false;
        } else {
          etapaEl.textContent = '';
          etapaEl.hidden = true;
        }
      }
    }

    function informar(status, detail, etapa) {
      statusEl.textContent = status;
      statusEl.classList.remove('error');
      iconoEl.className = 'icono';
      spinnerEl.hidden = false;
      marcaEl.hidden = true;
      if (detail) detailEl.textContent = detail;
      marcaPasoPdf(etapa);
    }

    function marcaPasoPdf(etapa) {
      marcarPaso('pdf', etapa || '');
    }

    function hasOnlyPdfPadding(bytes, end) {
      let index = 0;
      if (end >= 3 && bytes[0] === 239 && bytes[1] === 187 && bytes[2] === 191) {
        index = 3;
      }
      for (; index < end; index += 1) {
        const value = bytes[index];
        if (value !== 9 && value !== 10 && value !== 12 && value !== 13 && value !== 32) return false;
      }
      return true;
    }

    function pdfBytesFromResponse(bytes) {
      const marker = [37, 80, 68, 70, 45];
      const hasMarkerAt = (offset) =>
        bytes.length >= offset + marker.length &&
        marker.every((value, index) => bytes[offset + index] === value);
      if (hasMarkerAt(0)) return bytes;
      const limit = Math.min(Math.max(bytes.length - marker.length, 0), 16);
      for (let offset = 1; offset <= limit; offset += 1) {
        if (hasMarkerAt(offset) && hasOnlyPdfPadding(bytes, offset)) return bytes.subarray(offset);
      }
      return null;
    }

    function showActions(visible) {
      actionsEl.classList.toggle('visible', visible);
    }

    function printPdf() {
      const frame = document.getElementById('pdf-frame');
      if (!frame) return;
      try {
        frame.contentWindow.focus();
        frame.contentWindow.print();
      } catch (error) {
        try { window.print(); } catch (_) {}
      }
    }

    function showPdf(bytes) {
      pdfBlobUrl = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' }));
      openPdfEl.href = pdfBlobUrl;
      openPdfEl.hidden = false;
      retryEl.hidden = true;
      statusEl.textContent = 'El PDF del ' + documentName + ' está listo.';
      detailEl.textContent = 'Abriendo el archivo para imprimir.';
      marcarPaso('print', '');

      // El PDF va en un iframe y NO se navega la pestaña: al reemplazar el
      // documento que creó el blob, el navegador lo revoca y la URL queda en
      // ERR_FILE_NOT_FOUND. Era una carrera, por eso a veces salía bien.
      const visor = document.createElement('iframe');
      visor.id = 'pdf-frame';
      visor.src = pdfBlobUrl;
      visor.title = 'PDF del ' + documentName;
      // El operador ya pidió imprimir cuando abrió esta pestaña: el diálogo sale
      // solo apenas el visor terminó de cargar. El botón queda para reintentar.
      visor.addEventListener('load', () => window.setTimeout(printPdf, PRINT_DELAY_MS));
      visorLabelEl.textContent = 'PDF del ' + documentName;
      visorEl.appendChild(visor);
      visorEl.hidden = false;
      showActions(false);
    }

    function fail(message, detail) {
      statusEl.textContent = message;
      statusEl.classList.add('error');
      iconoEl.className = 'icono error';
      spinnerEl.hidden = true;
      marcaEl.hidden = false;
      detailEl.textContent = detail || fallbackDetail;
      retryEl.hidden = false;
      openPdfEl.hidden = !pdfBlobUrl;
      showActions(true);
    }

    function retryDelayFrom(response, payload) {
      const retryAfterHeader = String(response.headers.get('Retry-After') || '').trim();
      const retryAfterSeconds = Number(retryAfterHeader);
      if (retryAfterSeconds > 0) return retryAfterSeconds * 1000;
      const retryAfterDate = Date.parse(retryAfterHeader);
      if (Number.isFinite(retryAfterDate) && retryAfterDate > Date.now()) return retryAfterDate - Date.now();
      const retryAfterMs = Number(payload && payload.retry_after_ms || 0);
      if (retryAfterMs > 0) return retryAfterMs;
      return Math.min(900 + attempts * 250, 2000);
    }

    function normalizeLookupText(value) {
      return String(value || '')
        .normalize('NFD')
        .replace(/[\\u0300-\\u036f]/g, '')
        .toLowerCase();
    }

    function shouldRetryLookupMiss(response, payload) {
      if (![404, 502, 503, 504].includes(response.status)) return false;
      const text = normalizeLookupText(String((payload && payload.code) || '') + ' ' + String((payload && payload.detail) || ''));
      return text.includes(REMITO_DOCUMENT_NOT_FOUND.toLowerCase())
        || text.includes('no se encontro el comprobante asociado')
        || text.includes('no se encontro el pdf del comprobante')
        || text.includes('no se encontro el archivo pdf')
        || text.includes('no se encontro en bejerman un remito');
    }

    async function readResponsePayload(response) {
      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        return await response.json().catch(() => ({}));
      }
      const text = await response.text().catch(() => '');
      return { detail: text };
    }

    function scheduleNext(delayMs) {
      window.setTimeout(pollPdf, Math.max(1000, Number(delayMs) || 1500));
    }

    async function pollPdf() {
      attempts += 1;
      showActions(false);
      informar(
        attempts === 1
          ? 'Buscando el PDF del ' + documentName
          : 'Esperando el PDF del ' + documentName,
        'NEXORA está comprobando si el archivo ya está disponible.',
        '',
      );

      try {
        const separator = pdfUrl.includes('?') ? '&' : '?';
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 25000);
        const response = await fetch(pdfUrl + separator + 't=' + Date.now(), {
          cache: 'no-store',
          credentials: 'same-origin',
          signal: controller.signal,
        });
        window.clearTimeout(timeout);

        if (response.status === 202) {
          const payload = await readResponsePayload(response);
          const detail = payload && payload.detail ? String(payload.detail) : '';
          // `stage_label` lo manda el backend (STAGE_LABELS): es la diferencia
          // entre "algo pasa" y un spinner que parece colgado.
          const stage = payload && payload.stage_label ? String(payload.stage_label) : '';
          informar(
            'El ' + documentName + ' está emitido. NEXORA está preparando el PDF.',
            detail || 'Esperando la disponibilidad del archivo para imprimir.',
            stage,
          );
          scheduleNext(retryDelayFrom(response, payload));
          return;
        }

        if (response.status === 200) {
          const buffer = await response.arrayBuffer();
          const bytes = pdfBytesFromResponse(new Uint8Array(buffer));
          if (bytes) {
            try {
              showPdf(bytes);
            } catch (error) {
              detailEl.textContent = 'El navegador no pudo abrirlo automáticamente. Use Abrir e imprimir.';
              showActions(true);
            }
            return;
          }
          fail('NEXORA recibió una respuesta que no es un PDF válido.', 'Reintente la impresión desde esta pestaña.');
          return;
        }

        const payload = await readResponsePayload(response);
        const detail = payload && payload.detail ? String(payload.detail) : '';

        if (shouldRetryLookupMiss(response, payload)) {
          informar(
            'NEXORA todavía no encontró el PDF del ' + documentName + '.',
            'NEXORA volverá a comprobarlo automáticamente.',
            'Buscando el comprobante en Bejerman',
          );
          scheduleNext(Math.min(1200 + attempts * 350, 3000));
          return;
        }
        if (response.status === 401 || response.status === 403) {
          fail('Tu sesión no tiene permiso para ver este remito.', 'Inicie sesión nuevamente o solicite permisos de impresión.');
          return;
        }
        if (response.status === 409) {
          fail(detail || 'El remito todavía no está emitido o no tiene referencia de comprobante.');
          return;
        }
        fail(detail || 'No se pudo obtener el PDF del remito.', 'Reintente la impresión. Si el problema persiste, revise el estado de Bejerman.');
        return;
      } catch (error) {
        detailEl.textContent = error && error.name === 'AbortError'
          ? 'La consulta del PDF tardó demasiado. Se reintentará automáticamente.'
          : 'No se pudo consultar el PDF en este intento. Se reintentará automáticamente.';
      }

      scheduleNext(Math.min(900 + attempts * 250, 2000));
    }

    retryEl.addEventListener('click', () => {
      attempts = 0;
      retryEl.hidden = false;
      void pollPdf();
    });
    printEl.addEventListener('click', printPdf);
    closeEl.addEventListener('click', () => window.close());
    closeVisorEl.addEventListener('click', () => window.close());
    void pollPdf();
"""


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
    config = _js(
        {
            "pdfUrl": clean_pdf_url,
            "documentName": clean_document_name,
            "fallbackDetail": clean_fallback_detail,
        }
    )
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(clean_title)}</title>
  <style>{_WAIT_PAGE_CSS}</style>
</head>
<body>
  <main>
    <div class="fila">
      <div class="icono" id="icono">
        <span class="spinner" id="spinner" aria-hidden="true"></span>
        <span class="marca" id="marca" aria-hidden="true" hidden>!</span>
      </div>
      <div class="cuerpo">
        <h1>Preparando el {escape(clean_document_name)}</h1>
        <div class="linea-estado">
          <p id="status" aria-live="polite">Buscando el PDF del {escape(clean_document_name)}</p>
          <span id="reloj" class="reloj">0:00</span>
        </div>
        <p id="detail" class="detalle">El comprobante ya está emitido en Bejerman. Cuando el archivo esté listo se abre y se manda a imprimir solo.</p>
        <ol id="pasos" class="pasos">
          <li data-paso="pdf" class="activo">
            <span class="punto" aria-hidden="true"></span>
            <span class="texto"><span class="rotulo">Buscando el PDF en Bejerman</span><span class="etapa" hidden></span></span>
          </li>
          <li data-paso="print" class="">
            <span class="punto" aria-hidden="true"></span>
            <span class="texto"><span class="rotulo">Abriendo la impresión</span><span class="etapa" hidden></span></span>
          </li>
        </ol>
        <div id="actions" class="acciones">
          <button id="retry" type="button">Volver a comprobar</button>
          <a id="open-pdf" href="#" target="_self" hidden>Abrir e imprimir</a>
          <button id="close" type="button">Cerrar pestaña</button>
        </div>
      </div>
    </div>
  </main>
  <div id="visor" class="visor" hidden>
    <header>
      <strong id="visor-label"></strong>
      <div class="barra">
        <button id="print" type="button">Imprimir</button>
        <button id="close-visor" type="button">Cerrar</button>
      </div>
    </header>
  </div>
  <script id="nexora-espera" type="application/json">{config}</script>
  <script>{_WAIT_PAGE_SCRIPT}</script>
</body>
</html>"""
