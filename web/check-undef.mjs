// no-undef casero: `web/` no tiene ESLint y vite compila igual con identificadores que no
// existen, asi que un `setFoo` huerfano solo explota cuando el usuario toca la pantalla.
// Uso: node check-undef.mjs [archivo...]   (sin argumentos recorre todo web/src)
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { parse } from "@babel/parser";
import _traverse from "@babel/traverse";

const traverse = _traverse.default || _traverse;

const BROWSER_GLOBALS = new Set([
  "window", "document", "navigator", "location", "history", "screen", "console", "fetch",
  "URL", "URLSearchParams", "FormData", "Blob", "File", "FileReader", "Image", "Audio",
  "AbortController", "Headers", "Request", "Response", "WebSocket", "EventSource",
  "localStorage", "sessionStorage", "indexedDB", "caches", "crypto", "performance",
  "setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
  "cancelAnimationFrame", "queueMicrotask", "structuredClone", "atob", "btoa",
  "Uint8Array", "Int8Array", "Uint16Array", "Uint32Array", "Float32Array", "Float64Array",
  "ArrayBuffer", "DataView", "TextEncoder", "TextDecoder", "Intl", "process", "globalThis",
  "HTMLElement", "HTMLInputElement", "HTMLTextAreaElement", "HTMLSelectElement",
  "HTMLCanvasElement", "HTMLImageElement", "Node", "Event", "CustomEvent", "MouseEvent",
  "KeyboardEvent", "MutationObserver", "ResizeObserver", "IntersectionObserver",
  "matchMedia", "alert", "confirm", "prompt", "print", "open", "close", "self",
  "ServiceWorkerRegistration", "Notification", "BroadcastChannel", "MediaRecorder",
  "getComputedStyle", "scrollTo", "requestIdleCallback", "DOMParser", "XMLHttpRequest",
  // Built-ins del lenguaje: no los aporta ningun import.
  "Object", "Array", "String", "Number", "Boolean", "Math", "JSON", "Date", "RegExp",
  "Map", "Set", "WeakMap", "WeakSet", "Promise", "Symbol", "Error", "TypeError",
  "Proxy", "Reflect", "BigInt", "Infinity", "NaN", "undefined", "isNaN", "parseInt",
  "parseFloat", "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
]);

function jsxFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...jsxFiles(full));
    else if (/\.(jsx?|mjs)$/.test(entry)) out.push(full);
  }
  return out;
}

const args = process.argv.slice(2);
const files = args.length ? args : jsxFiles(join(process.cwd(), "src"));
let problemas = 0;

for (const file of files) {
  const code = readFileSync(file, "utf8");
  let ast;
  try {
    ast = parse(code, { sourceType: "module", plugins: ["jsx"] });
  } catch (err) {
    console.log(`PARSE ERROR ${relative(process.cwd(), file)}: ${err.message}`);
    problemas += 1;
    continue;
  }
  traverse(ast, {
    Program(path) {
      for (const name of Object.keys(path.scope.globals)) {
        if (BROWSER_GLOBALS.has(name)) continue;
        problemas += 1;
        console.log(`${relative(process.cwd(), file)}: ${name} no está definido`);
      }
    },
  });
}

console.log(problemas ? `\n${problemas} problema(s)` : `\nSin identificadores sueltos (${files.length} archivos)`);
