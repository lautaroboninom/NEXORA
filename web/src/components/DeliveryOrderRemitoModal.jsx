import { useEffect, useMemo, useState } from "react";
import {
  getDeliveryOrderBejermanArticles,
  patchDeliveryOrderItemArticle,
  patchDeliveryOrderItemPartidas,
  postDeliveryOrderBejermanRemito,
} from "../lib/api";
import {
  deliveryOrderEquipmentContext,
  deliveryOrderItemEffectivePartida,
  deliveryOrderItemLabel,
  deliveryOrderItemPartidaLabel,
  deliveryOrderSourceLabel,
} from "../lib/delivery-orders";

const clean = (value) => String(value || "").trim();

function numberOf(value, fallback = 0) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  return Number.isFinite(parsed) ? parsed : fallback;
}

function itemNeedsPartidas(order, item) {
  if (!clean(item.articleCode)) return false;
  const quantity = numberOf(item.quantity, 1);
  const partidas = Array.isArray(item.partidas) ? item.partidas : [];
  const assigned = partidas.reduce((sum, partida) => sum + numberOf(partida.assignedQuantity), 0);
  const equipmentSerialFallback =
    !clean(item.partida) &&
    partidas.length === 0 &&
    Boolean(deliveryOrderItemEffectivePartida(item, order));

  if (equipmentSerialFallback) return false;
  if (quantity <= 1 && partidas.length === 0) return false;
  return assigned <= 0 || Math.abs(assigned - quantity) > 0.0001;
}

function orderIssues(order) {
  const items = Array.isArray(order.items) ? order.items : [];
  const missingArticle = !items.some((item) => clean(item.articleCode));
  const missingPartidas = items.filter((item) => itemNeedsPartidas(order, item)).length;
  return { missingArticle, missingPartidas };
}

function compatibilityIssues(orders) {
  if (!orders.length) return ["Seleccione al menos una orden."];
  const customerCodes = new Set(orders.map((order) => clean(order.bejermanCustomerCode)));
  const types = new Set(orders.map((order) => clean(order.deliveryType)));
  const issues = [];
  if (customerCodes.size !== 1 || !Array.from(customerCodes)[0]) {
    issues.push("Todas las órdenes deben tener el mismo cliente con código Bejerman.");
  }
  if (types.size !== 1) issues.push("Todas las órdenes deben tener el mismo tipo de remito.");
  if (orders.some((order) => order.remitoNumber || order.status === "facturado" || order.status === "cancelado")) {
    issues.push("No se pueden emitir órdenes cerradas, canceladas o con remito.");
  }
  if (orders.some((order) => clean(order.bejermanRemitoGroupId))) {
    issues.push("Hay una orden con emisión Bejerman en curso.");
  }
  orders.forEach((order) => {
    const issuesForOrder = orderIssues(order);
    if (issuesForOrder.missingArticle) issues.push(`${order.orderNumber}: falta artículo Bejerman.`);
    if (issuesForOrder.missingPartidas) issues.push(`${order.orderNumber}: faltan partidas completas.`);
  });
  return issues;
}

function replaceOrder(orders, updated) {
  return orders.map((order) => (order.id === updated.id ? updated : order));
}

function openPreparingPrintWindow() {
  const printWindow = window.open("", "_blank");
  if (!printWindow) return null;
  printWindow.document.write(
    '<!doctype html><title>Remito</title><body style="font-family: system-ui; padding: 24px;">Preparando PDF del remito...</body>'
  );
  printWindow.document.close();
  return printWindow;
}

function openPrintUrl(printUrl, printWindow) {
  if (printWindow) {
    printWindow.location.href = printUrl;
    printWindow.focus();
    return true;
  }
  return Boolean(window.open(printUrl, "_blank", "noopener,noreferrer"));
}

