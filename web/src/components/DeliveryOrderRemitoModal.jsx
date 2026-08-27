import { useEffect, useMemo, useRef, useState } from "react";
import {
  bejermanLookupErrorMessage,
  getDeliveryOrder,
  getDeliveryOrderBejermanArticleStock,
  getDeliveryOrderBejermanArticles,
  patchDeliveryOrderItemArticle,
  patchDeliveryOrderItemPartidas,
  getDeliveryOrderBejermanRemitoJob,
  postDeliveryOrderBejermanRemito,
  postDeliveryOrderBejermanRemitoPreflight,
} from "../lib/api";
import {
  forgetRemitoJob,
  rememberRemitoJob,
  waitForRemitoJob,
} from "../lib/delivery-remito-job";
import {
  deliveryOrderBejermanCustomerLabel,
  deliveryOrderEquipmentContext,
  deliveryOrderCompanyKey,
  deliveryOrderCompanyLabel,
  deliveryOrderIsOccasionalCustomer,
  deliveryOrderOccasionalCustomerReady,
  deliveryOrderItemCanOmitPartida,
  deliveryOrderItemEffectivePartida,
  deliveryOrderItemLabel,
  deliveryOrderItemPartidaLabel,
  deliveryOrderItemRequiresPartida,
  deliveryOrderSourceLabel,
} from "../lib/delivery-orders";
import { openPrintablePdf, reservePdfWindow, waitForPdfBlob } from "../lib/pdf";
import RisProgressModal, {
  waitForRisProgressMinimum,
  waitForRisProgressPaint,
} from "./RisProgressModal.jsx";
import { ResponsiveModalOverlay, ResponsiveModalPanel } from "./Responsive.jsx";

const clean = (value) => String(value || "").trim();

const SYNC_CHECK_IDLE = { loading: false, retrying: false, blocked: false, title: "", message: "", jobs: [] };
const SYNC_CHECK_DEFAULT_TITLE = "No se puede emitir todavía: falta sincronizar el stock.";

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

function remitoGroupDetail(group) {
  const summary = group?.responseSummary && typeof group.responseSummary === "object" ? group.responseSummary : {};
  return (
    clean(group?.lastError) ||
    clean(summary.error) ||
    clean(summary.detail) ||
    clean(summary.message) ||
    clean(summary.originalEmitError)
  );
}

function remitoGroupIssue(order) {
  if (clean(order?.remitoSetStatus).toLowerCase() === "partial") return "";
  if (!clean(order?.bejermanRemitoGroupId)) return "";
  const group = order?.bejermanRemitoGroup && typeof order.bejermanRemitoGroup === "object" ? order.bejermanRemitoGroup : {};
  const status = clean(group.status).toLowerCase();
  const label = clean(order?.orderNumber) || "La orden";
  const detail = remitoGroupDetail(group);
  if (status === "failed_review") {
    return `${label}: la emisión Bejerman quedó en revisión${detail ? `: ${detail}` : "."}`;
  }
  if (status === "sent_unknown") {
    return `${label}: Bejerman no confirmó si emitió el remito${detail ? `: ${detail}` : "."}`;
  }
  if (status === "failed") {
    return `${label}: la emisión Bejerman falló${detail ? `: ${detail}` : "."}`;
  }
  if (status === "reserved" || status === "sending" || status === "pending" || !status) {
    return `${label}: hay una emisión Bejerman en curso.`;
  }
  return `${label}: ya tiene una emisión Bejerman asociada${detail ? `: ${detail}` : "."}`;
}

