import { useEffect, useMemo, useState } from "react";
import {
  bejermanLookupErrorMessage,
  getDeviceAltaMasivaConfig,
  getMarcas,
  getMarcasPorTipo,
  getModelosByBrand,
  getTiposEquipo,
  getVariantesPorModelo,
  postDeviceAltaMasiva,
  postDeviceAltaMasivaValidar,
} from "../lib/api";
import {
  altaMasivaPayloadRows,
  altaMasivaTextFromCells,
  looksLikeScientificNotation,
  parseAltaMasivaText,
  removeAltaMasivaLines,
} from "../lib/alta-masiva.js";

// Alta masiva de equipos propios: un tipo, una marca y un modelo, y la lista de series con su MG.
// Es el alta con "Equipo propio" del modal de Equipos para muchas unidades: quedan a nombre de
// MG BIO en la Estantería de Alquiler, y las que están en VAL pasan a STL en un solo lote.
//
// Dos pasos a propósito: "Validar" no graba nada y muestra qué pasa con cada fila; "Dar de alta" se
// habilita recién cuando la lista validada es exactamente la que está en pantalla.

function errorMessage(error, fallback) {
  if (error?.data?.conflict_type === "BEJERMAN_UNAVAILABLE") {
    return bejermanLookupErrorMessage(error, fallback);
  }
  return error?.data?.detail || error?.message || fallback;
}