function ArticleEditor({ order, item, onUpdated, disabled }) {
  const [search, setSearch] = useState(clean(item.articleName) || clean(item.description) || clean(item.sourceText) || clean(item.articleCode));
  const [manualCode, setManualCode] = useState(clean(item.articleCode));
  const [manualName, setManualName] = useState(clean(item.articleName));
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const runSearch = async () => {
    const q = clean(search);
    if (!q) return;
    setLoading(true);
    setError("");
    try {
      const data = await getDeliveryOrderBejermanArticles({ q, search: q, limit: 20 });
      setResults(Array.isArray(data?.items) ? data.items : []);
      if (data?.unavailable) setError("No se pudo consultar Bejerman en este momento.");
    } catch (err) {
      setResults([]);
      setError(err?.message || "No se pudo consultar artículos.");
    } finally {
      setLoading(false);
    }
  };

  const saveArticle = async (article) => {
    const articleCode = clean(article?.code || manualCode);
    const articleName = clean(article?.name || manualName);
    if (!articleCode) {
      setError("Ingrese un código de artículo.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const updated = await patchDeliveryOrderItemArticle(order.id, item.id, {
        articleCode,
        articleName: articleName || articleCode,
        unitPrice: item.unitPrice,
        partida: deliveryOrderItemEffectivePartida(item, order),
      });
      onUpdated(updated);
      setManualCode(articleCode);
      setManualName(articleName || articleCode);
    } catch (err) {
      setError(err?.message || "No se pudo guardar el artículo.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded border border-amber-200 bg-amber-50 p-3">
      <div className="mb-2 text-sm font-medium text-amber-900">Asignar artículo Bejerman</div>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-[minmax(180px,1fr)_110px]">
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              runSearch();
            }
          }}
          className="h-9 rounded border bg-white px-2"
          placeholder="Buscar artículo por código o descripción"
          disabled={disabled || saving}
        />
        <button type="button" onClick={runSearch} disabled={disabled || loading || saving} className="rounded border bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50">
          {loading ? "Buscando..." : "Buscar"}
        </button>
      </div>
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-[130px_minmax(180px,1fr)_110px]">
        <input value={manualCode} onChange={(event) => setManualCode(event.target.value)} className="h-9 rounded border bg-white px-2" placeholder="Código" disabled={disabled || saving} />
        <input value={manualName} onChange={(event) => setManualName(event.target.value)} className="h-9 rounded border bg-white px-2" placeholder="Descripción" disabled={disabled || saving} />
        <button type="button" onClick={() => saveArticle(null)} disabled={disabled || saving} className="rounded border bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50">
          Guardar
        </button>
      </div>
      {error && <div className="mt-2 text-xs text-red-700">{error}</div>}
      {results.length > 0 && (
        <div className="mt-2 max-h-44 overflow-y-auto rounded border bg-white">
          {results.map((article) => (
            <button
              key={article.id || article.code}
              type="button"
              onClick={() => saveArticle(article)}
              className="block w-full border-b px-3 py-2 text-left text-sm hover:bg-gray-50"
              disabled={disabled || saving}
            >
              <span className="font-medium">{article.code || "-"}</span>
              <span className="ml-2 text-gray-700">{article.name || article.description || "-"}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function PartidasEditor({ order, item, onUpdated, disabled }) {
  const initial = Array.isArray(item.partidas) && item.partidas.length
    ? item.partidas.map((partida) => ({
        partida: clean(partida.partida),
        assignedQuantity: String(partida.assignedQuantity || item.quantity || 1),
        partidaExpirationDate: clean(partida.partidaExpirationDate),
        stockDepositCode: clean(partida.stockDepositCode),
      }))
    : [
        {
          partida: deliveryOrderItemEffectivePartida(item, order),
          assignedQuantity: String(item.quantity || 1),
          partidaExpirationDate: clean(item.partidaExpirationDate),
          stockDepositCode: clean(item.stockDepositCode),
        },
      ];
  const [rows, setRows] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const updateRow = (index, changes) => {
    setRows((current) => current.map((row, rowIndex) => (rowIndex === index ? { ...row, ...changes } : row)));
  };

  const save = async () => {
    const partidas = rows
      .map((row, index) => ({
        partida: clean(row.partida),
        assignedQuantity: numberOf(row.assignedQuantity, 0),
        partidaExpirationDate: clean(row.partidaExpirationDate) || null,
        stockDepositCode: clean(row.stockDepositCode) || null,
        sortOrder: index,
      }))
      .filter((row) => row.partida && row.assignedQuantity > 0);
    if (!partidas.length) {
      setError("Cargue al menos una partida.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const updated = await patchDeliveryOrderItemPartidas(order.id, item.id, partidas);
      onUpdated(updated);
    } catch (err) {
      setError(err?.message || "No se pudieron guardar las partidas.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded border border-sky-200 bg-sky-50 p-3">
      <div className="mb-2 text-sm font-medium text-sky-900">Completar partidas</div>
      <div className="space-y-2">
        {rows.map((row, index) => (
          <div key={index} className="grid grid-cols-1 gap-2 md:grid-cols-[minmax(160px,1fr)_95px_140px_100px_38px]">
            <input value={row.partida} onChange={(event) => updateRow(index, { partida: event.target.value })} className="h-9 rounded border bg-white px-2" placeholder="Partida" disabled={disabled || saving} />
            <input value={row.assignedQuantity} onChange={(event) => updateRow(index, { assignedQuantity: event.target.value })} className="h-9 rounded border bg-white px-2" placeholder="Cantidad" disabled={disabled || saving} />
            <input type="date" value={row.partidaExpirationDate} onChange={(event) => updateRow(index, { partidaExpirationDate: event.target.value })} className="h-9 rounded border bg-white px-2" disabled={disabled || saving} />
            <input value={row.stockDepositCode} onChange={(event) => updateRow(index, { stockDepositCode: event.target.value })} className="h-9 rounded border bg-white px-2" placeholder="Depósito" disabled={disabled || saving} />
            <button type="button" onClick={() => setRows((current) => current.filter((_, rowIndex) => rowIndex !== index))} className="rounded border bg-white px-2 text-sm disabled:opacity-40" disabled={disabled || saving || rows.length <= 1}>
              X
            </button>
          </div>
        ))}
      </div>
      <div className="mt-2 flex justify-between gap-2">
        <button type="button" onClick={() => setRows((current) => [...current, { partida: "", assignedQuantity: "1", partidaExpirationDate: "", stockDepositCode: "" }])} className="rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-50" disabled={disabled || saving}>
          Agregar partida
        </button>
        <button type="button" onClick={save} className="rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50" disabled={disabled || saving}>
          Guardar partidas
        </button>
      </div>
      {error && <div className="mt-2 text-xs text-red-700">{error}</div>}
    </div>
  );
}

export default function DeliveryOrderRemitoModal({ open, orders, canAssignArticles, onClose, onGenerated }) {
  const [localOrders, setLocalOrders] = useState([]);
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setLocalOrders(orders || []);
    setNotes("");
    setError("");
  }, [open, orders]);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  const issues = useMemo(() => compatibilityIssues(localOrders), [localOrders]);
  const canGenerate = issues.length === 0 && !saving;

  if (!open) return null;

  const updateOrder = (updated) => {
    setLocalOrders((current) => replaceOrder(current, updated));
  };

  const generate = async () => {
    setSaving(true);
    setError("");
    const printWindow = openPreparingPrintWindow();
    try {
      const result = await postDeliveryOrderBejermanRemito({
        orderIds: localOrders.map((order) => order.id),
        notes,
      });
      const url = result?.printUrl || result?.pdfUrl;
      if (url && !openPrintUrl(url, printWindow)) {
        setError(`Remito ${result?.remitoNumber || ""} emitido, pero el navegador bloqueó la impresión automática.`);
      }
      onGenerated(result);
    } catch (err) {
      if (printWindow) printWindow.close();
      setError(err?.message || "No se pudo generar el remito Bejerman.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="max-h-[92vh] w-full max-w-6xl overflow-hidden rounded bg-white shadow-xl" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Generar remito Bejerman</h2>
            <p className="text-sm text-gray-600">Complete artículos y partidas antes de emitir.</p>
          </div>
          <button type="button" className="text-sm text-gray-500 hover:text-gray-900" onClick={onClose}>
            Cerrar
          </button>
        </div>

        <div className="max-h-[calc(92vh-148px)] overflow-y-auto p-5">
          {issues.length > 0 && (
            <div className="mb-4 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              {issues.map((issue) => (
                <div key={issue}>{issue}</div>
              ))}
            </div>
          )}
          {error && <div className="mb-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

          <div className="space-y-4">
            {localOrders.map((order) => {
              const issuesForOrder = orderIssues(order);
              return (
                <section key={order.id} className="rounded border">
                  <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-gray-50 px-3 py-2">
                    <div>
                      <div className="font-medium text-gray-900">{order.orderNumber}</div>
                      <div className="text-xs text-gray-600">
                        {[order.customerName, order.bejermanCustomerCode || "sin código", deliveryOrderSourceLabel(order)].filter(Boolean).join(" - ")}
                      </div>
                      {deliveryOrderEquipmentContext(order) && (
                        <div className="mt-0.5 text-xs text-gray-500">{deliveryOrderEquipmentContext(order)}</div>
                      )}
                    </div>
                    <div className="text-xs text-gray-500">{order.sourceReference || order.deliveryType}</div>
                  </div>
                  <div className="space-y-3 p-3">
                    {(order.items || []).map((item) => (
                      <div key={item.id} className="space-y-2 rounded border bg-white p-3">
                        <div className="grid grid-cols-1 gap-2 text-sm md:grid-cols-[120px_minmax(180px,1fr)_90px_130px]">
                          <div>
                            <div className="text-xs uppercase text-gray-500">Artículo</div>
                            <div className="font-medium">{item.articleCode || "-"}</div>
                          </div>
                          <div>
                            <div className="text-xs uppercase text-gray-500">Detalle</div>
                            <div>{deliveryOrderItemLabel(item)}</div>
                          </div>
                          <div>
                            <div className="text-xs uppercase text-gray-500">Cantidad</div>
                            <div>{item.quantity || 1}</div>
                          </div>
                          <div>
                            <div className="text-xs uppercase text-gray-500">Partida</div>
                            <div>{deliveryOrderItemPartidaLabel(item, order) || "-"}</div>
                          </div>
                        </div>
                        {!clean(item.articleCode) && canAssignArticles && (
                          <ArticleEditor order={order} item={item} onUpdated={updateOrder} disabled={saving} />
                        )}
                        {itemNeedsPartidas(order, item) && canAssignArticles && (
                          <PartidasEditor order={order} item={item} onUpdated={updateOrder} disabled={saving} />
                        )}
                      </div>
                    ))}
                    {!canAssignArticles && (issuesForOrder.missingArticle || issuesForOrder.missingPartidas) && (
                      <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                        Tu usuario no tiene permiso para asignar artículos o partidas.
                      </div>
                    )}
                  </div>
                </section>
              );
            })}
          </div>

          <label className="mt-4 block text-sm">
            <span className="mb-1 block text-xs uppercase text-gray-500">Observaciones</span>
            <textarea value={notes} onChange={(event) => setNotes(event.target.value)} rows={3} className="w-full rounded border px-3 py-2" />
          </label>
        </div>

        <div className="flex justify-end gap-2 border-t px-5 py-4">
          <button type="button" className="rounded border px-4 py-2 text-sm hover:bg-gray-50" onClick={onClose} disabled={saving}>
            Cancelar
          </button>
          <button type="button" className="btn disabled:opacity-50" onClick={generate} disabled={!canGenerate}>
            {saving ? "Generando..." : "Generar remito"}
          </button>
        </div>
      </div>
    </div>
  );
}