// Espejo de bejerman_delivery._validate_remito_orders: sin código Bejerman la única emisión
// posible es la de cliente ocasional (el código especial @@@#@@), que es exclusiva de los
// particulares y exige el mismo nombre y documento de propietario en todas las órdenes.
function missingCustomerCodeIssue(orders) {
  // Un cliente con nombre propio y sin código no entra por el camino ocasional: hay que
  // vincularlo con Bejerman. Nombrarlo evita el mensaje sobre "todas las órdenes" cuando hay una sola.
  const unmapped = orders.filter((order) => !deliveryOrderIsOccasionalCustomer(order));
  if (unmapped.length) {
    const names = Array.from(new Set(unmapped.map((order) => clean(order.customerName)).filter(Boolean)));
    if (!names.length) {
      return "Hay órdenes cuyo cliente no tiene código Bejerman: vincúlelo con Bejerman antes de emitir el remito.";
    }
    return `${names.join(", ")}: el cliente no tiene código Bejerman. Vincúlelo con Bejerman antes de emitir el remito.`;
  }
  if (!orders.every((order) => deliveryOrderOccasionalCustomerReady(order))) {
    return "El particular se emite como cliente ocasional: cargue nombre y documento del propietario en el ingreso.";
  }
  const names = new Set(orders.map((order) => clean(order.propietarioNombre)));
  const docs = new Set(orders.map((order) => clean(order.propietarioDoc)));
  if (names.size !== 1 || docs.size !== 1) {
    return "Las órdenes de particulares deben corresponder al mismo propietario (mismo nombre y documento).";
  }
  return "";
}

