/**
 * La pestaña que espera el PDF para imprimirlo.
 *
 * Se abre dentro del mismo click que pide la impresión (si se abriera después,
 * el navegador la trata como popup y la bloquea), así que nace cuando todavía
 * no hay nada que mostrar. Antes era un spinner mudo con una sola frase fija:
 * acá se arma con la misma cara que el modal de progreso de NEXORA y con una
 * API (`NEXORA_PDF_WAIT.update`) para ir contando en qué etapa real está.
 *
 * Vive aparte de `pdf.js` porque es HTML puro sin dependencias: así se puede
 * testear con `node --test` sin arrastrar `api.js` ni `import.meta.env`.
 */

/** Emisión + PDF + impresión: el camino completo cuando el comprobante todavía no existe. */
export const PDF_WAIT_STEPS = [
  { id: "emit", label: "Emitiendo el comprobante en Bejerman" },
  { id: "pdf", label: "Preparando el PDF" },
  { id: "print", label: "Abriendo la impresión" },
];

/** Reimpresión: el comprobante ya está emitido, sólo falta el archivo. */
export const PDF_REPRINT_STEPS = [
  { id: "pdf", label: "Buscando el PDF en Bejerman" },
  { id: "print", label: "Abriendo la impresión" },
];

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function normalizedSteps(steps) {
  const list = (Array.isArray(steps) ? steps : PDF_WAIT_STEPS)
    .map((step) => ({ id: String(step?.id || "").trim(), label: String(step?.label || "").trim() }))
    .filter((step) => step.id && step.label);
  return list.length ? list : PDF_WAIT_STEPS;
}

function stepsHtml(steps) {
  return steps
    .map(
      (step, index) =>
        `<li data-paso="${escapeHtml(step.id)}" class="${index === 0 ? "activo" : ""}">` +
        '<span class="punto" aria-hidden="true"></span>' +
        '<span class="texto">' +
        `<span class="rotulo">${escapeHtml(step.label)}</span>` +
        '<span class="etapa" hidden></span>' +
        "</span></li>",
    )
    .join("");
}

/**
 * Traduce el progreso de `waitForPdfBlob` a lo que la pestaña sabe mostrar.
 * El `stageLabel` viene del backend (`STAGE_LABELS`) y es la diferencia entre
 * "algo está pasando" y un spinner que parece colgado.
 */
export function waitPageUpdateFrom(progress) {
  const data = progress || {};
  const ready = data.state === "ready";
  return {
    status: data.status || "",
    detail: data.detail || "",
    stage: ready ? "" : String(data.stageLabel || ""),
    step: ready ? "print" : "pdf",
    state: data.state || "",
  };
}

