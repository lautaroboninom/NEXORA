// Lista de un alta masiva de equipos propios: número de serie y MG por renglón.
//
// Llega de dos lados —pegada desde Excel o leída de un .xlsx/.csv— y los dos terminan en el mismo
// texto, que es lo que el operador ve y corrige. Acá no se valida contra la base ni contra
// Bejerman: eso lo hace el backend, que es el que sabe.

const SERIE_HEADER_RE =
  /^(n\s*[°º]?\s*\/?\s*s|n\s*[°º]\s*(de\s+)?serie|n[uú]mero\s+de\s+serie|nro\.?\s+(de\s+)?serie|serie|serial|partida)$/i;
const MG_HEADER_RE = /^(mg|n[uú]mero\s+interno|nro\.?\s+interno|n\s*[°º]\s*interno|interno)$/i;
// Excel muestra así los números largos de una columna en formato General ("2,1E+11"). Con el
// signo obligatorio para no confundir una serie real del estilo "21E3".
const SCIENTIFIC_RE = /^\d+(?:[.,]\d+)?e[+-]\d+$/i;

// Excel y las páginas web pegan espacios duros (U+00A0) que conviene tratar como espacios comunes.
const clean = (value) => String(value ?? "").replace(/ /g, " ").trim();

export function looksLikeScientificNotation(value) {
  return SCIENTIFIC_RE.test(clean(value).replace(/\s+/g, ""));
}

// Separa un renglón pegado. Copiar dos columnas de Excel deja un tab entre celdas; un CSV trae
// `;` o `,`. El tab va primero porque la serie en notación científica ("2,1E+11") trae una coma.
// Sin separador se toma la primera palabra como serie y el resto como MG, así "E21BE708153 MG 9525"
// también funciona.
export function splitAltaMasivaLine(line) {
  const text = clean(line);
  if (!text) return [];
  for (const separator of ["\t", ";", ","]) {
    if (text.includes(separator)) return text.split(separator).map(clean);
  }
  const [serie, ...resto] = text.split(/\s+/);
  return [serie, resto.join(" ")];
}

function isHeaderRow(values) {
  return values.length >= 2 && SERIE_HEADER_RE.test(values[0]) && MG_HEADER_RE.test(values[1]);
}

// Filas `{linea, numero_serie, numero_interno}` a partir de celdas. `linea` es el renglón de origen
// (1 = el primero) y es lo que permite sacar después del texto las filas con error. Las columnas se
// respetan por posición: una fila sin serie no corre el MG a la columna de la serie.
export function altaMasivaRowsFromCells(rows) {
  const out = [];
  let firstRow = true;
  (Array.isArray(rows) ? rows : []).forEach((cells, index) => {
    const values = (Array.isArray(cells) ? cells : []).map(clean);
    if (!values.some(Boolean)) return;
    const isFirst = firstRow;
    firstRow = false;
    if (isFirst && isHeaderRow(values)) return;
    out.push({ linea: index + 1, numero_serie: values[0] || "", numero_interno: values[1] || "" });
  });
  return out;
}

export function parseAltaMasivaText(text) {
  return altaMasivaRowsFromCells(String(text ?? "").split(/\r?\n/).map(splitAltaMasivaLine));
}

// Una planilla leída con SheetJS (`sheet_to_json` con `header: 1`) pasa al mismo texto que se pega.
export function altaMasivaTextFromCells(rows) {
  return altaMasivaRowsFromCells(rows)
    .map((row) => `${row.numero_serie}\t${row.numero_interno}`)
    .join("\n");
}

export function removeAltaMasivaLines(text, lineNumbers) {
  const drop = new Set((lineNumbers || []).map(Number));
  return String(text ?? "")
    .split(/\r?\n/)
    .filter((_, index) => !drop.has(index + 1))
    .join("\n");
}

export function altaMasivaPayloadRows(rows) {
  return (rows || []).map((row) => ({
    fila: row.linea,
    numero_serie: row.numero_serie,
    numero_interno: row.numero_interno,
  }));
}
