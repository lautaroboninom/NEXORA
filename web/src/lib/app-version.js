// web/src/lib/app-version.js
//
// Aviso de versión nueva para las pestañas que quedaron abiertas durante un rebuild.
//
// Cada build publica su JS y su CSS con un hash en el nombre (`/assets/index-XXXX.js`)
// y el index.html los nombra. Si los que nombra el index.html del servidor no son los
// que está usando esta pestaña, hubo un rebuild después de que se abrió.

// Cada cuánto se fija una pestaña que se queda abierta y a la vista. La consulta es el
// index.html: un archivo de 1 KB que sirve nginx sin pasar por la API.
export const VERSION_CHECK_MS = 10 * 60 * 1000;

// Volver a la pestaña también dispara la consulta, pero no más de una vez por minuto.
export const VERSION_CHECK_MIN_GAP_MS = 60 * 1000;

const ASSET_REF_RE = /(?:src|href)="(\/assets\/[^"]+)"/g;

/**
 * La huella de un build: los archivos de `/assets/` que nombra un HTML, ordenados.
 * Devuelve "" si no nombra ninguno (una página de error, por ejemplo), y eso se
 * trata como "no sé", nunca como versión nueva.
 */
export function buildFingerprint(html) {
  const archivos = new Set();
  for (const match of String(html || "").matchAll(ASSET_REF_RE)) archivos.add(match[1]);
  return [...archivos].sort().join(" ");
}

/** Hay versión nueva sólo si se conocen las dos huellas y son distintas. */
export function isNewBuild(runningFingerprint, servedFingerprint) {
  return Boolean(runningFingerprint && servedFingerprint && runningFingerprint !== servedFingerprint);
}
