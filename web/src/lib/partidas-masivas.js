// Carga masiva de partidas en un renglón de una orden de entrega: una partida (número de serie) por
// renglón, pegada desde Excel o leída de un .xlsx/.csv.
//
// Como en el alta masiva de equipos, el archivo y lo pegado terminan en el mismo texto, que es lo que
// el operador ve y corrige. Contra el stock valida el backend (`ordenes-entrega/partidas-masivas/
// validar/`); acá sólo se lee la lista y se arma el renglón con lo que la validación dejó pasar.

/** Espejo de PARTIDAS_MASIVAS_MAX_FILAS del backend. */
export const PARTIDAS_MASIVAS_MAX_FILAS = 500;

const HEADER_RE =
  /^(partidas?|series?|serial|ns|sn|[ns]\s*[°º]?\s*\/\s*[sn]|(n[uú]mero|nro\.?|n\s*[°º])\s*(de\s+)?(serie|partida))$/i;
// Excel muestra así los números largos de una columna en formato General ("2,1E+11"). Con el signo
// obligatorio para no confundir una partida real del estilo "21E3".
const SCIENTIFIC_RE = /^\d+(?:[.,]\d+)?e[+-]\d+$/i;

// Excel y las páginas web pegan espacios duros (U+00A0) que conviene tratar como espacios comunes.
const clean = (value) => String(value ?? "").replace(/ /g, " ").trim();

export function looksLikeScientificNotation(value) {
  return SCIENTIFIC_RE.test(clean(value).replace(/\s+/g, ""));
}

// Celdas de un renglón pegado. Copiar varias columnas de Excel deja un tab entre celdas y un CSV pegado
// como texto trae `;`. La coma no separa, porque la partida en notación científica ("2,1E+11") la trae;
// el espacio tampoco: hay series con espacios y "MG 1234" es un número interno que se acepta.
export function splitPartidasMasivasLine(line) {
  const text = clean(line);
  return text ? text.split(/[\t;]/).map(clean) : [];
}

// Filas `{linea, partida}` a partir de celdas. `linea` es el renglón de origen (1 = el primero) y es lo
// que permite sacar después del texto las filas con error. Si el primer renglón con datos es un
// encabezado, la partida se toma de la columna que se llama así (una planilla puede traer artículo,
// descripción y partida); si no, de la primera.
export function partidasMasivasRowsFromCells(rows) {
  const out = [];
  let column = 0;
  let firstRow = true;
  (Array.isArray(rows) ? rows : []).forEach((cells, index) => {
    const values = (Array.isArray(cells) ? cells : [cells]).map(clean);
    if (!values.some(Boolean)) return;
    if (firstRow) {
      firstRow = false;
      const headerColumn = values.findIndex((value) => HEADER_RE.test(value));
      if (headerColumn >= 0) {
        column = headerColumn;
        return;
      }
    }
    const partida = values[column] || "";
    if (partida) out.push({ linea: index + 1, partida });
  });
  return out;
}

export function parsePartidasMasivasText(text) {
  return partidasMasivasRowsFromCells(String(text ?? "").split(/\r?\n/).map(splitPartidasMasivasLine));
}

// Una planilla leída con SheetJS (`sheet_to_json` con `header: 1`) pasa al mismo texto que se pega.
export function partidasMasivasTextFromCells(rows) {
  return partidasMasivasRowsFromCells(rows)
    .map((row) => row.partida)
    .join("\n");
}

export function removePartidasMasivasLines(text, lineNumbers) {
  const drop = new Set((lineNumbers || []).map(Number));
  return String(text ?? "")
    .split(/\r?\n/)
    .filter((_, index) => !drop.has(index + 1))
    .join("\n");
}

export function partidasMasivasPayloadRows(rows) {
  return (rows || []).map((row) => ({ fila: row.linea, partida: row.partida }));
}