export function buildPdfWaitPageHtml({
  title = "Impresión - NEXORA",
  heading = "Preparando la impresión",
  status = "NEXORA está preparando el PDF.",
  detail = "Esta pestaña muestra el PDF y abre la impresión cuando el archivo esté listo.",
  fallbackMessage = "La operación está tardando más de lo habitual. Puede cerrar esta pestaña y volver a intentar desde NEXORA.",
  fallbackAfterMs = 20000,
  steps = PDF_WAIT_STEPS,
} = {}) {
  const pasos = normalizedSteps(steps);
  const safeFallbackAfterMs = Math.max(5000, Number(fallbackAfterMs) || 20000);
  return `<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${escapeHtml(title)}</title>
  <style>${PDF_WAIT_PAGE_CSS}</style>
</head>
<body>
  <main>
    <div class="fila">
      <div class="icono" id="icono">
        <span class="spinner" id="spinner" aria-hidden="true"></span>
        <span class="tilde" id="tilde" aria-hidden="true" hidden>&#10003;</span>
        <span class="cruz" id="cruz" aria-hidden="true" hidden>&#10007;</span>
      </div>
      <div class="cuerpo">
        <h1 id="titulo">${escapeHtml(heading)}</h1>
        <div class="linea-estado">
          <p id="estado" aria-live="polite">${escapeHtml(status)}</p>
          <span id="reloj" class="reloj">0:00</span>
        </div>
        <p id="detalle" class="detalle">${escapeHtml(detail)}</p>
        <p id="comprobante" class="comprobante" hidden>
          <span aria-hidden="true">&#10003;</span>
          <span><strong id="comprobante-numero"></strong> <span id="comprobante-texto"></span></span>
        </p>
        <ol id="pasos" class="pasos">${stepsHtml(pasos)}</ol>
        <p id="aviso" class="aviso" hidden>${escapeHtml(fallbackMessage)}</p>
        <div id="acciones" class="acciones" hidden>
          <button type="button" id="cerrar">Cerrar pestaña</button>
        </div>
      </div>
    </div>
  </main>
  <script>
    (function () {
      var inicio = Date.now();
      var icono = document.getElementById('icono');
      var spinner = document.getElementById('spinner');
      var tilde = document.getElementById('tilde');
      var cruz = document.getElementById('cruz');
      var tituloEl = document.getElementById('titulo');
      var estadoEl = document.getElementById('estado');
      var detalleEl = document.getElementById('detalle');
      var relojEl = document.getElementById('reloj');
      var comprobanteEl = document.getElementById('comprobante');
      var comprobanteNumeroEl = document.getElementById('comprobante-numero');
      var comprobanteTextoEl = document.getElementById('comprobante-texto');
      var pasosEl = document.getElementById('pasos');
      var avisoEl = document.getElementById('aviso');
      var accionesEl = document.getElementById('acciones');
      var cerrarEl = document.getElementById('cerrar');

      function pintarReloj() {
        var total = Math.max(0, Math.floor((Date.now() - inicio) / 1000));
        var segundos = String(total % 60);
        if (segundos.length < 2) segundos = '0' + segundos;
        relojEl.textContent = Math.floor(total / 60) + ':' + segundos;
      }
      pintarReloj();
      window.setInterval(pintarReloj, 1000);

      window.setTimeout(function () {
        // Nada de esto cancela la espera: el aviso sólo existe para que el
        // operador sepa que puede irse de la pestaña sin perder el remito.
        avisoEl.hidden = false;
        accionesEl.hidden = false;
      }, ${safeFallbackAfterMs});

      cerrarEl.addEventListener('click', function () { window.close(); });

      function pasoIndice(id) {
        var items = pasosEl.children;
        for (var i = 0; i < items.length; i += 1) {
          if (items[i].getAttribute('data-paso') === id) return i;
        }
        return -1;
      }

      function pasoActivo() {
        var items = pasosEl.children;
        for (var i = 0; i < items.length; i += 1) {
          if (items[i].className === 'activo') return items[i].getAttribute('data-paso');
        }
        return '';
      }

      function marcarPaso(id, etapa) {
        var objetivo = pasoIndice(id);
        if (objetivo < 0) return;
        var items = pasosEl.children;
        for (var i = 0; i < items.length; i += 1) {
          var item = items[i];
          item.className = i < objetivo ? 'hecho' : (i === objetivo ? 'activo' : '');
          var etapaEl = item.querySelector('.etapa');
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

      function terminar() {
        var items = pasosEl.children;
        for (var i = 0; i < items.length; i += 1) items[i].className = 'hecho';
        icono.className = 'icono listo';
        spinner.hidden = true;
        tilde.hidden = false;
        cruz.hidden = true;
        avisoEl.hidden = true;
        accionesEl.hidden = true;
      }

      // La pestaña no se cierra sola cuando algo falla: el operador que la estaba
      // mirando tiene que leer acá el mismo motivo que ve NEXORA.
      function fallar() {
        var items = pasosEl.children;
        for (var i = 0; i < items.length; i += 1) {
          if (items[i].className === 'activo') items[i].className = 'fallo';
        }
        icono.className = 'icono fallo';
        spinner.hidden = true;
        tilde.hidden = true;
        cruz.hidden = false;
        avisoEl.hidden = true;
        accionesEl.hidden = false;
      }

      window.NEXORA_PDF_WAIT = {
        update: function (patch) {
          var data = patch || {};
          if (data.heading) tituloEl.textContent = String(data.heading);
          if (data.status) estadoEl.textContent = String(data.status);
          if (data.detail) detalleEl.textContent = String(data.detail);
          if (data.documentLabel) {
            comprobanteNumeroEl.textContent = String(data.documentLabel);
            comprobanteTextoEl.textContent = data.documentDetail
              ? String(data.documentDetail)
              : 'ya emitido en Bejerman. NEXORA está preparando el PDF para imprimir.';
            comprobanteEl.hidden = false;
          }
          // Sin paso ni etapa no se toca la lista: un cambio de estado suelto no
          // tiene por qué borrar la etapa que ya se estaba mostrando.
          if (data.step || data.stage) {
            var paso = data.step ? String(data.step) : pasoActivo();
            if (paso) marcarPaso(paso, data.stage ? String(data.stage) : '');
          }
          if (data.state === 'ready') terminar();
          if (data.state === 'error') fallar();
        },
      };
    })();
  </script>
</body>
</html>`;
}

/** Paleta y medidas copiadas del modal de progreso de NEXORA (Tailwind sky/gray/emerald). */
const PDF_WAIT_PAGE_CSS = `
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
    .icono.listo { background: #ecfdf5; color: #047857; }
    .icono.fallo { background: #fef2f2; color: #b91c1c; }
    .tilde { font-size: 22px; line-height: 1; }
    .cruz { font-size: 22px; line-height: 1; }
    .spinner { width: 24px; height: 24px; border: 2px solid #bae6fd; border-top-color: #0369a1;
      border-radius: 999px; animation: girar .8s linear infinite; }
    h1 { margin: 0; font-size: 16px; font-weight: 600; color: #030712; }
    .linea-estado { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-top: 4px; }
    #estado { margin: 0; min-width: 0; font-size: 14px; font-weight: 500; color: #075985; }
    .reloj { flex: 0 0 auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
      font-variant-numeric: tabular-nums; color: #6b7280; }
    .detalle { margin: 8px 0 0; font-size: 14px; line-height: 1.5; color: #4b5563; }
    .comprobante { display: flex; align-items: flex-start; gap: 6px; margin: 12px 0 0; padding: 8px 10px;
      border: 1px solid #a7f3d0; background: #ecfdf5; border-radius: 6px; font-size: 14px; line-height: 1.4; color: #065f46; }
    .comprobante[hidden] { display: none; }
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
    .pasos li.fallo { color: #b91c1c; font-weight: 500; }
    .pasos li.fallo .punto { border-color: #b91c1c; background: #fee2e2; }
    .aviso { margin: 12px 0 0; padding: 8px 10px; border: 1px solid #fde68a; background: #fffbeb;
      border-radius: 6px; font-size: 13px; line-height: 1.45; color: #92400e; }
    .aviso[hidden] { display: none; }
    .acciones { display: flex; justify-content: flex-end; margin-top: 16px; }
    .acciones[hidden] { display: none; }
    button { border: 1px solid #d1d5db; border-radius: 6px; background: #fff; color: #374151;
      padding: 6px 12px; font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; }
    button:hover { background: #f9fafb; }
    @keyframes girar { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .spinner { animation-duration: 2.4s; } }
`;