function plural(count, singular, pluralForm) {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

export default function AltaMasivaEquiposModal({ onClose, onDone }) {
  const [config, setConfig] = useState(null);
  const [empresa, setEmpresa] = useState("SEPID");
  const [deposito, setDeposito] = useState("VAL");
  const [tiposEquipo, setTiposEquipo] = useState([]);
  const [marcas, setMarcas] = useState([]);
  const [marcasPorTipo, setMarcasPorTipo] = useState([]);
  const [modelos, setModelos] = useState([]);
  const [variantes, setVariantes] = useState([]);
  const [tipo, setTipo] = useState("");
  const [marcaTxt, setMarcaTxt] = useState("");
  const [marcaId, setMarcaId] = useState(null);
  const [modeloId, setModeloId] = useState("");
  const [variante, setVariante] = useState("");
  const [texto, setTexto] = useState("");
  const [validacion, setValidacion] = useState(null);
  const [validadoKey, setValidadoKey] = useState("");
  const [validando, setValidando] = useState(false);
  const [guardando, setGuardando] = useState(false);
  const [importando, setImportando] = useState(false);
  const [error, setError] = useState("");
  const [resultado, setResultado] = useState(null);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [cfg, marcasRows, tiposRows] = await Promise.all([
          getDeviceAltaMasivaConfig(),
          getMarcas(),
          getTiposEquipo(),
        ]);
        if (!active) return;
        setConfig(cfg || null);
        if (cfg?.deposito_origen) setDeposito(cfg.deposito_origen);
        setMarcas(Array.isArray(marcasRows) ? marcasRows : []);
        const tipos = (Array.isArray(tiposRows) ? tiposRows : [])
          .map((t) => t?.nombre || t?.label || t?.name || t?.value || t)
          .map((value) => String(value).trim())
          .filter(Boolean);
        setTiposEquipo(Array.from(new Set(tipos)));
      } catch (e) {
        if (active) setError(errorMessage(e, "No se pudieron cargar los catálogos."));
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  // Cascada tipo → marca → modelo → variante, como en el alta de a uno: cambiar un nivel limpia
  // los de abajo.
  useEffect(() => {
    let active = true;
    setMarcaTxt("");
    setMarcaId(null);
    setModelos([]);
    setModeloId("");
    setVariante("");
    setVariantes([]);
    if (!tipo) {
      setMarcasPorTipo([]);
      return () => {
        active = false;
      };
    }
    getMarcasPorTipo(tipo)
      .then((rows) => {
        if (active) setMarcasPorTipo(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (active) setMarcasPorTipo([]);
      });
    return () => {
      active = false;
    };
  }, [tipo]);

  useEffect(() => {
    let active = true;
    setModelos([]);
    setModeloId("");
    setVariante("");
    setVariantes([]);
    if (!marcaId) {
      return () => {
        active = false;
      };
    }
    getModelosByBrand(marcaId)
      .then((rows) => {
        if (!active) return;
        const norm = (value) => String(value || "").trim().toUpperCase();
        const list = Array.isArray(rows) ? rows : [];
        setModelos(tipo ? list.filter((m) => norm(m?.tipo_equipo) === norm(tipo)) : list);
      })
      .catch((e) => {
        if (active) setError(errorMessage(e, "No se pudieron cargar los modelos."));
      });
    return () => {
      active = false;
    };
  }, [marcaId, tipo]);

  useEffect(() => {
    let active = true;
    setVariantes([]);
    if (!modeloId) {
      return () => {
        active = false;
      };
    }
    getVariantesPorModelo(modeloId)
      .then((rows) => {
        if (!active) return;
        const seen = new Set();
        const names = (Array.isArray(rows) ? rows : [])
          .filter((v) => v?.active !== false)
          .map((v) => String(v?.name || v?.nombre || v?.label || "").trim())
          .filter((name) => {
            const key = name.toUpperCase();
            if (!name || seen.has(key)) return false;
            seen.add(key);
            return true;
          });
        setVariantes(names);
      })
      .catch(() => {
        if (active) setVariantes([]);
      });
    return () => {
      active = false;
    };
  }, [modeloId]);

  const marcasPool = tipo && marcasPorTipo.length ? marcasPorTipo : marcas;
  const onMarcaInput = (value) => {
    setMarcaTxt(value);
    const match = (marcasPool || []).find(
      (m) => String(m?.nombre || "").toLowerCase() === String(value || "").trim().toLowerCase()
    );
    setMarcaId(match ? Number(match.id) : null);
  };

  const filas = useMemo(() => parseAltaMasivaText(texto), [texto]);
  const maxFilas = Number(config?.max_filas) || 100;
  const origen = deposito || "VAL";
  const destino = config?.deposito_destino || "STL";
  const payload = useMemo(
    () => ({
      empresa,
      stock_deposito: deposito,
      tipo_equipo: tipo,
      marca_id: marcaId,
      model_id: modeloId ? Number(modeloId) : null,
      variante: variante.trim(),
      filas: altaMasivaPayloadRows(filas),
    }),
    [empresa, deposito, tipo, marcaId, modeloId, variante, filas]
  );
  const payloadKey = useMemo(() => JSON.stringify(payload), [payload]);
  const validacionVigente = !!validacion && validadoKey === payloadKey;
  const resumen = validacionVigente ? validacion.resumen : null;
  const cientificas = filas.filter((fila) => looksLikeScientificNotation(fila.numero_serie)).length;
  const puedeValidar =
    !!(tipo && marcaId && modeloId) &&
    filas.length > 0 &&
    filas.length <= maxFilas &&
    !validando &&
    !guardando;
  const puedeConfirmar =
    validacionVigente && !!resumen && resumen.total > 0 && resumen.con_error === 0 && !validando && !guardando;

  const validar = async () => {
    if (!puedeValidar) return;
    const key = payloadKey;
    try {
      setValidando(true);
      setError("");
      const data = await postDeviceAltaMasivaValidar(payload);
      setValidacion(data || null);
      setValidadoKey(key);
    } catch (e) {
      setValidacion(null);
      setValidadoKey("");
      setError(errorMessage(e, "No se pudo validar la lista."));
    } finally {
      setValidando(false);
    }
  };

  const confirmar = async () => {
    if (!puedeConfirmar) return;
    const key = payloadKey;
    try {
      setGuardando(true);
      setError("");
      const data = await postDeviceAltaMasiva(payload);
      setResultado(data || { ok: true });
    } catch (e) {
      // Si al confirmar apareció un error nuevo (otro usuario cargó una de las series), el backend
      // devuelve la validación otra vez: se muestra en la tabla y no sólo como un mensaje suelto.
      if (Array.isArray(e?.data?.filas)) {
        setValidacion(e.data);
        setValidadoKey(key);
      }
      setError(errorMessage(e, "No se pudo dar de alta la lista."));
    } finally {
      setGuardando(false);
    }
  };

  const quitarFilasConError = () => {
    if (!validacionVigente) return;
    const lineas = (validacion.filas || [])
      .filter((fila) => (fila.errores || []).length)
      .map((fila) => fila.fila);
    setTexto((prev) => removeAltaMasivaLines(prev, lineas));
  };

  const importarArchivo = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      setImportando(true);
      setError("");
      const XLSX = await import("xlsx");
      // `raw: true` deja el CSV como texto: si no, una serie "00123" pierde los ceros al leerse.
      const workbook = XLSX.read(await file.arrayBuffer(), { type: "array", raw: true });
      const sheet = workbook.Sheets[workbook.SheetNames[0]];
      const cells = sheet ? XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: "" }) : [];
      const text = altaMasivaTextFromCells(cells);
      if (!text) {
        setError("El archivo no tiene filas con número de serie y MG.");
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
      const sheet = XLSX.utils.aoa_to_sheet([["N/S", "MG"]]);
      // Columnas en formato Texto: con formato General Excel se come los ceros a la izquierda y
      // pasa las series largas a notación científica.
      for (let row = 2; row <= maxFilas + 1; row += 1) {
        sheet[`A${row}`] = { t: "s", v: "", z: "@" };
        sheet[`B${row}`] = { t: "s", v: "", z: "@" };
      }
      sheet["!ref"] = `A1:B${maxFilas + 1}`;
      sheet["!cols"] = [{ wch: 24 }, { wch: 14 }];
      const workbook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(workbook, sheet, "Equipos");
      XLSX.writeFile(workbook, "alta-masiva-equipos-propios.xlsx");
    } catch (e) {
      setError(`No se pudo generar la plantilla: ${e?.message || e}`);
    }
  };

  const cerrar = () => {
    if (guardando) return;
    if (resultado) onDone?.(resultado);
    else onClose?.();
  };

  const stockLabel = (fila) => {
    if (fila.accion === "transferir") return `${origen} → ${destino}`;
    if (fila.accion === "ya_en_destino") return `Ya en ${destino}`;
    return "-";
  };

  const lote = resultado?.stock_lote || null;

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded shadow-lg w-full max-w-3xl max-h-full flex flex-col">
        <div className="px-4 pt-4 pb-3 border-b">
          <div className="text-lg font-semibold mb-1">Alta masiva de equipos propios</div>
          <div className="text-sm text-gray-600">
            Quedan a nombre de MG BIO, en la Estantería de Alquiler. Las unidades que están en {origen}{" "}
            pasan a {destino} en Bejerman en un solo lote.
          </div>
          {error && (
            <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mt-3 text-sm">{error}</div>
          )}
        </div>

        {resultado ? (
          <div className="min-h-0 overflow-y-auto p-4 space-y-3 text-sm">
            <div className="rounded border border-emerald-300 bg-emerald-50 p-3 text-emerald-900">
              <div className="font-medium">
                {plural(Number(resultado.creados) || 0, "equipo dado de alta", "equipos dados de alta")}.
              </div>
              <div className="mt-1">
                {Number(resultado.transferir) > 0
                  ? `${plural(Number(resultado.transferir), "unidad pasa", "unidades pasan")} de ${
                      lote?.source_deposit || origen
                    } a ${lote?.target_deposit || destino} en un solo lote (${lote?.reference || "-"}).`
                  : "No hay stock para mover."}
                {Number(resultado.ya_en_destino) > 0
                  ? ` ${plural(Number(resultado.ya_en_destino), "ya estaba", "ya estaban")} en ${destino}.`
                  : ""}
              </div>
            </div>
            <p className="text-gray-600">
              La transferencia corre en segundo plano y se sigue en el panel de Bejerman. Los equipos figuran
              disponibles para alquiler recién cuando termina.
            </p>
          </div>
        ) : (
          <div className="min-h-0 overflow-y-auto p-4 space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="block">
                <div className="text-sm text-gray-700 mb-1">Empresa</div>
                <select
                  className="border rounded p-2 w-full"
                  value={empresa}
                  onChange={(e) => setEmpresa(e.target.value)}
                  disabled={guardando}
                >
                  {(config?.empresas?.length ? config.empresas : [{ key: "SEPID", label: "SEPID" }]).map((c) => (
                    <option key={c.key} value={c.key}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block">
                <div className="text-sm text-gray-700 mb-1">Depósito de origen</div>
                <select
                  className="border rounded p-2 w-full"
                  value={deposito}
                  onChange={(e) => setDeposito(e.target.value)}
                  disabled={guardando}
                >
                  {(config?.depositos?.length ? config.depositos : ["VAL"]).map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block md:col-span-2">
                <div className="text-sm text-gray-700 mb-1">Tipo de equipo *</div>
                <select
                  className="border rounded p-2 w-full"
                  value={tipo}
                  onChange={(e) => setTipo(e.target.value)}
                  disabled={guardando}
                >
                  <option value="">-- Seleccionar --</option>
                  {tiposEquipo.map((t, i) => (
                    <option key={`${t}-${i}`} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block">
                <div className="text-sm text-gray-700 mb-1">Marca *</div>
                <input
                  list="alta-masiva-marcas"
                  className="border rounded p-2 w-full"
                  value={marcaTxt}
                  onChange={(e) => onMarcaInput(e.target.value)}
                  placeholder="Marca"
                  disabled={guardando}
                />
                <datalist id="alta-masiva-marcas">
                  {(marcasPool || []).map((m) => (
                    <option key={m.id} value={m.nombre} />
                  ))}
                </datalist>
                {marcaTxt && !marcaId && (
                  <div className="text-xs text-red-600 mt-1">Elegí una marca de las sugeridas.</div>
                )}
              </label>

              <label className="block">
                <div className="text-sm text-gray-700 mb-1">Modelo *</div>
                <select
                  className="border rounded p-2 w-full"
                  value={modeloId}
                  onChange={(e) => setModeloId(e.target.value)}
                  disabled={!marcaId || !modelos.length || guardando}
                >
                  <option value="">{marcaId ? "Seleccionar modelo" : "Elegí la marca primero"}</option>
                  {modelos.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.nombre}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block md:col-span-2">
                <div className="text-sm text-gray-700 mb-1">Variante / detalle</div>
                <input
                  list="alta-masiva-variantes"
                  className="border rounded p-2 w-full"
                  value={variante}
                  onChange={(e) => setVariante(e.target.value)}
                  disabled={guardando}
                />
                <datalist id="alta-masiva-variantes">
                  {variantes.map((v, i) => (
                    <option key={`${v}-${i}`} value={v} />
                  ))}
                </datalist>
              </label>
            </div>

            <div>
              <div className="flex flex-wrap items-end justify-between gap-2 mb-1">
                <div className="text-sm text-gray-700">Números de serie y MG *</div>
                <div className="flex items-center gap-3 text-sm">
                  <label
                    className={`text-blue-700 underline cursor-pointer ${
                      importando || guardando ? "opacity-50 pointer-events-none" : ""
                    }`}
                  >
                    {importando ? "Leyendo archivo…" : "Subir Excel o CSV"}
                    <input
                      type="file"
                      accept=".xlsx,.xls,.csv,text/csv"
                      className="hidden"
                      onChange={importarArchivo}
                      disabled={importando || guardando}
                    />
                  </label>
                  <button type="button" className="text-blue-700 underline" onClick={descargarPlantilla}>
                    Descargar plantilla
                  </button>
                </div>
              </div>
              <textarea
                className="border rounded p-2 w-full font-mono text-sm"
                rows={8}
                value={texto}
                onChange={(e) => setTexto(e.target.value)}
                placeholder={"Pegá dos columnas desde Excel: N/S y MG\nE21BE708153    MG 9525\nE21BE708154    MG 9526"}
                disabled={guardando}
                spellCheck={false}
              />
              <div className="text-xs text-gray-500 mt-1">
                {plural(filas.length, "fila", "filas")} · máximo {maxFilas} por alta. Una fila por equipo: la serie
                y su MG, separados por tab, punto y coma o coma.
              </div>
              {filas.length > maxFilas && (
                <div className="text-xs text-red-600 mt-1">
                  La lista pasa el máximo de {maxFilas} equipos: dividila en varias altas.
                </div>
              )}
              {cientificas > 0 && (
                <div className="text-xs text-amber-700 mt-1">
                  {plural(cientificas, "serie parece convertida", "series parecen convertidas")} por Excel a
                  notación científica (por ejemplo 2,1E+11): esas series ya no son las originales.
                </div>
              )}
            </div>

            {validacion && !validacionVigente && (
              <div className="text-xs text-amber-700">
                La lista o el modelo cambiaron desde la última validación: volvé a validar.
              </div>
            )}

            {validacionVigente && resumen && (
              <div>
                <div className="flex flex-wrap items-center justify-between gap-2 mb-2 text-sm">
                  <div>
                    <span className="font-medium text-emerald-700">{plural(resumen.ok, "lista", "listas")}</span>
                    {resumen.con_error > 0 && (
                      <span className="font-medium text-red-700"> · {resumen.con_error} con error</span>
                    )}
                    <span className="text-gray-600">
                      {" "}
                      · {resumen.transferir} de {origen} a {destino}
                    </span>
                    {resumen.ya_en_destino > 0 && (
                      <span className="text-gray-600">
                        {" "}
                        · {resumen.ya_en_destino} ya en {destino}
                      </span>
                    )}
                  </div>
                  {resumen.con_error > 0 && (
                    <button type="button" className="text-sm text-blue-700 underline" onClick={quitarFilasConError}>
                      Quitar {plural(resumen.con_error, "la fila con error", "las filas con error")}
                    </button>
                  )}
                </div>
                <div className="border rounded overflow-x-auto">
                  <table className="min-w-full text-sm">
                    <thead className="bg-gray-50 text-left text-xs text-gray-600">
                      <tr>
                        <th className="px-2 py-1">Fila</th>
                        <th className="px-2 py-1">N/S</th>
                        <th className="px-2 py-1">MG</th>
                        <th className="px-2 py-1">Stock</th>
                        <th className="px-2 py-1">Estado</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(validacion.filas || []).map((fila) => (
                        <tr
                          key={`${fila.fila}-${fila.numero_serie}`}
                          className={`border-t align-top ${fila.errores?.length ? "bg-red-50" : ""}`}
                        >
                          <td className="px-2 py-1 text-gray-500">{fila.fila}</td>
                          <td className="px-2 py-1 font-mono">{fila.numero_serie || "-"}</td>
                          <td className="px-2 py-1 whitespace-nowrap">{fila.numero_interno || "-"}</td>
                          <td className="px-2 py-1 whitespace-nowrap">
                            {stockLabel(fila)}
                            {fila.article_code ? (
                              <div className="text-xs text-gray-500">Art. {fila.article_code}</div>
                            ) : null}
                          </td>
                          <td className="px-2 py-1">
                            {fila.errores?.length ? (
                              fila.errores.map((mensaje, i) => (
                                <div key={`e${i}`} className="text-red-700">
                                  {mensaje}
                                </div>
                              ))
                            ) : (
                              <div className="text-emerald-700">OK</div>
                            )}
                            {(fila.avisos || []).map((mensaje, i) => (
                              <div key={`a${i}`} className="text-xs text-amber-700">
                                {mensaje}
                              </div>
                            ))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center justify-end gap-2 px-4 py-3 border-t">
          {resultado ? (
            <button className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700" onClick={cerrar}>
              Cerrar
            </button>
          ) : (
            <>
              <button className="px-3 py-1.5 rounded border" onClick={cerrar} disabled={guardando}>
                Cancelar
              </button>
              <button
                className="px-3 py-1.5 rounded border border-blue-600 text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                onClick={validar}
                disabled={!puedeValidar}
              >
                {validando ? "Validando…" : "Validar"}
              </button>
              <button
                className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                onClick={confirmar}
                disabled={!puedeConfirmar}
              >
                {guardando
                  ? "Dando de alta…"
                  : puedeConfirmar
                    ? `Dar de alta ${plural(resumen.ok, "equipo", "equipos")}`
                    : "Dar de alta"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
