import { useMemo, useState } from "react";
import { bejermanLookupErrorMessage, postDeliveryOrderPartidasMasivasValidar } from "../lib/api";
import {
  PARTIDAS_MASIVAS_MAX_FILAS,
  looksLikeScientificNotation,
  parsePartidasMasivasText,
  partidasMasivasCargadas,
  partidasMasivasPayloadRows,
  partidasMasivasResultado,
  partidasMasivasTextFromCells,
  removePartidasMasivasLines,
} from "../lib/partidas-masivas.js";

// Carga masiva de partidas de un renglón de la orden de entrega: la columna de partidas pegada desde
// Excel o subida en una planilla, validada contra el stock antes de cargarla.
//
// Dos pasos, como el alta masiva de equipos: "Validar" no toca el renglón y muestra qué pasa con cada
// fila; "Cargar" agrega las que pasaron, y sólo si la validación es de la lista que está en pantalla.
// Nada se graba hasta guardar la orden, que vuelve a validar cada partida contra Bejerman.

const QUANTITY_TOLERANCE = 0.0001;

function plural(count, singular, pluralForm) {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function formatNumber(value) {
  return Number.isInteger(value) ? String(value) : value.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

function errorMessage(error, fallback) {
  const detail = error?.data?.detail;
  return typeof detail === "string" && detail ? detail : bejermanLookupErrorMessage(error, fallback);
}

export default function DeliveryOrderPartidasMasivas({
  items,
  itemIndex,
  deliveryType,
  companyKey,
  depositCode,
  orderId = "",
  canAdjustQuantity = true,
  disabled = false,
  onApply,
  onClose,
}) {
  const isRental = deliveryType === "rental";
  const one = isRental ? "NS" : "partida";
  const many = isRental ? "NS" : "partidas";
  const theMany = isRental ? "los NS" : "las partidas";
  const item = items?.[itemIndex] || {};
  const articleCode = String(item.articleCode || "").trim();
  const [texto, setTexto] = useState("");
  const [validacion, setValidacion] = useState(null);
  const [validadoKey, setValidadoKey] = useState("");
  const [validando, setValidando] = useState(false);
  const [importando, setImportando] = useState(false);
  const [error, setError] = useState("");
  const [mensajeCarga, setMensajeCarga] = useState("");
  const [ajustarCantidad, setAjustarCantidad] = useState(true);

  const filas = useMemo(() => parsePartidasMasivasText(texto), [texto]);
  const cargadas = useMemo(
    () => partidasMasivasCargadas(items, itemIndex, depositCode),
    [items, itemIndex, depositCode]
  );
  const payload = useMemo(
    () => ({
      deliveryType,
      companyKey,
      depositCode,
      articleCode,
      orderId: orderId || null,
      filas: partidasMasivasPayloadRows(filas),
      cargadas,
    }),
    [deliveryType, companyKey, depositCode, articleCode, orderId, filas, cargadas]
  );
  const payloadKey = useMemo(() => JSON.stringify(payload), [payload]);
  const validacionVigente = Boolean(validacion) && validadoKey === payloadKey;
  const resumen = validacionVigente ? validacion.resumen : null;
  const resultado = useMemo(
    () => (validacionVigente ? partidasMasivasResultado(item, validacion.filas) : null),
    [validacionVigente, item, validacion]
  );
  // Primero lo que hay que mirar: con cien filas, las tres con error no pueden quedar en el medio.
  const filasOrdenadas = useMemo(() => {
    if (!validacionVigente) return [];
    const rank = (fila) => (fila.errores?.length ? 0 : fila.avisos?.length ? 1 : 2);
    return [...(validacion.filas || [])].sort((a, b) => rank(a) - rank(b) || a.fila - b.fila);
  }, [validacionVigente, validacion]);

  const cantidad = (() => {
    const parsed = Number.parseFloat(String(item.quantity ?? "").replace(",", "."));
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  })();
  const cientificas = filas.filter((fila) => looksLikeScientificNotation(fila.partida)).length;
  const sumaDistinta = Boolean(resultado) && Math.abs(resultado.assigned - cantidad) > QUANTITY_TOLERANCE;
  // Al cargar partidas de una orden ya creada la cantidad del renglón es fija: la lista no puede pasarse.
  const excedeCantidadFija = !canAdjustQuantity && Boolean(resultado) && resultado.assigned - cantidad > QUANTITY_TOLERANCE;
  const ajustar = canAdjustQuantity && ajustarCantidad && sumaDistinta;

  const puedeValidar =
    Boolean(articleCode) && filas.length > 0 && filas.length <= PARTIDAS_MASIVAS_MAX_FILAS && !validando && !disabled;
  const puedeCargar =
    validacionVigente && Boolean(resultado) && resultado.added > 0 && !excedeCantidadFija && !validando && !disabled;

  const validar = async () => {
    if (!puedeValidar) return;
    const key = payloadKey;
    try {
      setValidando(true);
      setError("");
      setMensajeCarga("");
      const data = await postDeliveryOrderPartidasMasivasValidar(payload);
      setValidacion(data || null);
      setValidadoKey(key);
    } catch (e) {
      setValidacion(null);
      setValidadoKey("");
      setError(errorMessage(e, `No se pudieron validar las ${many}`));
    } finally {
      setValidando(false);
    }
  };

  const cargar = () => {
    if (!puedeCargar) return;
    const lineasCargadas = (validacion.filas || []).filter((fila) => fila.estado === "ok").map((fila) => fila.fila);
    const conError = resumen?.con_error || 0;
    onApply?.(validacion.filas, { adjustQuantity: ajustar });
    // En la lista quedan sólo las que no entraron, para corregirlas y volver a validar.
    setTexto((prev) => removePartidasMasivasLines(prev, lineasCargadas));
    setValidacion(null);
    setValidadoKey("");
    setMensajeCarga(
      `${resultado.added === 1 ? "Se cargó" : "Se cargaron"} ${plural(resultado.added, one, many)} en el renglón.` +
        (conError ? ` En la lista ${conError === 1 ? "quedó la que tiene" : `quedaron las ${conError} que tienen`} error.` : "")
    );
  };

  const quitarFilasConError = () => {
    if (!validacionVigente) return;
    const lineas = (validacion.filas || []).filter((fila) => fila.errores?.length).map((fila) => fila.fila);
    setTexto((prev) => removePartidasMasivasLines(prev, lineas));
  };

  const importarArchivo = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      setImportando(true);
      setError("");
      setMensajeCarga("");
      const XLSX = await import("xlsx");
      // `raw: true` deja el CSV como texto: si no, una partida "00123" pierde los ceros al leerse.
      const workbook = XLSX.read(await file.arrayBuffer(), { type: "array", raw: true });
      const sheet = workbook.Sheets[workbook.SheetNames[0]];
      const cells = sheet ? XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: "" }) : [];
      const text = partidasMasivasTextFromCells(cells);
      if (!text) {
        setError(`El archivo no tiene ${many}.`);
        return;
      }
      setTexto(text);
    } catch (e) {
      setError(`No se pudo leer el archivo: ${e?.message || e}`);
    } finally {
      setImportando(false);
    }
  };

  const descargarPlantilla = async () => {
    try {
      const XLSX = await import("xlsx");
      const sheet = XLSX.utils.aoa_to_sheet([[isRental ? "NS" : "Partida"]]);
      // Columna en formato Texto: con formato General Excel se come los ceros a la izquierda y pasa
      // las series largas a notación científica.
      for (let row = 2; row <= PARTIDAS_MASIVAS_MAX_FILAS + 1; row += 1) {
        sheet[`A${row}`] = { t: "s", v: "", z: "@" };
      }
      sheet["!ref"] = `A1:A${PARTIDAS_MASIVAS_MAX_FILAS + 1}`;
      sheet["!cols"] = [{ wch: 28 }];
      const workbook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(workbook, sheet, isRental ? "NS" : "Partidas");
      XLSX.writeFile(workbook, isRental ? "carga-masiva-ns.xlsx" : "carga-masiva-partidas.xlsx");
    } catch (e) {
      setError(`No se pudo generar la plantilla: ${e?.message || e}`);
    }
  };

  return (
    <div className="mt-2 rounded border border-sky-200 bg-sky-50 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-xs font-medium uppercase text-sky-900">Carga masiva de {many}</div>
          <div className="mt-0.5 text-xs text-gray-600">
            {isRental ? "Pegá un NS por renglón (también sirve el MG)" : "Pegá una partida por renglón"} —una columna de
            Excel— o subí la planilla. Se validan contra el stock{articleCode ? ` de ${articleCode}` : ""} en{" "}
            {depositCode} antes de {isRental ? "cargarlos" : "cargarlas"} en el renglón.
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-3 text-xs">
          <label
            className={`cursor-pointer text-blue-700 underline ${importando || disabled ? "pointer-events-none opacity-50" : ""}`}
          >
            {importando ? "Leyendo archivo…" : "Subir Excel o CSV"}
            <input
              type="file"
              accept=".xlsx,.xls,.csv,text/csv"
              className="hidden"
              onChange={importarArchivo}
              disabled={importando || disabled}
            />
          </label>
          <button type="button" className="text-blue-700 underline" onClick={descargarPlantilla}>
            Descargar plantilla
          </button>
          <button type="button" className="text-gray-500 hover:text-gray-900" onClick={onClose} disabled={validando}>
            Cerrar
          </button>
        </div>
      </div>

      {!articleCode && (
        <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          Elegí primero el artículo del renglón: {theMany} se validan contra su stock.
        </div>
      )}
      {error && <div className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}
      {mensajeCarga && (
        <div className="mt-2 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          {mensajeCarga}
        </div>
      )}

      <textarea
        className="mt-2 w-full rounded border bg-white px-3 py-2 font-mono text-sm disabled:bg-gray-100"
        rows={6}
        value={texto}
        onChange={(event) => {
          setTexto(event.target.value);
          setMensajeCarga("");
        }}
        placeholder={isRental ? "E21BE708153\nE21BE708154\nMG 9525" : "E238D501001\nE238D501002\nE238D501003"}
        aria-label={`Lista de ${many} para cargar`}
        disabled={disabled}
        spellCheck={false}
      />
      <div className="mt-1 text-xs text-gray-500">
        {plural(filas.length, one, many)} · máximo {PARTIDAS_MASIVAS_MAX_FILAS} por carga. Cada {one} se carga como una
        unidad.
      </div>
      {filas.length > PARTIDAS_MASIVAS_MAX_FILAS && (
        <div className="mt-1 text-xs text-red-700">
          La lista pasa el máximo de {PARTIDAS_MASIVAS_MAX_FILAS}: dividila en varias cargas.
        </div>
      )}
      {cientificas > 0 && (
        <div className="mt-1 text-xs text-amber-800">
          {plural(cientificas, "valor parece convertido", "valores parecen convertidos")} por Excel a notación
          científica (por ejemplo 2,1E+11): esas ya no son las originales.
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={validar}
          disabled={!puedeValidar}
          className="rounded border border-blue-600 bg-white px-3 py-1.5 text-sm text-blue-700 hover:bg-blue-50 disabled:opacity-50"
        >
          {validando ? "Validando…" : filas.length ? `Validar ${plural(filas.length, one, many)}` : "Validar"}
        </button>
        {validacion && !validacionVigente && !validando && (
          <span className="text-xs text-amber-800">
            La lista, el artículo o el depósito cambiaron desde la última validación: volvé a validar.
          </span>
        )}
      </div>

      {validacionVigente && resumen && (
        <div className="mt-3">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm">
            <div>
              <span className="font-medium text-emerald-700">{resumen.ok} OK</span>
              {resumen.con_error > 0 && <span className="font-medium text-red-700"> · {resumen.con_error} con error</span>}
              {resumen.ya_cargadas > 0 && (
                <span className="text-gray-600">
                  {" "}
                  · {plural(resumen.ya_cargadas, "ya estaba cargada", "ya estaban cargadas")}
                </span>
              )}
              <span className="text-gray-600"> · depósito {validacion.depositCode || depositCode}</span>
            </div>
            {resumen.con_error > 0 && (
              <button type="button" className="text-sm text-blue-700 underline" onClick={quitarFilasConError}>
                {resumen.con_error === 1 ? "Quitar la fila con error" : `Quitar las ${resumen.con_error} filas con error`}
              </button>
            )}
          </div>
          <div className="max-h-72 overflow-auto rounded border bg-white">
            <table className="min-w-full text-sm">
              <thead className="sticky top-0 bg-gray-50 text-left text-xs text-gray-600">
                <tr>
                  <th className="px-2 py-1">Fila</th>
                  <th className="px-2 py-1">{isRental ? "NS" : "Partida"}</th>
                  <th className="px-2 py-1">Stock</th>
                  <th className="px-2 py-1">Estado</th>
                </tr>
              </thead>
              <tbody>
                {filasOrdenadas.map((fila) => {
                  const lote = fila.lote;
                  const partidaBejerman = String(lote?.partida || "");
                  const otraForma = partidaBejerman && partidaBejerman.toLowerCase() !== String(fila.partida || "").toLowerCase();
                  return (
                    <tr key={`${fila.fila}-${fila.partida}`} className={`border-t align-top ${fila.errores?.length ? "bg-red-50" : ""}`}>
                      <td className="px-2 py-1 text-gray-500">{fila.fila}</td>
                      <td className="px-2 py-1 font-mono">
                        {fila.partida || "-"}
                        {otraForma && <div className="text-xs text-gray-500">→ {partidaBejerman}</div>}
                      </td>
                      <td className="whitespace-nowrap px-2 py-1 text-xs text-gray-600">
                        {lote ? (
                          <>
                            {lote.stockDepositCode} · {formatNumber(Number(lote.stockAvailableQuantity) || 0)}
                            {lote.partidaExpirationDate ? <div>Vto. {lote.partidaExpirationDate}</div> : null}
                            {lote.equipmentInternalNumber ? <div>{lote.equipmentInternalNumber}</div> : null}
                          </>
                        ) : (
                          "-"
                        )}
                      </td>
                      <td className="px-2 py-1">
                        {fila.errores?.length ? (
                          fila.errores.map((mensaje, i) => (
                            <div key={`e${i}`} className="text-red-700">
                              {mensaje}
                            </div>
                          ))
                        ) : fila.accion === "ya_cargada" ? (
                          <div className="text-gray-600">Ya cargada</div>
                        ) : (
                          <div className="text-emerald-700">OK</div>
                        )}
                        {(fila.avisos || []).map((mensaje, i) => (
                          <div key={`a${i}`} className="text-xs text-amber-800">
                            {mensaje}
                          </div>
                        ))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {resultado && resultado.added > 0 && (
            <div className="mt-2 space-y-1 text-xs">
              {canAdjustQuantity && sumaDistinta && (
                <label className="flex items-center gap-2 text-gray-700">
                  <input
                    type="checkbox"
                    checked={ajustarCantidad}
                    onChange={(event) => setAjustarCantidad(event.target.checked)}
                    disabled={disabled}
                  />
                  Ajustar la cantidad del renglón a {formatNumber(resultado.assigned)} (hoy {formatNumber(cantidad)})
                </label>
              )}
              {canAdjustQuantity && sumaDistinta && !ajustarCantidad && (
                <div className="text-gray-600">
                  La cantidad queda en {formatNumber(cantidad)}: hasta que coincida con la suma de las {many} la orden no se
                  puede guardar.
                </div>
              )}
              {excedeCantidadFija && (
                <div className="text-red-700">
                  El renglón pide {formatNumber(cantidad)} y con esta lista sumaría {formatNumber(resultado.assigned)}: sacá{" "}
                  {formatNumber(resultado.assigned - cantidad)} de la lista.
                </div>
              )}
              {!canAdjustQuantity && !excedeCantidadFija && sumaDistinta && (
                <div className="text-gray-600">
                  El renglón pide {formatNumber(cantidad)}: quedarían {formatNumber(cantidad - resultado.assigned)} pendientes.
                </div>
              )}
            </div>
          )}
          {resultado && resultado.added === 0 && (
            <div className="mt-2 text-xs text-gray-600">{isRental ? "No hay NS nuevos" : "No hay partidas nuevas"} para cargar en el renglón.</div>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={cargar}
              disabled={!puedeCargar}
              className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {resultado?.added ? `Cargar ${plural(resultado.added, one, many)}` : "Cargar"}
            </button>
            {resumen.con_error > 0 && resultado?.added > 0 && (
              <span className="text-xs text-gray-600">
                {resumen.con_error === 1 ? "La fila con error no se carga." : `Las ${resumen.con_error} filas con error no se cargan.`}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
