import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  ClipboardList,
  PackagePlus,
  RefreshCw,
  Save,
  Search,
  Send,
  Trash2,
  XCircle,
} from "lucide-react";
import {
  deleteBejermanPurchaseEntry,
  deleteBejermanPurchaseEntryLine,
  deleteBejermanPurchaseEntryScan,
  getBejermanPurchaseArticles,
  getBejermanPurchaseEntries,
  getBejermanPurchaseEntry,
  getBejermanPurchaseHistory,
  getBejermanPurchaseProviders,
  patchBejermanPurchaseEntry,
  patchBejermanPurchaseEntryLine,
  patchBejermanPurchaseEntryScan,
  postBejermanPurchaseEntry,
  postBejermanPurchaseEntryEmit,
  postBejermanPurchaseEntryLine,
  postBejermanPurchaseEntryScan,
  postBejermanPurchaseEntryValidate,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import {
  DesktopTableWrap,
  MobileDataCard,
  MobileDataList,
  ResponsiveActionBar,
  fullWidthButtonClass,
} from "../components/Responsive.jsx";

const STATUS_LABELS = {
  draft: "Borrador",
  validated: "Validado",
  running: "Emitiendo",
  generated: "Generado",
  failed: "Fallido",
  cancelled: "Cancelado",
};

const STATUS_CLASS = {
  draft: "border-slate-200 bg-slate-50 text-slate-700",
  validated: "border-emerald-200 bg-emerald-50 text-emerald-700",
  running: "border-blue-200 bg-blue-50 text-blue-700",
  generated: "border-emerald-300 bg-emerald-100 text-emerald-800",
  failed: "border-rose-200 bg-rose-50 text-rose-700",
  cancelled: "border-gray-200 bg-gray-50 text-gray-600",
};

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function money(value) {
  const number = Number(value || 0);
  return number.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function quantity(value) {
  const number = Number(value || 0);
  return number.toLocaleString("es-AR", { maximumFractionDigits: 4 });
}

function errText(error) {
  return error?.data?.detail || error?.message || "Error inesperado";
}

function StatusBadge({ status }) {
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs ${STATUS_CLASS[status] || STATUS_CLASS.draft}`}>
      {STATUS_LABELS[status] || status || "Borrador"}
    </span>
  );
}

function flattenScans(entry) {
  return (entry?.lines || []).flatMap((line) =>
    (line.scans || []).map((scan) => ({
      ...scan,
      line,
    }))
  );
}

const emptyHeader = {
  supplierCode: "",
  supplierCodeRaw: "",
  supplierName: "",
  supplierTaxId: "",
  paymentTermCode: "",
  comprobanteLetra: "R",
  comprobantePtoVenta: "",
  comprobanteNumero: "",
  issueDate: todayIso(),
  accountingDate: todayIso(),
  ddjjDate: todayIso(),
  notes: "",
};

function headerFromEntry(entry) {
  if (!entry) return emptyHeader;
  return {
    supplierCode: entry.supplierCode || "",
    supplierCodeRaw: entry.supplierCodeRaw || entry.supplierCode || "",
    supplierName: entry.supplierName || "",
    supplierTaxId: entry.supplierTaxId || "",
    paymentTermCode: entry.paymentTermCode || "",
    comprobanteLetra: entry.comprobanteLetra || "R",
    comprobantePtoVenta: entry.comprobantePtoVenta || "",
    comprobanteNumero: entry.comprobanteNumero || "",
    issueDate: entry.issueDate || todayIso(),
    accountingDate: entry.accountingDate || entry.issueDate || todayIso(),
    ddjjDate: entry.ddjjDate || entry.issueDate || todayIso(),
    notes: entry.notes || "",
  };
}

export default function BejermanPurchaseEntries({ embedded = false } = {}) {
  const { user } = useAuth();
  const canManage = can(user, PERMISSION_CODES.ACTION_BEJERMAN_PURCHASE_ENTRIES_MANAGE);
  const canEmit = can(user, PERMISSION_CODES.ACTION_BEJERMAN_PURCHASE_ENTRIES_EMIT);
  const scannerRef = useRef(null);
  const articleLookupSeqRef = useRef(0);

  const [entries, setEntries] = useState([]);
  const [entry, setEntry] = useState(null);
  const [header, setHeader] = useState(emptyHeader);
  const [activeLineId, setActiveLineId] = useState("");
  const [providerQuery, setProviderQuery] = useState("");
  const [providers, setProviders] = useState([]);
  const [articleQuery, setArticleQuery] = useState("");
  const [articles, setArticles] = useState([]);
  const [articleLoading, setArticleLoading] = useState(false);
  const [articleError, setArticleError] = useState("");
  const [lineForm, setLineForm] = useState({ articleCode: "", articleDescription: "", defaultConversionFactor: "1", defaultUnitValue: "" });
  const [activeDefaults, setActiveDefaults] = useState({ defaultConversionFactor: "1", defaultUnitValue: "" });
  const [scanText, setScanText] = useState("");
  const [manualQuantity, setManualQuantity] = useState("");
  const [scanEdits, setScanEdits] = useState({});
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [validation, setValidation] = useState(null);

  const activeLine = useMemo(
    () => (entry?.lines || []).find((line) => line.id === activeLineId) || (entry?.lines || [])[0] || null,
    [activeLineId, entry]
  );
  const scans = useMemo(() => flattenScans(entry), [entry]);
  const isLocked = ["running", "generated", "cancelled"].includes(entry?.status);

  const focusScanner = useCallback(() => {
    window.setTimeout(() => scannerRef.current?.focus(), 30);
  }, []);

  const loadEntries = useCallback(async () => {
    const data = await getBejermanPurchaseEntries({ limit: 80 });
    setEntries(data.items || []);
    return data.items || [];
  }, []);

  const loadEntry = useCallback(
    async (entryId) => {
      const data = await getBejermanPurchaseEntry(entryId);
      setEntry(data);
      setHeader(headerFromEntry(data));
      setActiveLineId((current) => (data.lines || []).some((line) => line.id === current) ? current : (data.lines || [])[0]?.id || "");
      setValidation(null);
      focusScanner();
      return data;
    },
    [focusScanner]
  );

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        setLoading(true);
        const items = await loadEntries();
        if (alive && items[0]?.id) await loadEntry(items[0].id);
      } catch (exc) {
        if (alive) setError(errText(exc));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [loadEntries, loadEntry]);

  useEffect(() => {
    focusScanner();
  }, [activeLineId, entry?.id, focusScanner]);

  useEffect(() => {
    if (!activeLine) return;
    setActiveDefaults({
      defaultConversionFactor: String(activeLine.defaultConversionFactor ?? "1"),
      defaultUnitValue: String(activeLine.defaultUnitValue ?? ""),
    });
  }, [activeLine?.id, activeLine?.defaultConversionFactor, activeLine?.defaultUnitValue]);

  const loadArticleSuggestions = useCallback(async (query, { limit = 12, showError = false } = {}) => {
    const q = String(query || "").trim();
    if (q.length < 2) {
      articleLookupSeqRef.current += 1;
      setArticles([]);
      setArticleError("");
      setArticleLoading(false);
      return null;
    }
    const seq = articleLookupSeqRef.current + 1;
    articleLookupSeqRef.current = seq;
    setArticleLoading(true);
    setArticleError("");
    try {
      const data = await getBejermanPurchaseArticles({ q, limit });
      if (articleLookupSeqRef.current !== seq) return data;
      setArticles(data.items || []);
      return data;
    } catch (exc) {
      if (articleLookupSeqRef.current !== seq) return null;
      setArticles([]);
      const message = errText(exc);
      if (showError) setError(message);
      else setArticleError(message);
      return null;
    } finally {
      if (articleLookupSeqRef.current === seq) setArticleLoading(false);
    }
  }, []);

  useEffect(() => {
    const q = articleQuery.trim();
    if (q.length < 2 || isLocked) {
      articleLookupSeqRef.current += 1;
      setArticles([]);
      setArticleError("");
      setArticleLoading(false);
      return undefined;
    }
    const handle = window.setTimeout(() => {
      loadArticleSuggestions(q, { limit: 12 });
    }, 250);
    return () => window.clearTimeout(handle);
  }, [articleQuery, isLocked, loadArticleSuggestions]);

  async function run(action, okMessage = "") {
    setError("");
    setMessage("");
    setLoading(true);
    try {
      const result = await action();
      if (okMessage) setMessage(okMessage);
      return result;
    } catch (exc) {
      setError(errText(exc));
      return null;
    } finally {
      setLoading(false);
      focusScanner();
    }
  }

  async function refreshCurrent(nextEntry = null) {
    await loadEntries();
    if (nextEntry?.id) {
      setEntry(nextEntry);
      setHeader(headerFromEntry(nextEntry));
      setActiveLineId((nextEntry.lines || []).at(-1)?.id || activeLineId);
      return nextEntry;
    }
    if (entry?.id) return loadEntry(entry.id);
    return null;
  }

  async function createDraft() {
    const data = await run(
      () => postBejermanPurchaseEntry({ comprobanteLetra: "R", issueDate: todayIso(), accountingDate: todayIso(), ddjjDate: todayIso() }),
      "Borrador creado"
    );
    if (data?.id) {
      await loadEntries();
      await loadEntry(data.id);
    }
  }

  async function saveHeader() {
    if (!entry?.id) return;
    const data = await run(() => patchBejermanPurchaseEntry(entry.id, header), "Encabezado guardado");
    if (data) await refreshCurrent(data);
  }

  async function searchProviders() {
    const data = await run(() => getBejermanPurchaseProviders({ q: providerQuery, limit: 30 }));
    if (data) setProviders(data.items || []);
  }

  async function pickProvider(provider) {
    const next = {
      ...header,
      supplierCode: provider.code || "",
      supplierCodeRaw: provider.rawCode || provider.code || "",
      supplierName: provider.name || "",
      supplierTaxId: provider.cuit || "",
      paymentTermCode: header.paymentTermCode || provider.paymentTermCode || "",
      provider,
    };
    setHeader(next);
    if (entry?.id) {
      const data = await run(() => patchBejermanPurchaseEntry(entry.id, next), "Proveedor asignado");
      if (data) await refreshCurrent(data);
    }
  }

  async function searchArticles() {
    setError("");
    setMessage("");
    await loadArticleSuggestions(articleQuery, { limit: 30, showError: true });
  }

  function pickArticle(article) {
    setLineForm((current) => ({
      ...current,
      articleCode: article.code || article.id || "",
      articleDescription: article.description || article.name || "",
    }));
    setArticles([]);
    setArticleError("");
  }

  async function addLine() {
    if (!entry?.id || !lineForm.articleCode.trim()) return;
    const data = await run(() => postBejermanPurchaseEntryLine(entry.id, lineForm), "Artículo agregado");
    if (data) await refreshCurrent(data);
  }

  async function updateActiveLine() {
    if (!entry?.id || !activeLine?.id) return;
    const data = await run(
      () =>
        patchBejermanPurchaseEntryLine(entry.id, activeLine.id, {
          defaultConversionFactor: activeDefaults.defaultConversionFactor || activeLine.defaultConversionFactor,
          defaultUnitValue: activeDefaults.defaultUnitValue || activeLine.defaultUnitValue,
        }),
      "Valores por defecto actualizados"
    );
    if (data) await refreshCurrent(data);
  }

  async function deleteLine(lineId) {
    if (!entry?.id || !lineId) return;
    const data = await run(() => deleteBejermanPurchaseEntryLine(entry.id, lineId), "Línea eliminada");
    if (data) await refreshCurrent(data);
  }

  async function discardEntry() {
    if (!entry?.id) return;
    const confirmed = window.confirm("Descartar este lote borrador? Se ocultará de la lista activa.");
    if (!confirmed) return;
    const discardedId = entry.id;
    const data = await run(() => deleteBejermanPurchaseEntry(discardedId), "Lote descartado");
    if (!data) return;
    const items = await loadEntries();
    const next = (items || []).find((item) => item.id !== discardedId) || (items || [])[0] || null;
    if (next?.id) {
      await loadEntry(next.id);
    } else {
      setEntry(null);
      setHeader(emptyHeader);
      setActiveLineId("");
      setValidation(null);
    }
  }

  async function submitScan(text) {
    const value = String(text || "").trim();
    if (!entry?.id || !activeLine?.id || !value) return;
    const payload = value.includes("\n") || value.includes("\t") ? { barcodes: value } : { barcode: value };
    const data = await run(() => postBejermanPurchaseEntryScan(entry.id, activeLine.id, payload), "Escaneo cargado");
    setScanText("");
    if (data) await refreshCurrent(data);
  }

  async function addManualQuantity() {
    if (!entry?.id || !activeLine?.id || !manualQuantity) return;
    const data = await run(
      () => postBejermanPurchaseEntryScan(entry.id, activeLine.id, { quantity: manualQuantity }),
      "Cantidad manual cargada"
    );
    setManualQuantity("");
    if (data) await refreshCurrent(data);
  }

  async function saveScan(scan) {
    if (!entry?.id || !scan?.id) return;
    const edit = scanEdits[scan.id] || {};
    const data = await run(() => patchBejermanPurchaseEntryScan(entry.id, scan.id, edit), "Bulto actualizado");
    if (data) await refreshCurrent(data);
  }

  async function deleteScan(scanId) {
    if (!entry?.id || !scanId) return;
    const data = await run(() => deleteBejermanPurchaseEntryScan(entry.id, scanId), "Bulto eliminado");
    if (data) await refreshCurrent(data);
  }

  async function validateEntry() {
    if (!entry?.id) return;
    const data = await run(() => postBejermanPurchaseEntryValidate(entry.id, {}), "Validación ejecutada");
    if (data) {
      setValidation(data);
      await refreshCurrent(data.entry);
    }
  }

  async function emitEntry() {
    if (!entry?.id) return;
    const confirmed = window.confirm("Emitir el RT de compra en Bejerman con los bultos cargados?");
    if (!confirmed) return;
    const data = await run(() => postBejermanPurchaseEntryEmit(entry.id, {}), "Ingreso emitido en Bejerman");
    if (data) await refreshCurrent(data);
  }

  async function loadHistory() {
    const data = await run(() => getBejermanPurchaseHistory({ tipo: "RT", limit: 30 }));
    if (data) setHistory(data.items || []);
  }

  function setScanEdit(scanId, field, value) {
    setScanEdits((current) => ({
      ...current,
      [scanId]: {
        conversionFactor: current[scanId]?.conversionFactor ?? scans.find((scan) => scan.id === scanId)?.conversionFactor,
        unitValue: current[scanId]?.unitValue ?? scans.find((scan) => scan.id === scanId)?.unitValue,
        barcode: current[scanId]?.barcode ?? scans.find((scan) => scan.id === scanId)?.barcode,
        ...current[scanId],
        [field]: value,
      },
    }));
  }

  const validationErrors = validation?.errors || [];

  return (
    <div className={embedded ? "text-gray-900" : "min-h-screen bg-gray-50 p-2 text-gray-900 sm:p-4 md:p-6"}>
      <div className={embedded ? "space-y-4" : "mx-auto max-w-[1600px] space-y-4"}>
        <div className={`flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between ${embedded ? "rounded border border-gray-200 bg-white p-3" : "border-b border-gray-200 pb-3"}`}>
          <div className="min-w-0">
            <h2 className={embedded ? "text-lg font-semibold tracking-normal" : "text-2xl font-semibold tracking-normal"}>Ingresos de mercadería Bejerman</h2>
            <p className="text-sm text-gray-600">Compras RT / MC para stock de venta en SEPID, depósito VAL.</p>
          </div>
          <ResponsiveActionBar>
            <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 ${fullWidthButtonClass}`} onClick={loadHistory}>
              <ClipboardList className="h-4 w-4" />
              Historial RT
            </button>
            <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 ${fullWidthButtonClass}`} onClick={() => refreshCurrent()}>
              <RefreshCw className="h-4 w-4" />
              Actualizar
            </button>
            {canManage && (
              <button type="button" className={`inline-flex h-9 items-center gap-2 rounded bg-emerald-700 px-3 text-sm font-medium text-white hover:bg-emerald-800 ${fullWidthButtonClass}`} onClick={createDraft}>
                <PackagePlus className="h-4 w-4" />
                Nuevo lote
              </button>
            )}
          </ResponsiveActionBar>
        </div>

        {(error || message) && (
          <div className={`rounded border px-3 py-2 text-sm ${error ? "border-rose-200 bg-rose-50 text-rose-800" : "border-emerald-200 bg-emerald-50 text-emerald-800"}`}>
            {error || message}
          </div>
        )}

        <div className="grid gap-3 xl:grid-cols-[300px_minmax(0,1fr)]">
          <aside className="space-y-3 xl:sticky xl:top-4 xl:self-start">
            <div className="rounded border border-gray-200 bg-white">
              <div className="border-b border-gray-200 px-3 py-2 text-sm font-semibold">Lotes</div>
              <div className="max-h-56 overflow-y-auto md:max-h-72 xl:max-h-[520px]">
                {entries.length === 0 && <div className="px-3 py-4 text-sm text-gray-500">Sin lotes cargados.</div>}
                {entries.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`block w-full border-b border-gray-100 px-3 py-2 text-left hover:bg-gray-50 ${entry?.id === item.id ? "bg-emerald-50" : "bg-white"}`}
                    onClick={() => loadEntry(item.id)}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{item.remitoNumber || `${item.comprobanteTipo || "RT"} ${item.comprobantePtoVenta || "-"}-${item.comprobanteNumero || "-"}`}</span>
                      <StatusBadge status={item.status} />
                    </div>
                    <div className="mt-1 truncate text-xs text-gray-600">{item.supplierName || "Proveedor sin asignar"}</div>
                    <div className="mt-1 flex justify-between text-xs text-gray-500">
                      <span>{item.issueDate || "-"}</span>
                      <span>$ {money(item.totalValue)}</span>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            {history.length > 0 && (
              <div className="rounded border border-gray-200 bg-white">
                <div className="border-b border-gray-200 px-3 py-2 text-sm font-semibold">Historial Bejerman</div>
                <div className="max-h-56 overflow-y-auto md:max-h-80">
                  {history.map((item, index) => (
                    <div key={`${item.remitoNumber}-${index}`} className="border-b border-gray-100 px-3 py-2 text-xs">
                      <div className="font-medium text-gray-900">{item.remitoNumber}</div>
                      <div className="truncate text-gray-600">{item.supplierName || item.supplierCode}</div>
                      <div className="flex justify-between text-gray-500">
                        <span>{item.issueDate || "-"}</span>
                        <span>$ {money(item.totalValue)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </aside>

          <main className="min-w-0 space-y-3 md:space-y-4">
            {!entry && (
              <div className="rounded border border-gray-200 bg-white px-4 py-8 text-center text-sm text-gray-600">
                {loading ? "Cargando..." : "Creá un lote para empezar a escanear mercadería."}
              </div>
            )}

            {entry && (
              <>
                <section className="rounded border border-gray-200 bg-white p-2 sm:p-3">
                  <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusBadge status={entry.status} />
                      <span className="text-sm text-gray-600">Lote {entry.id}</span>
                      {entry.remitoNumber && <span className="text-sm font-semibold text-emerald-700">{entry.remitoNumber}</span>}
                    </div>
                    <ResponsiveActionBar>
                      {canManage && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={saveHeader} disabled={isLocked}>
                          <Save className="h-4 w-4" />
                          Guardar RT
                        </button>
                      )}
                      {canManage && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-rose-200 bg-white px-3 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={discardEntry} disabled={isLocked}>
                          <Trash2 className="h-4 w-4" />
                          Descartar
                        </button>
                      )}
                      {canManage && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-emerald-300 bg-emerald-50 px-3 text-sm text-emerald-800 hover:bg-emerald-100 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={validateEntry} disabled={isLocked}>
                          <CheckCircle2 className="h-4 w-4" />
                          Validar
                        </button>
                      )}
                      {canEmit && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded bg-emerald-700 px-3 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={emitEntry} disabled={isLocked || scans.length === 0}>
                          <Send className="h-4 w-4" />
                          Emitir
                        </button>
                      )}
                    </ResponsiveActionBar>
                  </div>

                  <div className="mt-3 grid gap-3 2xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
                    <div className="space-y-2">
                      <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
                        <input
                          className="h-9 rounded border border-gray-300 px-2 text-sm"
                          value={providerQuery}
                          onChange={(event) => setProviderQuery(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") searchProviders();
                          }}
                          placeholder="Buscar proveedor por código, CUIT o razón social"
                          disabled={isLocked}
                        />
                        <button type="button" className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50" onClick={searchProviders} disabled={isLocked}>
                          <Search className="h-4 w-4" />
                          Buscar
                        </button>
                      </div>
                      {providers.length > 0 && (
                        <div className="max-h-40 overflow-y-auto rounded border border-gray-200">
                          {providers.map((provider) => (
                            <button key={`${provider.rawCode}-${provider.name}`} type="button" className="block w-full border-b border-gray-100 px-3 py-2 text-left text-sm hover:bg-gray-50" onClick={() => pickProvider(provider)} disabled={isLocked}>
                              <span className="font-mono text-xs text-gray-600">{provider.code}</span>
                              <span className="ml-2 font-medium">{provider.name}</span>
                              {provider.cuit && <span className="ml-2 text-xs text-gray-500">{provider.cuit}</span>}
                            </button>
                          ))}
                        </div>
                      )}
                      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-[120px_1fr_160px_130px]">
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={header.supplierCode} onChange={(e) => setHeader({ ...header, supplierCode: e.target.value, supplierCodeRaw: e.target.value })} placeholder="Código" disabled={isLocked} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={header.supplierName} onChange={(e) => setHeader({ ...header, supplierName: e.target.value })} placeholder="Proveedor" disabled={isLocked} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={header.supplierTaxId} onChange={(e) => setHeader({ ...header, supplierTaxId: e.target.value })} placeholder="CUIT" disabled={isLocked} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={header.paymentTermCode} onChange={(e) => setHeader({ ...header, paymentTermCode: e.target.value })} placeholder="Cond. pago (opcional)" disabled={isLocked} />
                      </div>
                    </div>

                    <div className="grid gap-2 sm:grid-cols-3">
                      <label className="text-xs font-medium text-gray-600">
                        Letra
                        <input className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.comprobanteLetra} onChange={(e) => setHeader({ ...header, comprobanteLetra: e.target.value.toUpperCase() })} disabled={isLocked} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Punto
                        <input className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.comprobantePtoVenta} onChange={(e) => setHeader({ ...header, comprobantePtoVenta: e.target.value })} disabled={isLocked} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Número
                        <input className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.comprobanteNumero} onChange={(e) => setHeader({ ...header, comprobanteNumero: e.target.value })} disabled={isLocked} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Fecha
                        <input type="date" className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.issueDate} onChange={(e) => setHeader({ ...header, issueDate: e.target.value, accountingDate: e.target.value, ddjjDate: e.target.value })} disabled={isLocked} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Contable
                        <input type="date" className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.accountingDate} onChange={(e) => setHeader({ ...header, accountingDate: e.target.value })} disabled={isLocked} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        DDJJ
                        <input type="date" className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.ddjjDate} onChange={(e) => setHeader({ ...header, ddjjDate: e.target.value })} disabled={isLocked} />
                      </label>
                    </div>
                  </div>

                  <textarea className="mt-3 min-h-16 w-full rounded border border-gray-300 px-2 py-2 text-sm" value={header.notes} onChange={(e) => setHeader({ ...header, notes: e.target.value })} placeholder="Observaciones para auditoría" disabled={isLocked} />
                </section>

                <section className="rounded border border-gray-200 bg-white p-3">
                  <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(420px,0.42fr)]">
                    <div className="min-w-0">
                      <div className="grid gap-2 lg:grid-cols-[minmax(320px,1fr)_auto]">
                        <input
                          className="h-9 rounded border border-gray-300 px-2 text-sm"
                          value={articleQuery}
                          onChange={(event) => setArticleQuery(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") searchArticles();
                          }}
                          placeholder="Buscar artículo Bejerman por código o descripción"
                          disabled={isLocked}
                          autoComplete="off"
                        />
                        <button type="button" className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50" onClick={searchArticles} disabled={isLocked}>
                          <Search className="h-4 w-4" />
                          Buscar
                        </button>
                      </div>
                      {(articles.length > 0 || articleLoading || articleError) && (
                        <div className="mt-2 max-h-44 overflow-y-auto rounded border border-gray-200">
                          {articleLoading && (
                            <div className="px-3 py-2 text-sm text-gray-500">Buscando artículos...</div>
                          )}
                          {articleError && !articleLoading && (
                            <div className="px-3 py-2 text-sm text-rose-700">{articleError}</div>
                          )}
                          {articles.map((article) => (
                            <button key={`${article.code}-${article.name}`} type="button" className="block w-full border-b border-gray-100 px-3 py-2 text-left hover:bg-gray-50" onClick={() => pickArticle(article)} disabled={isLocked}>
                              <div className="font-mono text-xs text-gray-600">{article.code || article.id}</div>
                              <div className="text-sm font-medium text-gray-900">{article.description || article.name}</div>
                            </button>
                          ))}
                        </div>
                      )}
                      <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-[180px_minmax(260px,1fr)_120px_150px_auto]">
                        <input className="h-9 rounded border border-gray-300 px-2 font-mono text-sm" value={lineForm.articleCode} onChange={(e) => setLineForm({ ...lineForm, articleCode: e.target.value })} placeholder="Artículo" disabled={isLocked} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={lineForm.articleDescription} onChange={(e) => setLineForm({ ...lineForm, articleDescription: e.target.value })} placeholder="Descripción" disabled={isLocked} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={lineForm.defaultConversionFactor} onChange={(e) => setLineForm({ ...lineForm, defaultConversionFactor: e.target.value })} placeholder="Factor" disabled={isLocked} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={lineForm.defaultUnitValue} onChange={(e) => setLineForm({ ...lineForm, defaultUnitValue: e.target.value })} placeholder="Valor" disabled={isLocked} />
                        <button type="button" className="inline-flex h-9 items-center justify-center gap-2 rounded bg-emerald-700 px-3 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50 sm:col-span-2 xl:col-span-1" onClick={addLine} disabled={isLocked || !canManage}>
                          <PackagePlus className="h-4 w-4" />
                          Agregar
                        </button>
                      </div>
                    </div>

                    <div className="min-w-0 rounded border border-gray-200 bg-gray-50 p-3">
                      <div className="mb-2 text-sm font-semibold">Línea activa</div>
                      {activeLine ? (
                        <div className="space-y-2">
                          <select className="h-9 w-full rounded border border-gray-300 px-2 text-sm" value={activeLine.id} onChange={(e) => setActiveLineId(e.target.value)} disabled={isLocked}>
                            {(entry.lines || []).map((line) => (
                              <option key={line.id} value={line.id}>
                                {line.articleCode} - {line.articleDescription || "Sin descripción"}
                              </option>
                            ))}
                          </select>
                          <div className="rounded border border-gray-200 bg-white p-2">
                            <div className="font-mono text-xs text-gray-600">{activeLine.articleCode}</div>
                            <div className="mt-0.5 break-words text-sm font-medium text-gray-900">
                              {activeLine.articleDescription || "Sin descripción"}
                            </div>
                          </div>
                          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[minmax(100px,1fr)_minmax(120px,1fr)_44px]">
                            <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={activeDefaults.defaultConversionFactor} onChange={(e) => setActiveDefaults({ ...activeDefaults, defaultConversionFactor: e.target.value })} placeholder={`Factor ${activeLine.defaultConversionFactor}`} disabled={isLocked} />
                            <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={activeDefaults.defaultUnitValue} onChange={(e) => setActiveDefaults({ ...activeDefaults, defaultUnitValue: e.target.value })} placeholder={`Valor ${activeLine.defaultUnitValue}`} disabled={isLocked} />
                            <button type="button" className="inline-flex h-9 w-9 items-center justify-center rounded border border-gray-300 bg-white hover:bg-gray-50 disabled:opacity-50" onClick={updateActiveLine} disabled={isLocked || !canManage} title="Actualizar valores de la línea">
                              <Save className="h-4 w-4" />
                            </button>
                          </div>
                          <button type="button" className="inline-flex h-8 items-center gap-2 rounded border border-rose-200 bg-white px-2 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50" onClick={() => deleteLine(activeLine.id)} disabled={isLocked || !canManage}>
                            <Trash2 className="h-3.5 w-3.5" />
                            Eliminar línea
                          </button>
                        </div>
                      ) : (
                        <div className="text-sm text-gray-600">Agregá un artículo para habilitar el escáner.</div>
                      )}
                    </div>
                  </div>
                </section>

                <section className="rounded border border-gray-200 bg-white p-2 sm:p-3">
                  <div className="grid gap-3 2xl:grid-cols-[minmax(0,1fr)_minmax(420px,0.42fr)]">
                    <div>
                      <label className="text-xs font-semibold uppercase text-gray-500">Escáner</label>
                      <input
                        ref={scannerRef}
                        className="mt-1 h-12 w-full rounded border border-emerald-500 px-3 font-mono text-lg outline-none ring-2 ring-transparent focus:ring-emerald-200 disabled:border-gray-300 disabled:bg-gray-100"
                        value={scanText}
                        onChange={(event) => setScanText(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") {
                            event.preventDefault();
                            submitScan(scanText);
                          }
                        }}
                        onPaste={(event) => {
                          const text = event.clipboardData.getData("text");
                          if (text.includes("\n") || text.includes("\t")) {
                            event.preventDefault();
                            submitScan(text);
                          }
                        }}
                        placeholder={activeLine ? "Escanear código y Enter" : "Seleccioná una línea activa"}
                        disabled={isLocked || !activeLine || !canManage}
                      />
                    </div>
                    <div>
                      <label className="text-xs font-semibold uppercase text-gray-500">Cantidad sin código</label>
                      <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_auto]">
                        <input className="h-12 rounded border border-gray-300 px-3 text-sm" value={manualQuantity} onChange={(e) => setManualQuantity(e.target.value)} placeholder="Cantidad" disabled={isLocked || !activeLine || !canManage} />
                        <button type="button" className="inline-flex h-12 items-center justify-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50" onClick={addManualQuantity} disabled={isLocked || !activeLine || !manualQuantity || !canManage}>
                          <PackagePlus className="h-4 w-4" />
                          Agregar
                        </button>
                      </div>
                    </div>
                  </div>
                </section>

                <section className="rounded border border-gray-200 bg-white">
                  <div className="flex flex-col gap-2 border-b border-gray-200 px-3 py-2 md:flex-row md:items-center md:justify-between">
                    <div className="text-sm font-semibold">Bultos cargados</div>
                    <div className="flex flex-wrap gap-2 text-sm">
                      <span className="rounded border border-gray-200 bg-gray-50 px-2 py-1">Bultos {scans.length}</span>
                      <span className="rounded border border-gray-200 bg-gray-50 px-2 py-1">Unidades {quantity(entry.totalQuantity)}</span>
                      <span className="rounded border border-gray-200 bg-gray-50 px-2 py-1">Total $ {money(entry.totalValue)}</span>
                    </div>
                  </div>
                  <MobileDataList className="p-3">
                    {scans.length === 0 && (
                      <MobileDataCard className="text-center text-gray-500">
                        Sin bultos cargados.
                      </MobileDataCard>
                    )}
                    {scans.map((scan) => {
                      const edit = scanEdits[scan.id] || {};
                      return (
                        <MobileDataCard key={scan.id} className="space-y-3">
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="font-mono text-xs text-gray-600">{scan.articleCode}</div>
                              <div className="break-words font-medium text-gray-900">{scan.articleDescription || "-"}</div>
                            </div>
                            <span className="shrink-0 rounded border border-gray-200 bg-gray-50 px-2 py-1 text-xs text-gray-600">
                              {scan.isManualQuantity ? "Manual" : "Escaneo"}
                            </span>
                          </div>

                          <label className="block">
                            <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Código</span>
                            <input
                              className="h-10 w-full rounded border border-gray-300 px-2 font-mono text-sm disabled:bg-gray-100"
                              value={edit.barcode ?? scan.barcode ?? ""}
                              onChange={(e) => setScanEdit(scan.id, "barcode", e.target.value)}
                              onBlur={() => saveScan(scan)}
                              disabled={isLocked || !canManage || scan.isManualQuantity}
                            />
                          </label>

                          <div className="grid grid-cols-2 gap-2">
                            <label className="block">
                              <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Factor</span>
                              <input
                                className="h-10 w-full rounded border border-gray-300 px-2 text-right text-sm"
                                value={edit.conversionFactor ?? scan.conversionFactor}
                                onChange={(e) => setScanEdit(scan.id, "conversionFactor", e.target.value)}
                                onBlur={() => saveScan(scan)}
                                disabled={isLocked || !canManage}
                              />
                            </label>
                            <label className="block">
                              <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Valor unit.</span>
                              <input
                                className="h-10 w-full rounded border border-gray-300 px-2 text-right text-sm"
                                value={edit.unitValue ?? scan.unitValue}
                                onChange={(e) => setScanEdit(scan.id, "unitValue", e.target.value)}
                                onBlur={() => saveScan(scan)}
                                disabled={isLocked || !canManage}
                              />
                            </label>
                          </div>

                          <div className="flex items-center justify-between gap-3 border-t border-gray-100 pt-2">
                            <div>
                              <div className="text-[11px] font-semibold uppercase text-gray-500">Total</div>
                              <div className="font-semibold text-gray-900">$ {money(scan.totalValue)}</div>
                            </div>
                            <button
                              type="button"
                              className="inline-flex h-10 items-center justify-center gap-2 rounded border border-rose-200 px-3 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                              onClick={() => deleteScan(scan.id)}
                              disabled={isLocked || !canManage}
                            >
                              <Trash2 className="h-4 w-4" />
                              Eliminar
                            </button>
                          </div>
                        </MobileDataCard>
                      );
                    })}
                  </MobileDataList>
                  <DesktopTableWrap>
                    <table className="min-w-full text-left text-sm">
                      <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                        <tr>
                          <th className="px-3 py-2">Código</th>
                          <th className="px-3 py-2">Artículo</th>
                          <th className="px-3 py-2">Factor</th>
                          <th className="px-3 py-2">Valor unit.</th>
                          <th className="px-3 py-2">Total</th>
                          <th className="px-3 py-2">Modo</th>
                          <th className="px-3 py-2"></th>
                        </tr>
                      </thead>
                      <tbody>
                        {scans.length === 0 && (
                          <tr>
                            <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                              Sin bultos cargados.
                            </td>
                          </tr>
                        )}
                        {scans.map((scan) => {
                          const edit = scanEdits[scan.id] || {};
                          return (
                            <tr key={scan.id} className="border-t border-gray-100">
                              <td className="px-3 py-2">
                                <input className="h-8 w-48 rounded border border-gray-300 px-2 font-mono text-xs" value={edit.barcode ?? scan.barcode ?? ""} onChange={(e) => setScanEdit(scan.id, "barcode", e.target.value)} onBlur={() => saveScan(scan)} disabled={isLocked || !canManage || scan.isManualQuantity} />
                              </td>
                              <td className="px-3 py-2">
                                <div className="font-mono text-xs text-gray-600">{scan.articleCode}</div>
                                <div className="max-w-sm truncate text-gray-900">{scan.articleDescription}</div>
                              </td>
                              <td className="px-3 py-2">
                                <input className="h-8 w-24 rounded border border-gray-300 px-2 text-right text-sm" value={edit.conversionFactor ?? scan.conversionFactor} onChange={(e) => setScanEdit(scan.id, "conversionFactor", e.target.value)} onBlur={() => saveScan(scan)} disabled={isLocked || !canManage} />
                              </td>
                              <td className="px-3 py-2">
                                <input className="h-8 w-28 rounded border border-gray-300 px-2 text-right text-sm" value={edit.unitValue ?? scan.unitValue} onChange={(e) => setScanEdit(scan.id, "unitValue", e.target.value)} onBlur={() => saveScan(scan)} disabled={isLocked || !canManage} />
                              </td>
                              <td className="px-3 py-2 font-medium">$ {money(scan.totalValue)}</td>
                              <td className="px-3 py-2 text-xs text-gray-600">{scan.isManualQuantity ? "Manual" : "Escaneo"}</td>
                              <td className="px-3 py-2 text-right">
                                <button type="button" className="inline-flex h-8 w-8 items-center justify-center rounded border border-rose-200 text-rose-700 hover:bg-rose-50 disabled:opacity-50" onClick={() => deleteScan(scan.id)} disabled={isLocked || !canManage} title="Eliminar bulto">
                                  <Trash2 className="h-4 w-4" />
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </DesktopTableWrap>
                </section>

                {(validationErrors.length > 0 || validation?.ok) && (
                  <section className={`rounded border p-3 text-sm ${validation?.ok ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-rose-200 bg-rose-50 text-rose-900"}`}>
                    <div className="mb-2 flex items-center gap-2 font-semibold">
                      {validation?.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
                      {validation?.ok ? "Lote validado" : "Hay errores para corregir"}
                    </div>
                    {validationErrors.map((item) => (
                      <div key={`${item.code}-${item.field}`}>{item.message}</div>
                    ))}
                  </section>
                )}
              </>
            )}
          </main>
        </div>
      </div>
    </div>
  );
}