function partidaRowsOf(item) {
  if (Array.isArray(item?.partidas) && item.partidas.length) return item.partidas;
  return clean(item?.partida) ? [item] : [];
}

/** Las partidas que la orden ya tiene para el artículo del renglón, con su renglón (1 = el primero).
 * Mismo criterio que `deliveryOrderUsedPartidaKeys`: mismo artículo y, si se sabe, mismo depósito. El
 * backend no repite las del propio renglón y rechaza las de otro, porque el guardado sumaría las dos
 * contra el mismo stock. */
export function partidasMasivasCargadas(items, itemIndex, depositCode = "") {
  const list = Array.isArray(items) ? items : [];
  const article = clean(list[itemIndex]?.articleCode).toLowerCase();
  if (!article) return [];
  const deposit = clean(depositCode).toUpperCase();
  const out = [];
  list.forEach((item, index) => {
    if (clean(item?.articleCode).toLowerCase() !== article) return;
    partidaRowsOf(item).forEach((row) => {
      const partida = clean(row?.partida);
      if (!partida) return;
      const rowDeposit = clean(row?.stockDepositCode || item?.stockDepositCode).toUpperCase();
      if (deposit && rowDeposit && rowDeposit !== deposit) return;
      out.push({ partida, renglon: index + 1, actual: index === itemIndex });
    });
  });
  return out;
}

function quantityNumber(value) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function hasPartidaData(row) {
  return Boolean(clean(row?.partida) || clean(row?.partidaExpirationDate) || clean(row?.stockAvailableQuantity));
}

function activePartidas(item) {
  return (Array.isArray(item?.partidas) ? item.partidas : []).filter(hasPartidaData);
}

/** Las filas que una validación deja cargar: las `ok` que no estaban ya en el renglón. */
export function partidasMasivasParaCargar(filas) {
  return (Array.isArray(filas) ? filas : []).filter(
    (fila) => fila?.estado === "ok" && fila?.accion === "cargar" && clean(fila?.lote?.partida)
  );
}

/** El renglón con las partidas validadas agregadas: una unidad por partida, con el depósito y el stock
 * que informó la validación. Las que ya estaban no se repiten y la fila vacía de "Partida / serie" se
 * descarta. Con `adjustQuantity` la cantidad del renglón pasa a ser la suma de sus partidas. */
export function applyPartidasMasivas(item, filas, { adjustQuantity = false } = {}) {
  const existing = activePartidas(item);
  const seen = new Set(existing.map((row) => clean(row.partida).toLowerCase()));
  const added = [];
  partidasMasivasParaCargar(filas).forEach((fila) => {
    const partida = clean(fila.lote.partida);
    const key = partida.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    added.push({
      ingresoId: "",
      deviceId: "",
      partida,
      assignedQuantity: "1",
      partidaExpirationDate: clean(fila.lote.partidaExpirationDate),
      stockDepositCode: clean(fila.lote.stockDepositCode).toUpperCase(),
      stockAvailableQuantity: fila.lote.stockAvailableQuantity == null ? "" : String(fila.lote.stockAvailableQuantity),
      stockCheckedAt: clean(fila.lote.stockCheckedAt),
    });
  });
  if (!added.length) return item;
  const partidas = [...existing, ...added];
  const assigned = partidas.reduce((sum, row) => sum + quantityNumber(row.assignedQuantity), 0);
  return {
    ...item,
    ...(adjustQuantity ? { quantity: String(Math.round(assigned * 10000) / 10000) } : {}),
    partidas,
  };
}

/** Lo que resultaría de cargar la validación en el renglón, sin cargarla: cuántas partidas se agregan
 * y cuánto suma el renglón después. */
export function partidasMasivasResultado(item, filas) {
  const next = applyPartidasMasivas(item, filas);
  const sum = (rows) => rows.reduce((total, row) => total + quantityNumber(row.assignedQuantity), 0);
  return {
    added: activePartidas(next).length - activePartidas(item).length,
    assigned: sum(activePartidas(next)),
  };
}
