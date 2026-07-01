import { useEffect, useMemo, useState } from "react";
import {
  getDeliveryOrderBejermanArticleStock,
  getDeliveryOrderBejermanArticles,
  patchDeliveryOrderItemArticle,
  patchDeliveryOrderItemPartidas,
  postDeliveryOrderBejermanRemito,
} from "../lib/api";
import {
  deliveryOrderEquipmentContext,
  deliveryOrderCompanyKey,
  deliveryOrderCompanyLabel,
  deliveryOrderItemCanOmitPartida,
  deliveryOrderItemEffectivePartida,
  deliveryOrderItemLabel,
  deliveryOrderItemPartidaLabel,
  deliveryOrderItemRequiresPartida,
  deliveryOrderSourceLabel,
} from "../lib/delivery-orders";
import { openPrintablePdf, waitForPdfBlob } from "../lib/pdf";
import RisProgressModal, {
  waitForRisProgressMinimum,
  waitForRisProgressPaint,
} from "./RisProgressModal.jsx";
import { ResponsiveModalOverlay, ResponsiveModalPanel } from "./Responsive.jsx";

const clean = (value) => String(value || "").trim();

const REMITO_DOCUMENT_BY_TYPE = {
  sale: "RT",
  rental: "RTA",
  demo: "RTN",
  service_release: "RSS",
};

const REMITO_PROGRESS_BY_TYPE = {
  sale: "RT",
  rental: "RTA",
  demo: "RTN",
  service_release: "RSS",
};

function remitoDocumentFromOrders(orders) {
  const types = new Set((orders || []).map((order) => clean(order?.deliveryType)).filter(Boolean));
  if (types.size !== 1) return "remito";
  return REMITO_DOCUMENT_BY_TYPE[Array.from(types)[0]] || "remito";
}

function remitoProgressFromOrders(orders) {
  const types = new Set((orders || []).map((order) => clean(order?.deliveryType)).filter(Boolean));
  if (types.size !== 1) return "remito";
  return REMITO_PROGRESS_BY_TYPE[Array.from(types)[0]] || "remito";
}