function compatibilityIssues(orders) {
  if (!orders.length) return ["Seleccione al menos una orden."];
  const customerCodes = new Set(orders.map((order) => clean(order.bejermanCustomerCode)));
  const types = new Set(orders.map((order) => clean(order.deliveryType)));
  const companyKeys = new Set(orders.map((order) => deliveryOrderCompanyKey(order)));
  const issues = [];
  if (customerCodes.size !== 1) {
    issues.push("Las órdenes seleccionadas son de clientes distintos: emita un remito por cliente.");
  } else if (!Array.from(customerCodes)[0]) {
    const missingCodeIssue = missingCustomerCodeIssue(orders);
    if (missingCodeIssue) issues.push(missingCodeIssue);
  }
  if (types.size !== 1) issues.push("Todas las órdenes deben tener el mismo tipo de remito.");
  if (companyKeys.size !== 1) issues.push("Todas las órdenes deben pertenecer a la misma empresa Bejerman.");
  if (orders.some((order) => order.status === "pendiente_stock")) {
    issues.push("Las órdenes pendientes de stock deben pasar a pendiente de armado antes de emitir remito.");
  }
  if (orders.some((order) => (
    order.remitoNumber && clean(order.remitoSetStatus).toLowerCase() !== "partial"
  ) || order.status === "facturado" || order.status === "cancelado")) {
    issues.push("No se pueden emitir órdenes cerradas, canceladas o con remito.");
  }
  orders.map(remitoGroupIssue).filter(Boolean).forEach((issue) => issues.push(issue));
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

function remitoPrintableOptions(result, documentLabel = "remito") {
  const remito = clean(result?.remitoNumber);
  const label = clean(documentLabel) || "remito";
  return {
    title: remito ? `${label} ${remito}` : label,
    documentLabel: remito ? `${label} ${remito}` : label,
  };
}

function openRemitoPrintablePdf(blob, result, documentLabel = "remito") {
  return openPrintablePdf(blob, remitoPrintableOptions(result, documentLabel));
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
        setError(bejermanLookupErrorMessage(err, "No se pudieron consultar los artículos de Bejerman."));
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
      const unavailable = Boolean(data?.unavailable);
      setStock({
        loading: false,
        items: Array.isArray(data?.items) ? data.items : [],
        warning: clean(data?.warning) || (
          unavailable ? "No se pudieron consultar las partidas de Bejerman. Reintentá en unos minutos." : ""
        ),
        unavailable,
        depositCode: clean(data?.depositCode) || "VAL",
      });
    } catch (err) {
      setStock({
        loading: false,
        items: [],
        warning: bejermanLookupErrorMessage(err, "No se pudieron consultar las partidas de Bejerman."),
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

function ArticleStockBadge({ order, item }) {
  const [stock, setStock] = useState(null);
  const articleCode = clean(item.articleCode);
  const requestedQuantity = numberOf(item.quantity, 1);

  useEffect(() => {
    if (!articleCode) {
      setStock(null);
      return undefined;
    }
    let active = true;
    setStock({ loading: true });
    getDeliveryOrderBejermanArticleStock({
      articleCode,
      limit: 1,
      companyKey: deliveryOrderCompanyKey(order),
      deliveryType: order.deliveryType,
      depositCode: itemStockDeposit(order, item),
    })
      .then((data) => {
        if (!active) return;
        const unavailable = Boolean(data?.unavailable);
        const available = unavailable
          ? null
          : numberOf(data?.totalAvailableQuantity ?? data?.items?.[0]?.stockAvailableQuantity, 0);
        setStock({
          loading: false,
          available,
          warning: clean(data?.warning) || (
            unavailable ? "No se pudo verificar el stock de Bejerman. Reintentá en unos minutos." : ""
          ),
        });
      })
      .catch((err) => {
        if (!active) return;
        setStock({
          loading: false,
          available: null,
          warning: bejermanLookupErrorMessage(err, "No se pudo verificar el stock de Bejerman."),
        });
      });
    return () => {
      active = false;
    };
  }, [articleCode, item.stockDepositCode, order.deliveryType, order.companyKey, order.operationCompanyLabel, order.sourceCompanyId]);

  if (!articleCode || !stock || stock.loading) return null;
  if (stock.available == null) {
    return <div className="text-xs text-amber-800">{stock.warning || "No se pudo verificar stock Bejerman."}</div>;
  }
  const insufficient = requestedQuantity - stock.available > 0.0001;
  return (
    <div className={`text-xs ${insufficient ? "text-red-700" : "text-gray-500"}`}>
      Stock Bejerman disponible: {formatQuantity(stock.available)}
      {insufficient && ` — pediste ${formatQuantity(requestedQuantity)}, no alcanza`}
    </div>
  );
}

function savedPartidaRows(order, item) {
  if (Array.isArray(item.partidas) && item.partidas.length) {
    return item.partidas.map((partida) => ({
      ingresoId: partida.ingresoId == null ? "" : String(partida.ingresoId),
      deviceId: partida.deviceId == null ? "" : String(partida.deviceId),
      partida: clean(partida.partida),
      assignedQuantity: String(partida.assignedQuantity || item.quantity || 1),
      partidaExpirationDate: clean(partida.partidaExpirationDate),
      stockDepositCode: clean(partida.stockDepositCode),
      stockAvailableQuantity: partida.stockAvailableQuantity == null ? "" : String(partida.stockAvailableQuantity),
      stockCheckedAt: clean(partida.stockCheckedAt),
    }));
  }
  return [
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
}

// Lo escrito en el editor no vale hasta que se guarda: el resumen "Asignado x / y" contaba
// las filas locales y daba por completo un renglón que para la emisión seguía vacío.
function partidaRowsFingerprint(rows) {
  return (rows || [])
    .filter((row) => clean(row?.partida))
    .map((row) => `${clean(row.partida).toLowerCase()}:${numberOf(row?.assignedQuantity, 0)}`)
    .join("|");
}

function PartidasEditor({ order, item, onUpdated, disabled }) {
  const [rows, setRows] = useState(() => savedPartidaRows(order, item));
  const [stock, setStock] = useState(null);
  const [stockReloadKey, setStockReloadKey] = useState(0);
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
        const unavailable = Boolean(data?.unavailable);
        setStock({
          loading: false,
          items: Array.isArray(data?.items) ? data.items : [],
          warning: clean(data?.warning) || (
            unavailable ? "No se pudieron consultar las partidas de Bejerman. Reintentá en unos minutos." : ""
          ),
          unavailable,
          depositCode: clean(data?.depositCode) || "VAL",
        });
      })
      .catch((err) => {
        if (!active) return;
        setStock({
          loading: false,
          items: [],
          warning: bejermanLookupErrorMessage(err, "No se pudieron consultar las partidas de Bejerman."),
          unavailable: true,
          depositCode: "VAL",
        });
      });
    return () => {
      active = false;
    };
  }, [item.articleCode, item.stockDepositCode, order.deliveryType, order.companyKey, order.operationCompanyLabel, order.sourceCompanyId, stockReloadKey]);

  const updateRow = (index, changes) => {
    setRows((current) => current.map((row, rowIndex) => (rowIndex === index ? { ...row, ...changes } : row)));
  };

  // Elegir la partida del stock ya deja el renglón completo: guardarla sola evita que la
  // emisión quede bloqueada por filas cargadas pero nunca confirmadas.
  const applyRows = (nextRows) => {
    setRows(nextRows);
    setError("");
    const assignedTotal = nextRows.reduce(
      (sum, row) => sum + (clean(row.partida) ? numberOf(row.assignedQuantity, 0) : 0),
      0,
    );
    if (Math.abs(assignedTotal - numberOf(item.quantity, 1)) <= 0.0001) {
      void save(nextRows);
    }
  };

  const chooseStockLot = (lot) => {
    const available = lotAvailableQuantity(lot);
    const assignedQuantity = Math.min(Math.max(numberOf(item.quantity, 1), 1), available || Math.max(numberOf(item.quantity, 1), 1));
    const nextRow = lotDraft(lot, assignedQuantity);
    const firstEmptyIndex = rows.findIndex((row) => !clean(row.partida));
    applyRows(
      firstEmptyIndex < 0
        ? [...rows, nextRow]
        : rows.map((row, index) => (index === firstEmptyIndex ? nextRow : row)),
    );
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
    const nextRow = lotDraft(matchingLot, clean(row.assignedQuantity) || "1");
    applyRows(rows.map((current, rowIndex) => (rowIndex === index ? nextRow : current)));
  };

  const save = async (rowsToSave = rows) => {
    const partidas = rowsToSave
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
  const savedRows = savedPartidaRows(order, item);
  const savedFingerprint = partidaRowsFingerprint(savedRows);
  const pendingSave = partidaRowsFingerprint(rows) !== savedFingerprint;

  return (
    <div className="rounded border border-sky-200 bg-sky-50 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium text-sky-900">Completar partidas</div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {pendingSave ? (
            <span className="rounded border border-amber-300 bg-amber-100 px-2 py-0.5 font-medium text-amber-900">
              Sin guardar
            </span>
          ) : savedFingerprint ? (
            <span className="rounded border border-emerald-300 bg-emerald-50 px-2 py-0.5 font-medium text-emerald-800">
              Guardado
            </span>
          ) : null}
          <span className={quantityMismatch ? "text-red-700" : "text-sky-800"}>
            Asignado {formatQuantity(assigned)} / {formatQuantity(quantity)}
          </span>
        </div>
      </div>
      {pendingSave && !saving && !quantityMismatch && (
        <div className="mb-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          Las partidas cargadas todavía no están guardadas: use Guardar partidas para que la emisión las tome.
        </div>
      )}
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
          {!stock?.loading && stock?.warning && (
            <div className="flex flex-wrap items-center gap-2 text-xs text-sky-800">
              <span>{stock.warning}</span>
              {stock.unavailable && (
                <button
                  type="button"
                  onClick={() => setStockReloadKey((current) => current + 1)}
                  className="rounded border border-sky-300 bg-white px-2 py-0.5 font-medium text-sky-800 hover:bg-sky-50"
                  disabled={disabled || saving}
                >
                  Reintentar
                </button>
              )}
            </div>
          )}
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
        <button
          type="button"
          onClick={() => void save()}
          className={`rounded border px-3 py-1.5 text-sm disabled:opacity-50 ${
            pendingSave ? "border-sky-600 bg-sky-600 font-medium text-white hover:bg-sky-700" : "bg-white hover:bg-gray-50"
          }`}
          disabled={disabled || saving || quantityMismatch}
        >
          {saving ? "Guardando..." : "Guardar partidas"}
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
  const [manualRemitoPrints, setManualRemitoPrints] = useState([]);
  const [pdfWait, setPdfWait] = useState({ startedAt: 0, remito: "" });
  const [syncCheck, setSyncCheck] = useState(SYNC_CHECK_IDLE);
  const pdfWaitAbortRef = useRef(null);
  const orderSelectionKey = (orders || []).map((order) => order?.id).filter(Boolean).join("|");

  useEffect(() => {
    if (!open) return;
    setLocalOrders(orders || []);
    setNotes("");
    setError("");
    setProgressOpen(false);
    setProgressTitle("Emitiendo remito");
    setManualRemitoPrints([]);
  }, [open, orderSelectionKey]);

  // El stock pendiente de sincronizar se avisa al abrir: enterarse recién al
  // apretar "Generar remito", después de esperar el envío a Bejerman, era la
  // peor forma posible de dar la noticia.
  useEffect(() => {
    if (!open) return undefined;
    let cancelado = false;
    setSyncCheck({ ...SYNC_CHECK_IDLE, loading: true });
    postDeliveryOrderBejermanRemitoPreflight({
      orderIds: (orders || []).map((order) => order?.id).filter(Boolean),
    })
      .then((res) => {
        if (cancelado) return;
        setSyncCheck({
          ...SYNC_CHECK_IDLE,
          blocked: res?.canEmit === false,
          title: clean(res?.title),
          message: clean(res?.message),
          jobs: Array.isArray(res?.blockingSyncJobs) ? res.blockingSyncJobs : [],
        });
      })
      .catch(() => {
        // No poder consultar la cola no es motivo para trabar el modal: la
        // emisión vuelve a controlarlo del lado del servidor.
        if (!cancelado) setSyncCheck(SYNC_CHECK_IDLE);
      });
    return () => {
      cancelado = true;
    };
  }, [open, orderSelectionKey]);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  const issues = useMemo(() => compatibilityIssues(localOrders), [localOrders]);
  const canGenerate =
    issues.length === 0 && !saving && !syncCheck.loading && !syncCheck.retrying && !syncCheck.blocked;

  if (!open) return null;

  const updateOrder = (updated) => {
    setLocalOrders((current) => replaceOrder(current, updated));
  };

  const reintentarSincronizacion = async () => {
    setSyncCheck((current) => ({ ...current, retrying: true }));
    try {
      const res = await postDeliveryOrderBejermanRemitoPreflight({
        orderIds: localOrders.map((order) => order.id),
        retry: true,
      });
      setSyncCheck({
        ...SYNC_CHECK_IDLE,
        blocked: res?.canEmit === false,
        title: clean(res?.title),
        message: clean(res?.message),
        jobs: Array.isArray(res?.blockingSyncJobs) ? res.blockingSyncJobs : [],
      });
    } catch (err) {
      setSyncCheck((current) => ({
        ...current,
        retrying: false,
        message: clean(err?.data?.detail) || err?.message || current.message,
      }));
    }
  };

  const generate = async () => {
    const startedAt = Date.now();
    const documentLabel = remitoDocumentFromOrders(localOrders);
    const progressLabel = remitoProgressFromOrders(localOrders);
    // Debe ocurrir dentro del click que confirma la emisión. Si se abre después
    // de esperar el PDF, los navegadores lo interpretan como un popup no pedido.
    const reservedPrintWindow = reservePdfWindow({
      title: `${documentLabel} - NEXORA`,
      heading: `Emitiendo ${documentLabel}`,
      message: `Emitiendo ${progressLabel} en Bejerman`,
      detail: "NEXORA está enviando el remito a Bejerman. Esta pestaña abre la impresión cuando el PDF esté listo.",
      fallbackMessage:
        `NEXORA sigue preparando el PDF del ${documentLabel}. Aguarde un poco mas. No se ansioso.`,
    });
    // El modal y la pestaña de impresión cuentan lo mismo: el operador mira una
    // o la otra según dónde tenga el foco, y las dos tienen que decir la verdad.
    const avisar = (status, detail, patch = {}) => {
      setProgressStatus(status);
      if (detail) setProgressDetail(detail);
      reservedPrintWindow.update({ status, ...(detail ? { detail } : {}), ...patch });
    };
    let printWindowUsed = false;
    // Se recuerda el motivo para poder contárselo también a la pestaña de
    // impresión: cerrarla sin decir nada deja al operador sin ninguna noticia.
    let failureText = "";
    const fallar = (text) => {
      failureText = clean(text);
      setError(text);
    };
    setSaving(true);
    setError("");
    setManualRemitoPrints([]);
    setProgressTitle(`Emitiendo ${documentLabel}`);
    avisar(`Emitiendo ${progressLabel} en Bejerman`, "NEXORA está enviando el remito a Bejerman.", { step: "emit" });
    setProgressOpen(true);
    await waitForRisProgressPaint();
    let result = null;
    try {
      const orderIds = localOrders.map((order) => order.id);
      // El POST ya no espera a Bejerman: deja anotado el pedido y vuelve. El trabajo
      // corre en el servidor, así que si el navegador congela esta pestaña (pasa
      // apenas el operador mira la ventana de impresión) la emisión sigue igual y el
      // primer sondeo al volver trae el resultado.
      const job = await postDeliveryOrderBejermanRemito({ orderIds, notes });
      rememberRemitoJob(job?.jobId, orderIds);
      result = await waitForRemitoJob(job?.jobId, {
        fetchJob: getDeliveryOrderBejermanRemitoJob,
        onProgress: (progress) => {
          const segundos = Number(progress?.serverSeconds) || 0;
          avisar(
            `Emitiendo ${progressLabel} en Bejerman`,
            segundos > 20
              ? `NEXORA sigue emitiendo el remito en Bejerman (${segundos}s). Puede cambiar de pestaña: la emisión no se interrumpe.`
              : "NEXORA está enviando el remito a Bejerman.",
            { step: "emit" },
          );
        },
      });
      forgetRemitoJob();
      let refreshedOrders = Array.isArray(result?.orders)
        ? result.orders.filter((order) => order?.id)
        : result?.order?.id
          ? [result.order]
          : [];
      if (result?.success === false && !refreshedOrders.length) {
        try {
          refreshedOrders = await Promise.all(localOrders.map((order) => getDeliveryOrder(order.id)));
          result = { ...result, orders: refreshedOrders };
        } catch (_) {
          // El error de emisión original sigue siendo el dato prioritario para el operador.
        }
      }
      if (refreshedOrders.length) {
        const refreshedById = new Map(refreshedOrders.map((order) => [order.id, order]));
        setLocalOrders((current) => current.map((order) => refreshedById.get(order.id) || order));
      }
      const partialFailureMessage = result?.success === false
        ? clean(typeof result?.error === "string" ? result.error : result?.error?.message) ||
          "Algunos remitos no se pudieron emitir. Reintente solamente los pendientes."
        : "";
      avisar(`${documentLabel} emitido`, "NEXORA está preparando el PDF para imprimir.", {
        step: "pdf",
        documentLabel: clean(result?.remitoNumber) ? `Remito ${clean(result.remitoNumber)}` : "",
      });
      // La emisión puede dividirse en varios remitos si no entran en una hoja.
      // En un reintento, el resumen conserva todo el conjunto, pero sólo deben
      // abrirse los documentos que se emitieron en esta llamada.
      const hasNewRemitos = Array.isArray(result?.newRemitos);
      const subResults = hasNewRemitos
        ? result.newRemitos
        : Array.isArray(result?.remitos) && result.remitos.length
          ? result.remitos
          : [result];
      const printJobs = [];
      const missingPdfs = [];
      for (let index = 0; index < subResults.length; index += 1) {
        const subResult = subResults[index];
        const subLabel = subResults.length > 1 ? `${documentLabel} ${index + 1} de ${subResults.length}` : documentLabel;
        const pdfUrl = remitoPdfUrlFromResult(subResult);
        if (!pdfUrl) {
          missingPdfs.push(clean(subResult?.remitoNumber) || subLabel);
          continue;
        }
        try {
          // El remito ya está emitido: sólo falta el archivo, y esta espera se
          // puede cancelar sin dejar nada a medias.
          const controller = new AbortController();
          pdfWaitAbortRef.current = controller;
          setPdfWait({ startedAt: Date.now(), remito: clean(subResult?.remitoNumber) });
          reservedPrintWindow.update({
            step: "pdf",
            documentLabel: clean(subResult?.remitoNumber) ? `Remito ${clean(subResult.remitoNumber)}` : "",
          });
          const blob = await waitForPdfBlob(pdfUrl, {
            label: subLabel,
            signal: controller.signal,
            reservedWindow: reservedPrintWindow,
            onProgress: (progress) => {
              setProgressStatus(progress?.status || `Preparando PDF del ${subLabel}...`);
              setProgressDetail(progress?.detail || "NEXORA está preparando el archivo.");
            },
          });
          printJobs.push({ blob, result: subResult, documentLabel: subLabel });
        } catch (pdfError) {
          missingPdfs.push(clean(subResult?.remitoNumber) || subLabel);
        } finally {
          pdfWaitAbortRef.current = null;
          setPdfWait({ startedAt: 0, remito: "" });
        }
      }
      if (!subResults.length) {
        fallar(partialFailureMessage || "No se emitieron remitos nuevos en este intento.");
        onGenerated(result, { keepOpen: true });
        return;
      }
      if (!printJobs.length) {
        fallar(
          partialFailureMessage ||
            (`Remito ${result?.remitoNumber || ""} emitido, pero NEXORA no pudo preparar el PDF. ` +
               "Reintente imprimirlo desde el historial de pedidos.")
        );
        onGenerated(result, { keepOpen: true });
        return;
      }
      avisar(
        "Abriendo impresión",
        printJobs.length > 1
          ? `Se emitieron ${subResults.length} remitos. Se abrirá el primero; imprima el resto desde los botones.`
          : "El PDF está listo. Intentando abrir la ventana de impresión.",
        { step: "print", state: "ready" },
      );
      await waitForRisProgressMinimum(startedAt);
      const [firstJob, ...restJobs] = printJobs;
      const opened = reservedPrintWindow.openPrintable(
        firstJob.blob,
        remitoPrintableOptions(firstJob.result, firstJob.documentLabel),
      ).opened;
      printWindowUsed = opened;
      const pendingJobs = opened ? restJobs : printJobs;
      if (partialFailureMessage) {
        setManualRemitoPrints(pendingJobs);
        const parts = [partialFailureMessage];
        if (!opened) parts.push("El navegador bloqueó el PDF del remito emitido. Use Abrir e imprimir.");
        if (opened && pendingJobs.length) parts.push(`Quedan ${pendingJobs.length} remito(s) emitidos por imprimir.`);
        if (missingPdfs.length) parts.push(`No se pudo obtener el PDF de: ${missingPdfs.join(" / ")}.`);
        fallar(parts.join(" "));
        onGenerated(result, { keepOpen: true });
        return;
      }
      if (!pendingJobs.length && !missingPdfs.length) {
        onGenerated(result);
      } else {
        setManualRemitoPrints(pendingJobs);
        const parts = [];
        if (!opened) {
          parts.push(`Remito ${result?.remitoNumber || ""} emitido. El navegador bloqueó la ventana automática. Use Abrir e imprimir.`);
        } else if (pendingJobs.length) {
          parts.push(`Se emitieron ${subResults.length} remitos (${result?.remitoNumber || ""}). Imprima los restantes con el botón de abajo.`);
        }
        if (missingPdfs.length) {
          parts.push(`No se pudo obtener el PDF de: ${missingPdfs.join(" / ")}. Reintente desde el historial de pedidos.`);
        }
        fallar(parts.join(" "));
        onGenerated(result, { keepOpen: true });
      }
    } catch (err) {
      fallar(
        result?.remitoNumber
          ? `Remito ${result.remitoNumber} emitido, pero ocurrió un error posterior. ${err?.message || ""}`.trim()
          : err?.message || "No se pudo generar el remito Bejerman."
      );
      if (err?.data?.code === "BEJERMAN_REMITO_BLOCKED_BY_SYNC") {
        setSyncCheck({
          ...SYNC_CHECK_IDLE,
          blocked: true,
          message: clean(err.data.detail) || err?.message || "",
          jobs: Array.isArray(err.data.blockingSyncJobs) ? err.data.blockingSyncJobs : [],
        });
      }
    } finally {
      if (!printWindowUsed) {
        // Si la emisión falló, la pestaña reservada muestra el motivo en vez de
        // cerrarse sola y dejar al operador sin saber qué pasó.
        const avisada = failureText
          ? reservedPrintWindow.update({
              state: "error",
              status: "No se pudo completar la emisión",
              detail: failureText,
            })
          : false;
        if (!avisada) reservedPrintWindow.close();
      }
      setProgressOpen(false);
      setSaving(false);
    }
  };

  const abrirRemitoManualPendiente = () => {
    const [job, ...restJobs] = manualRemitoPrints;
    if (!job?.blob) return;
    const opened = openRemitoPrintablePdf(
      job.blob,
      job.result || {},
      job.documentLabel || remitoDocumentFromOrders(localOrders),
    ).opened;
    if (!opened) {
      setError("El navegador volvió a bloquear la ventana. Habilite ventanas emergentes para NEXORA y reintente.");
      return;
    }
    setManualRemitoPrints(restJobs);
    if (restJobs.length) {
      setError(`Quedan ${restJobs.length} remito(s) por imprimir. Use Abrir e imprimir para el siguiente.`);
      onGenerated(job.result, { keepOpen: true });
    } else {
      setError("");
      onGenerated(job.result);
    }
  };

  return (
    <>
      <RisProgressModal
        open={progressOpen}
        title={progressTitle}
        status={progressStatus}
        detail={progressDetail}
        documentLabel={pdfWait.remito ? `Remito ${pdfWait.remito}` : ""}
        waitStartedAt={pdfWait.startedAt}
        onCancel={pdfWait.startedAt ? () => pdfWaitAbortRef.current?.abort() : undefined}
      />
      <ResponsiveModalOverlay role="dialog" aria-modal="true" align="center" onClick={onClose}>
        <ResponsiveModalPanel
          scroll={false}
          className="flex max-w-6xl flex-col"
          onClick={(event) => event.stopPropagation()}
        >
        <div className="flex shrink-0 items-start justify-between gap-4 border-b px-4 py-3 sm:px-5 sm:py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Generar remito Bejerman</h2>
            <p className="text-sm text-gray-600">Complete artículos y partidas antes de emitir.</p>
          </div>
          <button type="button" className="text-sm text-gray-500 hover:text-gray-900" onClick={onClose}>
            Cerrar
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3 sm:p-5">
          {syncCheck.loading && (
            <div className="mb-4 rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-600">
              Verificando que el stock esté sincronizado con Bejerman...
            </div>
          )}
          {syncCheck.blocked && (
            <div className="mb-4 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              <div className="font-medium">{syncCheck.title || SYNC_CHECK_DEFAULT_TITLE}</div>
              <div className="mt-1">{syncCheck.message}</div>
              {/* Reintentar sólo tiene sentido si lo que bloquea es un job de
                  sincronización: contra un faltante de stock no hace nada. */}
              {syncCheck.jobs.length > 0 && (
                <button
                  type="button"
                  onClick={reintentarSincronizacion}
                  disabled={syncCheck.retrying}
                  className="mt-2 rounded bg-amber-700 px-3 py-1.5 font-medium text-white hover:bg-amber-800 disabled:opacity-60"
                >
                  {syncCheck.retrying ? "Sincronizando..." : "Reintentar sincronización"}
                </button>
              )}
            </div>
          )}
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
              {manualRemitoPrints[0]?.blob && (
                <button
                  type="button"
                  onClick={abrirRemitoManualPendiente}
                  className="mt-2 rounded bg-red-700 px-3 py-1.5 font-medium text-white hover:bg-red-800"
                >
                  Abrir e imprimir {clean(manualRemitoPrints[0]?.result?.remitoNumber) || manualRemitoPrints[0]?.documentLabel || remitoDocumentFromOrders(localOrders)}
                  {manualRemitoPrints.length > 1 ? ` (${manualRemitoPrints.length} pendientes)` : ""}
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
                        {[
                          order.customerName,
                          deliveryOrderBejermanCustomerLabel(order) || "sin código",
                          deliveryOrderCompanyLabel(order),
                          deliveryOrderSourceLabel(order),
                        ]
                          .filter(Boolean)
                          .join(" - ")}
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
                        {clean(item.articleCode) && deliveryOrderItemCanOmitPartida(order, item) && (
                          <ArticleStockBadge order={order} item={item} />
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

        <div className="flex shrink-0 justify-end gap-2 border-t bg-white px-4 py-3 sm:px-5 sm:py-4">
          <button type="button" className="rounded border px-4 py-2 text-sm hover:bg-gray-50" onClick={onClose} disabled={saving}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn disabled:opacity-50"
            onClick={generate}
            disabled={!canGenerate}
            title={syncCheck.blocked ? syncCheck.message : undefined}
          >
            {saving ? "Generando..." : "Generar remito"}
          </button>
        </div>
        </ResponsiveModalPanel>
      </ResponsiveModalOverlay>
    </>
  );
}
