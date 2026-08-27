import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";

import {
  buildPdfWaitPageHtml,
  PDF_REPRINT_STEPS,
  PDF_WAIT_STEPS,
  waitPageUpdateFrom,
} from "./pdf-wait-page.js";

function scriptOf(html) {
  const match = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(match, "la página tiene que traer su script");
  return match[1];
}

test("arranca con el primer paso activo y el resto pendiente", () => {
  const html = buildPdfWaitPageHtml({});
  assert.match(html, /<li data-paso="emit" class="activo">/);
  assert.match(html, /<li data-paso="pdf" class="">/);
  assert.match(html, /<li data-paso="print" class="">/);
  for (const step of PDF_WAIT_STEPS) {
    assert.ok(html.includes(step.label), `falta el paso ${step.id}`);
  }
});

test("la reimpresión no promete una emisión que no va a pasar", () => {
  const html = buildPdfWaitPageHtml({ steps: PDF_REPRINT_STEPS });
  assert.ok(!html.includes('data-paso="emit"'));
  assert.match(html, /<li data-paso="pdf" class="activo">/);
});

test("el script expone la API que usa NEXORA para contar en qué anda", () => {
  // Sin esto la pestaña vuelve a ser un spinner mudo: el opener no tiene por
  // dónde empujarle el estado real.
  const code = scriptOf(buildPdfWaitPageHtml({}));
  assert.match(code, /window\.NEXORA_PDF_WAIT = \{/);
  assert.match(code, /update: function \(patch\)/);
  new vm.Script(code); // compila, no ejecuta: acá no hay document
});

test("cada elemento que el script busca existe en el marcado", () => {
  // Un id mal escrito revienta el script entero y la pestaña queda muda, que es
  // justo el síntoma que esta página vino a arreglar.
  const html = buildPdfWaitPageHtml({});
  const marcado = html.replace(/<script>[\s\S]*?<\/script>/, "");
  for (const [, id] of scriptOf(html).matchAll(/getElementById\('([^']+)'\)/g)) {
    assert.ok(marcado.includes(`id="${id}"`), `falta el elemento ${id}`);
  }
});

test("muestra el comprobante emitido y el reloj de espera", () => {
  const html = buildPdfWaitPageHtml({});
  assert.match(html, /id="comprobante-numero"/);
  assert.ok(html.includes("ya emitido en Bejerman"));
  assert.match(html, /id="reloj"/);
});

test("los textos viajan escapados", () => {
  const html = buildPdfWaitPageHtml({
    title: '</script><b>x</b>',
    heading: 'Emitiendo "RT"',
    status: "<img src=x>",
  });
  assert.ok(!html.includes("</script><b>"));
  assert.ok(!html.includes("<img src=x>"));
  assert.ok(html.includes("&lt;img src=x&gt;"));
});

test("traduce el progreso de la espera a pasos de la pestaña", () => {
  assert.deepEqual(
    waitPageUpdateFrom({
      state: "pending",
      status: "remito RSS emitido. Preparando el PDF.",
      detail: "NEXORA vuelve a comprobarlo automáticamente.",
      stageLabel: "Recuperando el PDF de respaldo desde Bejerman",
    }),
    {
      status: "remito RSS emitido. Preparando el PDF.",
      detail: "NEXORA vuelve a comprobarlo automáticamente.",
      stage: "Recuperando el PDF de respaldo desde Bejerman",
      step: "pdf",
      state: "pending",
    },
  );

  const listo = waitPageUpdateFrom({ state: "ready", status: "PDF listo.", detail: "Abriendo impresión." });
  assert.equal(listo.step, "print");
  assert.equal(listo.stage, "");
});