function numberOf(value, fallback = 0) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatQuantity(value) {
  const parsed = numberOf(value, 0);
  if (Number.isInteger(parsed)) return String(parsed);
  return parsed.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

function suppressEnterSubmit(event) {
  if (event.key === "Enter") event.preventDefault();
}

function defaultDepositForOrder(order) {
  return clean(order?.deliveryType) === "rental" ? "STL" : "VAL";
}

function itemStockDeposit(order, item) {
  const itemDeposit = clean(item?.stockDepositCode).toUpperCase();
  if (itemDeposit) return itemDeposit;
  const partidas = Array.isArray(item?.partidas) ? item.partidas : [];
  for (const partida of partidas) {
    const deposit = clean(partida?.stockDepositCode).toUpperCase();
    if (deposit) return deposit;
  }
  return defaultDepositForOrder(order);
}

function lotAvailableQuantity(lot) {
  return numberOf(lot?.stockAvailableQuantity ?? lot?.availableQuantity ?? lot?.realQuantity, 0);
}

function lotExpiration(lot) {
  return clean(lot?.partidaExpirationDate || lot?.expirationDate);
}

function lotLabel(lot) {
  return [
    clean(lot?.partida),
    `Stock ${formatQuantity(lotAvailableQuantity(lot))}`,
    lotExpiration(lot) ? `Vto. ${lotExpiration(lot)}` : "",
  ]
    .filter(Boolean)
    .join(" - ");
}

function lotDraft(lot, assignedQuantity = "1") {
  return {
    ingresoId: "",
    deviceId: "",
    partida: clean(lot?.partida),
    assignedQuantity: String(assignedQuantity),
    partidaExpirationDate: lotExpiration(lot),
    stockDepositCode: clean(lot?.stockDepositCode || lot?.depositCode) || "VAL",
    stockAvailableQuantity: String(lotAvailableQuantity(lot)),
    stockCheckedAt: clean(lot?.stockCheckedAt),
  };
}

function itemNeedsPartidas(order, item) {
  if (!clean(item.articleCode)) return false;
  const quantity = numberOf(item.quantity, 1);
  const partidas = Array.isArray(item.partidas) ? item.partidas : [];
  const assigned = partidas.reduce((sum, partida) => sum + numberOf(partida.assignedQuantity), 0);
  const explicitPartida = clean(item.partida);
  const canOmitPartida = deliveryOrderItemCanOmitPartida(order, item);
  if (canOmitPartida && !explicitPartida && partidas.length === 0) return false;
  if (order?.deliveryType === "rental") {
    if (!explicitPartida && partidas.length === 0) return true;
    if (partidas.length > 0) return assigned <= 0 || Math.abs(assigned - quantity) > 0.0001;
    return false;
  }
  if (order?.deliveryType === "sale" && !explicitPartida && partidas.length === 0) return true;
  const equipmentSerialFallback =
    !explicitPartida &&
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
  const companyKeys = new Set(orders.map((order) => deliveryOrderCompanyKey(order)));
  const issues = [];
  if (customerCodes.size !== 1 || !Array.from(customerCodes)[0]) {
    issues.push("Todas las órdenes deben tener el mismo cliente con código Bejerman.");
  }
  if (types.size !== 1) issues.push("Todas las órdenes deben tener el mismo tipo de remito.");
  if (companyKeys.size !== 1) issues.push("Todas las órdenes deben pertenecer a la misma empresa Bejerman.");
  if (orders.some((order) => order.status === "pendiente_stock")) {
    issues.push("Las órdenes pendientes de stock deben pasar a pendiente de armado antes de emitir remito.");
  }
  if (orders.some((order) => order.remitoNumber || order.status === "facturado" || order.status === "cancelado")) {
    issues.push("No se pueden emitir órdenes cerradas, canceladas o con remito.");
  }
  if (orders.some((order) => clean(order.bejermanRemitoGroupId))) {
    issues.push("Hay una orden con emisión Bejerman en curso.");
  }
  orders.forEach((order) => {
    const issuesForOrder = orderIssues(order);
    if (issuesForOrder.missingArticle) issues.push(`${order.orderNumber}: falta artículo Bejerman.`);
    if (issuesForOrder.missingPartidas) {
      issues.push(`${order.orderNumber}: ${order.deliveryType === "rental" ? "faltan NS completos" : "faltan partidas completas"}.`);
    }
  });
  return issues;
}

function replaceOrder(orders, updated) {
  return orders.map((order) => (order.id === updated.id ? updated : order));
}

function remitoPdfUrlFromResult(result) {
  if (result?.pdfUrl) return result.pdfUrl;
  if (result?.groupId) return `/api/ordenes-entrega/remito-bejerman/${encodeURIComponent(result.groupId)}/pdf/`;
  const printUrl = String(result?.printUrl || "").trim();
  if (printUrl) return printUrl.replace(/\/print\/?$/, "/pdf/");
  return "";
}

function openRemitoPrintablePdf(blob, result, documentLabel = "remito") {
  const remito = clean(result?.remitoNumber);
  const label = clean(documentLabel) || "remito";
  return openPrintablePdf(blob, {
    title: remito ? `${label} ${remito}` : label,
    documentLabel: remito ? `${label} ${remito}` : label,
  });
}

function ArticleEditor({ order, item, onUpdated, disabled }) {
  const [search, setSearch] = useState(clean(item.articleName) || clean(item.description) || clean(item.sourceText) || clean(item.articleCode));
  const [manualCode, setManualCode] = useState(clean(item.articleCode));
  const [manualName, setManualName] = useState(clean(item.articleName));
  const [manualRequiresPartida, setManualRequiresPartida] = useState(deliveryOrderItemRequiresPartida(item));
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [stock, setStock] = useState(null);
  const [selectedLot, setSelectedLot] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const q = clean(search);
    if (q.length < 2) {
      setResults([]);
      setEmpty(false);
      setLoading(false);
      return undefined;
    }
    if (q === clean(manualName) && clean(manualCode)) {
      setResults([]);
      setEmpty(false);
      setLoading(false);
      return undefined;
    }
    const timer = window.setTimeout(async () => {
      setLoading(true);
      setEmpty(false);
      setError("");
      try {
        const data = await getDeliveryOrderBejermanArticles({
          q,
          search: q,
          limit: 20,
          companyKey: deliveryOrderCompanyKey(order),
        });
        const items = Array.isArray(data?.items) ? data.items : [];
        setResults(items);
        setEmpty(items.length === 0);
        if (data?.unavailable) setError("No se pudo consultar Bejerman en este momento.");
      } catch (err) {
        setResults([]);
        setEmpty(false);
        setError(err?.message || "No se pudo consultar artículos.");
      } finally {
        setLoading(false);
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search, manualCode, manualName]);

  const loadStock = async (articleCode) => {
    const code = clean(articleCode);
    if (!code) {
      setStock(null);
      return;
    }
    setStock({ loading: true, items: [], warning: "", unavailable: false, depositCode: "VAL" });
    setSelectedLot(null);
    try {
      const data = await getDeliveryOrderBejermanArticleStock({
        articleCode: code,
        limit: 100,
        companyKey: deliveryOrderCompanyKey(order),
        deliveryType: order.deliveryType,
        depositCode: itemStockDeposit(order, item),
      });
      setStock({
        loading: false,
        items: Array.isArray(data?.items) ? data.items : [],
        warning: clean(data?.warning),
        unavailable: Boolean(data?.unavailable),
        depositCode: clean(data?.depositCode) || "VAL",
      });
    } catch (err) {
      setStock({
        loading: false,
        items: [],
        warning: err?.message || "No se pudo verificar stock Bejerman.",
        unavailable: true,
        depositCode: "VAL",
      });
    }
  };

  const chooseArticle = (article) => {
    const articleCode = clean(article?.code);
    const articleName = clean(article?.name || article?.description || articleCode);
    setManualCode(articleCode);
    setManualName(articleName);
    setManualRequiresPartida(deliveryOrderItemRequiresPartida(article));
    setSearch(articleName || articleCode);
    setResults([]);
    setEmpty(false);
    setError("");
    void loadStock(articleCode);
  };

  const saveArticle = async () => {
    const articleCode = clean(manualCode);
    const articleName = clean(manualName);
    if (!articleCode) {
      setError("Ingrese un código de artículo.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const selectedLotDraft = selectedLot
        ? lotDraft(
            selectedLot,
            Math.min(
              Math.max(numberOf(item.quantity, 1), 1),
              lotAvailableQuantity(selectedLot) || Math.max(numberOf(item.quantity, 1), 1),
            ),
          )
        : null;
      const updated = await patchDeliveryOrderItemArticle(order.id, item.id, {
        articleCode,
        articleName: articleName || articleCode,
        articleRequiresPartida: manualRequiresPartida,
        unitPrice: item.unitPrice,
        partida: selectedLotDraft?.partida || deliveryOrderItemEffectivePartida(item, order),
        partidaExpirationDate: selectedLotDraft?.partidaExpirationDate || item.partidaExpirationDate || null,
        stockDepositCode: selectedLotDraft?.stockDepositCode || item.stockDepositCode || null,
        stockAvailableQuantity: selectedLotDraft?.stockAvailableQuantity || item.stockAvailableQuantity || null,
        stockCheckedAt: selectedLotDraft?.stockCheckedAt || item.stockCheckedAt || null,
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

  const stockLots = Array.isArray(stock?.items) ? stock.items : [];

  return (
    <div className="rounded border border-amber-200 bg-amber-50 p-3">
      <div className="mb-2 text-sm font-medium text-amber-900">Asignar artículo Bejerman</div>
      <div className="relative">
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          onKeyDown={suppressEnterSubmit}
          className="h-9 w-full rounded border bg-white px-2"
          placeholder="Buscar artículo por código o descripción"
          disabled={disabled || saving}
        />
        {(loading || empty || results.length > 0) && (
          <div className="absolute z-30 mt-1 max-h-52 w-full overflow-y-auto rounded border bg-white shadow">
            {loading && <div className="px-3 py-2 text-sm text-gray-600">Buscando artículos...</div>}
            {!loading && empty && <div className="px-3 py-2 text-sm text-gray-600">Sin coincidencias en Bejerman.</div>}
            {!loading &&
              results.map((article) => (
                <button
                  key={article.id || article.code}
                  type="button"
                  onClick={() => chooseArticle(article)}
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
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-[130px_minmax(180px,1fr)_110px]">
        <input
          value={manualCode}
          onChange={(event) => {
            setManualCode(event.target.value);
            setManualRequiresPartida(null);
          }}
          onKeyDown={suppressEnterSubmit}
          onBlur={(event) => void loadStock(event.target.value)}
          className="h-9 rounded border bg-white px-2"
          placeholder="Código"
          disabled={disabled || saving}
        />
        <input value={manualName} onChange={(event) => setManualName(event.target.value)} onKeyDown={suppressEnterSubmit} className="h-9 rounded border bg-white px-2" placeholder="Descripción" disabled={disabled || saving} />
        <button type="button" onClick={saveArticle} disabled={disabled || saving} className="rounded border bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50">
          Guardar
        </button>
      </div>
      {error && <div className="mt-2 text-xs text-red-700">{error}</div>}
      {manualRequiresPartida === false && (
        <div className="mt-2 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
          No requiere partida en Bejerman.
        </div>
      )}
      {(stock || manualCode) && manualRequiresPartida !== false && (
        <div className="mt-3 rounded border border-amber-200 bg-white p-2">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="text-xs font-medium uppercase text-amber-900">Partidas disponibles</div>
            <div className="text-xs text-amber-800">Depósito {stock?.depositCode || "VAL"}</div>
          </div>
          {stock?.loading && <div className="text-xs text-amber-800">Consultando stock...</div>}
          {!stock?.loading && stock?.warning && <div className="text-xs text-amber-800">{stock.warning}</div>}
          {!stock?.loading && stockLots.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {stockLots.map((lot) => (
                <button
                  key={`${lot.partida}-${lotExpiration(lot)}`}
                  type="button"
                  onClick={() => setSelectedLot(lot)}
                  className={`rounded border px-2 py-1 text-left text-xs hover:bg-amber-50 ${
                    selectedLot?.partida === lot.partida ? "border-amber-500 bg-amber-100" : "bg-white"
                  }`}
                  disabled={disabled || saving}
                >
                  {lotLabel(lot)}
                </button>
              ))}
            </div>
          )}
          {!stock?.loading && stock && !stock.warning && stockLots.length === 0 && (
            <div className="text-xs text-amber-800">Sin partidas con stock positivo en depósito VAL.</div>
          )}
        </div>
      )}
    </div>
  );
}

function PartidasEditor({ order, item, onUpdated, disabled }) {
  const initial = Array.isArray(item.partidas) && item.partidas.length
    ? item.partidas.map((partida) => ({
        ingresoId: partida.ingresoId == null ? "" : String(partida.ingresoId),
        deviceId: partida.deviceId == null ? "" : String(partida.deviceId),
        partida: clean(partida.partida),
        assignedQuantity: String(partida.assignedQuantity || item.quantity || 1),
        partidaExpirationDate: clean(partida.partidaExpirationDate),
        stockDepositCode: clean(partida.stockDepositCode),
        stockAvailableQuantity: partida.stockAvailableQuantity == null ? "" : String(partida.stockAvailableQuantity),
        stockCheckedAt: clean(partida.stockCheckedAt),
      }))
    : [
        {
          ingresoId: item?.ingresoId == null ? "" : String(item.ingresoId),
          deviceId: item?.deviceId == null ? "" : String(item.deviceId),
          partida: deliveryOrderItemEffectivePartida(item, order),
          assignedQuantity: String(item.quantity || 1),
          partidaExpirationDate: clean(item.partidaExpirationDate),
          stockDepositCode: clean(item.stockDepositCode),
          stockAvailableQuantity: item.stockAvailableQuantity == null ? "" : String(item.stockAvailableQuantity),
          stockCheckedAt: clean(item.stockCheckedAt),
        },
      ];
  const [rows, setRows] = useState(initial);
  const [stock, setStock] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const articleCode = clean(item.articleCode);
    if (!articleCode) {
      setStock(null);
      return undefined;
    }
    let active = true;
    setStock({ loading: true, items: [], warning: "", unavailable: false, depositCode: "VAL" });
    getDeliveryOrderBejermanArticleStock({
      articleCode,
      limit: 100,
      companyKey: deliveryOrderCompanyKey(order),
      deliveryType: order.deliveryType,
      depositCode: itemStockDeposit(order, item),
    })
      .then((data) => {
        if (!active) return;
        setStock({
          loading: false,
          items: Array.isArray(data?.items) ? data.items : [],
          warning: clean(data?.warning),
          unavailable: Boolean(data?.unavailable),
          depositCode: clean(data?.depositCode) || "VAL",
        });
      })
      .catch((err) => {
        if (!active) return;
        setStock({
          loading: false,
          items: [],
          warning: err?.message || "No se pudo verificar stock Bejerman.",
          unavailable: true,
          depositCode: "VAL",
        });
      });
    return () => {
      active = false;
    };
  }, [item.articleCode, item.stockDepositCode, order.deliveryType, order.companyKey, order.operationCompanyLabel, order.sourceCompanyId]);

  const updateRow = (index, changes) => {
    setRows((current) => current.map((row, rowIndex) => (rowIndex === index ? { ...row, ...changes } : row)));
  };

  const chooseStockLot = (lot) => {
    const available = lotAvailableQuantity(lot);
    const assignedQuantity = Math.min(Math.max(numberOf(item.quantity, 1), 1), available || Math.max(numberOf(item.quantity, 1), 1));
    const nextRow = lotDraft(lot, assignedQuantity);
    setRows((current) => {
      const firstEmptyIndex = current.findIndex((row) => !clean(row.partida));
      if (firstEmptyIndex < 0) return [...current, nextRow];
      return current.map((row, index) => (index === firstEmptyIndex ? nextRow : row));
    });
    setError("");
  };

  const completeTypedPartida = (index) => {
    const row = rows[index] || {};
    const partida = clean(row.partida);
    if (!partida || stock?.loading || stock?.unavailable || !stockLots.length) return;
    const matchingLot = stockLots.find((lot) => clean(lot?.partida).toLowerCase() === partida.toLowerCase());
    if (!matchingLot) {
      setError("La partida no figura para este artículo en el depósito seleccionado.");
      return;
    }
    updateRow(index, lotDraft(matchingLot, clean(row.assignedQuantity) || "1"));
    setError("");
  };

  const save = async () => {
    const partidas = rows
      .map((row, index) => ({
        ingresoId: clean(row.ingresoId) ? Number(row.ingresoId) : null,
        deviceId: clean(row.deviceId) ? Number(row.deviceId) : null,
        partida: clean(row.partida),
        assignedQuantity: numberOf(row.assignedQuantity, 0),
        partidaExpirationDate: clean(row.partidaExpirationDate) || null,
        stockDepositCode: clean(row.stockDepositCode) || itemStockDeposit(order, item) || null,
        stockAvailableQuantity: clean(row.stockAvailableQuantity) ? numberOf(row.stockAvailableQuantity, 0) : null,
        stockCheckedAt: clean(row.stockCheckedAt) || null,
        sortOrder: index,
      }))
      .filter((row) => row.partida && row.assignedQuantity > 0);
    if (!partidas.length) {
      setError("Cargue al menos una partida.");
      return;
    }
    const assigned = partidas.reduce((sum, partida) => sum + numberOf(partida.assignedQuantity, 0), 0);
    const quantity = numberOf(item.quantity, 1);
    if (Math.abs(assigned - quantity) > 0.0001) {
      setError(`La suma de las partidas debe ser igual a ${formatQuantity(quantity)}.`);
      return;
    }
    const availableStockLots = Array.isArray(stock?.items) ? stock.items : [];
    if (!stock?.loading && !stock?.unavailable && availableStockLots.length) {
      for (const partida of partidas) {
        const matchingLot = availableStockLots.find((lot) => clean(lot?.partida).toLowerCase() === partida.partida.toLowerCase());
        if (!matchingLot) {
          setError(`La partida ${partida.partida} no figura para este artículo en el depósito seleccionado.`);
          return;
        }
        if (numberOf(partida.assignedQuantity, 0) - lotAvailableQuantity(matchingLot) > 0.0001) {
          setError(`La partida ${partida.partida} no tiene stock suficiente en el depósito seleccionado.`);
          return;
        }
      }
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

  const stockLots = Array.isArray(stock?.items) ? stock.items : [];
  const partidaQueries = rows
    .map((row) => clean(row.partida).toLowerCase())
    .filter(Boolean);
  const visibleStockLots = partidaQueries.length
    ? stockLots.filter((lot) => partidaQueries.some((query) => lotLabel(lot).toLowerCase().includes(query)))
    : stockLots;
  const assigned = rows.reduce((sum, row) => sum + (clean(row.partida) ? numberOf(row.assignedQuantity, 0) : 0), 0);
  const quantity = numberOf(item.quantity, 1);
  const quantityMismatch = Math.abs(assigned - quantity) > 0.0001;

  return (
    <div className="rounded border border-sky-200 bg-sky-50 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium text-sky-900">Completar partidas</div>
        <div className={`text-xs ${quantityMismatch ? "text-red-700" : "text-sky-800"}`}>
          Asignado {formatQuantity(assigned)} / {formatQuantity(quantity)}
        </div>
      </div>
      {quantityMismatch && (
        <div className="mb-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          La suma de las cantidades por partida debe ser igual a la cantidad del artículo ({formatQuantity(quantity)}).
        </div>
      )}
      {clean(item.articleCode) && (
        <div className="mb-3 rounded border border-sky-200 bg-white p-2">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="text-xs font-medium uppercase text-sky-900">Partidas disponibles</div>
            <div className="text-xs text-sky-800">Depósito {stock?.depositCode || "VAL"}</div>
          </div>
          {stock?.loading && <div className="text-xs text-sky-800">Consultando stock...</div>}
          {!stock?.loading && stock?.warning && <div className="text-xs text-sky-800">{stock.warning}</div>}
          {!stock?.loading && visibleStockLots.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {visibleStockLots.map((lot) => (
                <button
                  key={`${lot.partida}-${lotExpiration(lot)}`}
                  type="button"
                  onClick={() => chooseStockLot(lot)}
                  className="rounded border bg-white px-2 py-1 text-left text-xs hover:bg-sky-50 disabled:opacity-50"
                  disabled={disabled || saving}
                >
                  {lotLabel(lot)}
                </button>
              ))}
            </div>
          )}
          {!stock?.loading && stockLots.length > 0 && visibleStockLots.length === 0 && (
            <div className="text-xs text-sky-800">Sin coincidencias para el filtro ingresado.</div>
          )}
          {!stock?.loading && stock && !stock.warning && stockLots.length === 0 && (
            <div className="text-xs text-sky-800">Sin partidas con stock positivo en depósito VAL.</div>
          )}
        </div>
      )}
      <div className="space-y-2">
        {rows.map((row, index) => (
          <div key={index} className="grid grid-cols-1 gap-2 lg:grid-cols-[minmax(150px,1fr)_90px_130px_90px_38px]">
            <input
              value={row.partida}
              onChange={(event) => updateRow(index, { partida: event.target.value })}
              onKeyDown={suppressEnterSubmit}
              onBlur={() => completeTypedPartida(index)}
              className="h-9 w-full rounded border bg-white px-2"
              placeholder="Partida"
              disabled={disabled || saving}
            />
            <input value={row.assignedQuantity} onChange={(event) => updateRow(index, { assignedQuantity: event.target.value })} onKeyDown={suppressEnterSubmit} className={`h-9 rounded border bg-white px-2 ${quantityMismatch && clean(row.partida) ? "border-red-400 text-red-900 focus:outline-red-500" : ""}`} placeholder="Cantidad" disabled={disabled || saving} />
            <input type="date" value={row.partidaExpirationDate} onChange={(event) => updateRow(index, { partidaExpirationDate: event.target.value })} className="h-9 rounded border bg-white px-2" disabled={disabled || saving} />
            <input value={row.stockAvailableQuantity} onChange={(event) => updateRow(index, { stockAvailableQuantity: event.target.value })} onKeyDown={suppressEnterSubmit} className="h-9 rounded border bg-white px-2" placeholder="Stock" disabled={disabled || saving} />
            <button type="button" onClick={() => setRows((current) => current.filter((_, rowIndex) => rowIndex !== index))} className="rounded border bg-white px-2 text-sm disabled:opacity-40" disabled={disabled || saving || rows.length <= 1}>
              X
            </button>
          </div>
        ))}
      </div>
      <div className="mt-2 flex justify-between gap-2">
        <button type="button" onClick={() => setRows((current) => [...current, { ingresoId: "", deviceId: "", partida: "", assignedQuantity: "1", partidaExpirationDate: "", stockDepositCode: "", stockAvailableQuantity: "", stockCheckedAt: "" }])} className="rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-50" disabled={disabled || saving}>
          Agregar partida
        </button>
        <button type="button" onClick={save} className="rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50" disabled={disabled || saving || quantityMismatch}>
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
  const [progressOpen, setProgressOpen] = useState(false);
  const [progressTitle, setProgressTitle] = useState("Emitiendo remito");
  const [progressStatus, setProgressStatus] = useState("Emitiendo remito en Bejerman");
  const [progressDetail, setProgressDetail] = useState("NEXORA está trabajando. El PDF se abrirá cuando esté listo.");
  const [manualRemitoPrint, setManualRemitoPrint] = useState(null);

  useEffect(() => {
    if (!open) return;
    setLocalOrders(orders || []);
    setNotes("");
    setError("");
    setProgressOpen(false);
    setProgressTitle("Emitiendo remito");
    setManualRemitoPrint(null);
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
    const startedAt = Date.now();
    const documentLabel = remitoDocumentFromOrders(localOrders);
    const progressLabel = remitoProgressFromOrders(localOrders);
    setSaving(true);
    setError("");
    setManualRemitoPrint(null);
    setProgressTitle(`Emitiendo ${documentLabel}`);
    setProgressStatus(`Emitiendo ${progressLabel} en Bejerman`);
    setProgressDetail("Enviando el remito a Bejerman.");
    setProgressOpen(true);
    await waitForRisProgressPaint();
    let result = null;
    try {
      result = await postDeliveryOrderBejermanRemito({
        orderIds: localOrders.map((order) => order.id),
        notes,
      });
      setProgressStatus(`${documentLabel} emitido`);
      setProgressDetail("Esperando el PDF publicado por Bejerman.");
      const pdfUrl = remitoPdfUrlFromResult(result);
      if (!pdfUrl) {
        setError(`Remito ${result?.remitoNumber || ""} emitido, pero no se recibió una URL de PDF.`);
        onGenerated(result, { keepOpen: true });
        return;
      }
      let blob = null;
      try {
        blob = await waitForPdfBlob(pdfUrl, {
          label: documentLabel,
          onProgress: (progress) => {
            setProgressStatus(progress?.status || `Preparando PDF del ${documentLabel}...`);
            setProgressDetail(progress?.detail || "Esperando el archivo de Bejerman.");
          },
        });
      } catch (pdfError) {
        setError(
          `Remito ${result?.remitoNumber || ""} emitido, pero Bejerman no pudo devolver el PDF. ${
            pdfError?.message || "Reintente imprimirlo desde el historial de pedidos."
          }`
        );
        onGenerated(result, { keepOpen: true });
        return;
      }
      setProgressStatus("Abriendo impresión");
      setProgressDetail("El PDF está listo. Intentando abrir la ventana de impresión.");
      await waitForRisProgressMinimum(startedAt);
      const opened = openRemitoPrintablePdf(blob, result, documentLabel).opened;
      if (opened) {
        onGenerated(result);
      } else {
        setManualRemitoPrint({ blob, result, documentLabel });
        setError(`Remito ${result?.remitoNumber || ""} emitido. El PDF está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir.`);
        onGenerated(result, { keepOpen: true });
      }
    } catch (err) {
      setError(
        result?.remitoNumber
          ? `Remito ${result.remitoNumber} emitido, pero ocurrió un error posterior. ${err?.message || ""}`.trim()
          : err?.message || "No se pudo generar el remito Bejerman."
      );
    } finally {
      setProgressOpen(false);
      setSaving(false);
    }
  };

  const abrirRemitoManualPendiente = () => {
    if (!manualRemitoPrint?.blob) return;
    const opened = openRemitoPrintablePdf(
      manualRemitoPrint.blob,
      manualRemitoPrint.result || {},
      manualRemitoPrint.documentLabel || remitoDocumentFromOrders(localOrders),
    ).opened;
    if (opened) {
      setManualRemitoPrint(null);
      setError("");
      onGenerated(manualRemitoPrint.result);
    } else {
      setError("El navegador volvió a bloquear la ventana. Habilite ventanas emergentes para NEXORA y reintente.");
    }
  };

  return (
    <>
      <RisProgressModal
        open={progressOpen}
        title={progressTitle}
        status={progressStatus}
        detail={progressDetail}
      />
      <ResponsiveModalOverlay role="dialog" aria-modal="true" onClick={onClose}>
        <ResponsiveModalPanel className="max-w-6xl overflow-hidden" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-4 border-b px-4 py-3 sm:px-5 sm:py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Generar remito Bejerman</h2>
            <p className="text-sm text-gray-600">Complete artículos y partidas antes de emitir.</p>
          </div>
          <button type="button" className="text-sm text-gray-500 hover:text-gray-900" onClick={onClose}>
            Cerrar
          </button>
        </div>

        <div className="max-h-[calc(100dvh-9rem)] overflow-y-auto p-3 sm:p-5">
          {issues.length > 0 && (
            <div className="mb-4 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              {issues.map((issue) => (
                <div key={issue}>{issue}</div>
              ))}
            </div>
          )}
          {error && (
            <div className="mb-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              <div>{error}</div>
              {manualRemitoPrint?.blob && (
                <button
                  type="button"
                  onClick={abrirRemitoManualPendiente}
                  className="mt-2 rounded bg-red-700 px-3 py-1.5 font-medium text-white hover:bg-red-800"
                >
                  Abrir e imprimir {manualRemitoPrint?.documentLabel || remitoDocumentFromOrders(localOrders)}
                </button>
              )}
            </div>
          )}

          <div className="space-y-4">
            {localOrders.map((order) => {
              const issuesForOrder = orderIssues(order);
              return (
                <section key={order.id} className="rounded border">
                  <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-gray-50 px-3 py-2">
                    <div>
                      <div className="font-medium text-gray-900">{order.orderNumber}</div>
                      <div className="text-xs text-gray-600">
                        {[order.customerName, order.bejermanCustomerCode || "sin código", deliveryOrderCompanyLabel(order), deliveryOrderSourceLabel(order)].filter(Boolean).join(" - ")}
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
                        <div className="grid grid-cols-1 gap-2 text-sm lg:grid-cols-[120px_minmax(180px,1fr)_90px_130px]">
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
                            <div>{deliveryOrderItemPartidaLabel(item, order, { includePartidaLabel: false }) || "-"}</div>
                          </div>
                        </div>
                        {!clean(item.articleCode) && canAssignArticles && (
                          <ArticleEditor order={order} item={item} onUpdated={updateOrder} disabled={saving} />
                        )}
                        {clean(item.articleCode) && canAssignArticles && !deliveryOrderItemCanOmitPartida(order, item) && (itemNeedsPartidas(order, item) || !deliveryOrderItemPartidaLabel(item, order)) && (
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
        </ResponsiveModalPanel>
      </ResponsiveModalOverlay>
    </>
  );
}
