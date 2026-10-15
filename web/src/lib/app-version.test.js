import test from "node:test";
import assert from "node:assert/strict";
import { buildFingerprint, isNewBuild } from "./app-version.js";

// Así sale el index.html de un build de producción (recortado).
const indexDe = (js, css) => `<!doctype html>
<html lang="es">
  <head>
    <link rel="manifest" href="/manifest.webmanifest" />
    <link rel="icon" type="image/png" sizes="32x32" href="/icons/logo-app-32.png" />
    <script type="module" crossorigin src="/assets/${js}"></script>
    <link rel="stylesheet" crossorigin href="/assets/${css}">
  </head>
  <body class="bg-gray-50"><div id="root"></div></body>
</html>`;

test("la huella son los archivos con hash, no los íconos ni el manifest", () => {
  assert.equal(
    buildFingerprint(indexDe("index-DSDCAMOY.js", "index-BpB1Ll1O.css")),
    "/assets/index-BpB1Ll1O.css /assets/index-DSDCAMOY.js"
  );
});

test("el mismo build no avisa nada", () => {
  const huella = buildFingerprint(indexDe("index-DSDCAMOY.js", "index-BpB1Ll1O.css"));
  assert.equal(isNewBuild(huella, buildFingerprint(indexDe("index-DSDCAMOY.js", "index-BpB1Ll1O.css"))), false);
});

test("un rebuild cambia la huella aunque cambie sólo el CSS", () => {
  const antes = buildFingerprint(indexDe("index-DSDCAMOY.js", "index-BpB1Ll1O.css"));
  assert.equal(isNewBuild(antes, buildFingerprint(indexDe("index-Xq81LmPo.js", "index-BpB1Ll1O.css"))), true);
  assert.equal(isNewBuild(antes, buildFingerprint(indexDe("index-DSDCAMOY.js", "index-Zz09AaBb.css"))), true);
});

test("el DOM serializa los atributos igual que el index.html", () => {
  // En la pestaña, la huella se toma de `document.head.innerHTML`, que escribe
  // `crossorigin=""` y no cierra el <link>: tiene que dar lo mismo.
  const head = '<script type="module" crossorigin="" src="/assets/index-DSDCAMOY.js"></script>'
    + '<link rel="stylesheet" crossorigin="" href="/assets/index-BpB1Ll1O.css">';
  assert.equal(buildFingerprint(head), buildFingerprint(indexDe("index-DSDCAMOY.js", "index-BpB1Ll1O.css")));
});

test("una respuesta sin archivos de build no se toma como versión nueva", () => {
  const huella = buildFingerprint(indexDe("index-DSDCAMOY.js", "index-BpB1Ll1O.css"));
  assert.equal(buildFingerprint("<html><body>502 Bad Gateway</body></html>"), "");
  assert.equal(isNewBuild(huella, ""), false);
  assert.equal(isNewBuild("", huella), false, "en desarrollo no hay huella propia");
});
