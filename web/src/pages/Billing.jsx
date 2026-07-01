import { useEffect, useMemo, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { AlertTriangle, CheckCircle2, FileText, Loader2, Search, X } from "lucide-react";
import {
  billingRemitoPdfUrl,
  getBillingCustomers,
  getBillingDocumentPdfBlob,
  getBillingDocuments,
  getBillingRemitos,
  getDeliveryOrder,
  getDeliveryOrders,
  postDeliveryOrderInvoiced,
  postDeliveryOrderNotBillable,
} from "../lib/api";
import { openPrintablePdf, reservePdfWindow, waitForPdfBlob } from "../lib/pdf";
import { remitoDocumentNumber } from "../lib/remitos";
import {
  deliveryOrderCommercialLabel,
  deliveryOrderCompanyLabel,
  deliveryOrderItemAmounts,
  deliveryOrderItemDiscountPercent,
  deliveryOrderItemLabel,
  deliveryOrderItemPartidaLabel,
  deliveryOrderItemUnitPrice,
  deliveryOrderItemsSummary,
  deliveryOrderItemsTotals,
  deliveryOrderItemPriceCurrency,
  deliveryOrderSourceLabel,
  formatOrderMoney,
  formatOrderQuantity,
  formatOrderTotalsAmount,
} from "../lib/delivery-orders";
import {
  DesktopTableWrap,
  MobileDataCard,
  MobileDataField,
  MobileDataList,
  ResponsiveModalOverlay,
  ResponsiveModalPanel,
} from "../components/Responsive.jsx";

const BILLABLE_REMITO_TYPES = new Set(["RT"]);
const BILLING_PAGE_SIZE = 25;
const EMPTY_PAGINATION = {
  page: 1,
  pageSize: BILLING_PAGE_SIZE,
  total: 0,
  totalPages: 1,
  hasNextPage: false,
  hasPreviousPage: false,
};
const BEJERMAN_COMPANY_OPTIONS = [
  { value: "SEPID", label: "SEPID SA" },
  { value: "MGBIO", label: "MG BIO" },
  { value: "TEST", label: "Empresa de prueba" },
];
const REMITO_TYPE_OPTIONS = [
  { value: "RT", label: "RT - Venta" },
  { value: "RTA", label: "RTA - Alquiler" },
  { value: "RTN", label: "RTN - Demo" },
  { value: "RSS", label: "RSS - Servicio técnico" },
  { value: "RIS", label: "RIS - Ingreso servicio" },
  { value: "RDA", label: "RDA - Retorno alquiler" },
  { value: "RDN", label: "RDN - Retorno demo" },
];
const REMITO_OPERATION_OPTIONS = [
  { value: "MC", label: "MC - Mercadería" },
  { value: "REP", label: "REP - Reparación" },
  { value: "ALQ", label: "ALQ - Alquiler" },
  { value: "DEMO", label: "DEMO - Demostración" },
  { value: "BUSO", label: "BUSO - Venta bien de uso" },
  { value: "FAB", label: "FAB - Fabricación" },
];
const REMITO_LOCATION_LABELS = {
  recepcion: "Recepción",
  oficina: "Oficina",
};

function openBlob(blob, fallbackName) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  anchor.download = fallbackName;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function clean(value) {
  return String(value ?? "").trim();
}

function initialOrderIdFromUrl() {
  try {
    return clean(new URLSearchParams(window.location.search).get("orderId"));
  } catch {
    return "";
  }
}

function initialServiceOrderIdFromUrl() {
  try {
    return clean(new URLSearchParams(window.location.search).get("serviceOrderId"));
  } catch {
    return "";
  }
}

function valueOf(item, keys) {
  for (const key of keys) {
    const value = item?.[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return "";
}

function formatAmount(value) {
  const raw = value === undefined || value === null ? "" : String(value).trim();
  if (!raw) return "-";
  const formatted = formatOrderMoney(raw);
  return formatted === "-" ? raw : formatted;
}

function formatDateTime(value) {
  const raw = clean(value);
  if (!raw) return "-";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString("es-AR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function pendingRemitoEmissionRaw(order) {
  return (
    clean(order?.bejermanRemitoGroup?.generatedAt) ||
    clean(order?.orderDate) ||
    clean(order?.bejermanRemitoGroup?.createdAt) ||
    clean(order?.deliveredAt) ||
    clean(order?.createdAt)
  );
}

function pendingRemitoEmissionTime(order) {
  const raw = pendingRemitoEmissionRaw(order);
  if (!raw) return Number.POSITIVE_INFINITY;
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? Number.POSITIVE_INFINITY : date.getTime();
}

function normalizePendingBillingOrders(items) {
  return [...items]
    .filter((order) => !clean(order?.invoiceNumber))
    .sort((a, b) => {
      const byDate = pendingRemitoEmissionTime(a) - pendingRemitoEmissionTime(b);
      if (byDate) return byDate;
      const byOrder = clean(a?.orderNumber).localeCompare(clean(b?.orderNumber), "es-AR", { numeric: true });
      if (byOrder) return byOrder;
      return clean(a?.id).localeCompare(clean(b?.id), "es-AR", { numeric: true });
    });
}

function orderMatchesServiceOrderId(order, serviceOrderId) {
  const requested = clean(serviceOrderId);
  if (!requested) return false;
  const numeric = /^\d+$/.test(requested) ? String(Number(requested)) : requested;
  return [order?.ingresoId, order?.sourceReference]
    .map((value) => clean(value))
    .some((value) => value === requested || value === numeric || value === `OS-${numeric.padStart(5, "0")}`);
}

function serviceOrderSearchQuery(serviceOrderId) {
  const requested = clean(serviceOrderId);
  if (!requested) return "";
  if (/^\d+$/.test(requested)) return `OS-${String(Number(requested)).padStart(5, "0")}`;
  return requested;
}

function orderSubtitle(order) {
  return [
    clean(order?.orderNumber),
    clean(order?.bejermanCustomerCode) || "sin código Bejerman",
  ]
    .filter(Boolean)
    .join(" · ");
}

function customerCode(customer) {
  return clean(customer?.bejermanCustomerCode);
}

function customerName(customer) {
  return clean(customer?.name || customer?.razon_social || customer?.nombre);
}

function customerOptionLabel(customer) {
  return [customerName(customer), customerCode(customer)].filter(Boolean).join(" · ");
}

function customerSearchKey(value) {
  return clean(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function customerMatchesQuery(customer, query) {
  if (!query) return true;
  return customerSearchKey([customerName(customer), customerCode(customer)].filter(Boolean).join(" ")).includes(query);
}

function selectedCodeOption(selectedCode, selectedOrder, customers) {
  if (!selectedCode) return null;
  if (customers.some((customer) => customerCode(customer) === selectedCode)) return null;
  return {
    id: `selected-${selectedCode}`,
    name: selectedOrder?.customerName || "Cliente seleccionado",
    bejermanCustomerCode: selectedCode,
  };
}

function remitoLocationLabel(value) {
  const key = clean(value).toLowerCase();
  return REMITO_LOCATION_LABELS[key] || value || "-";
}

function remitoPrintUrl(order) {
  return clean(order?.bejermanRemitoGroup?.printUrl);
}

function remitoTypeFromOrder(order) {
  const remitoType = clean(order?.remitoNumber).toUpperCase().split(/\s+/)[0];
  return remitoType || clean(order?.bejermanRemitoGroup?.comprobanteTipo).toUpperCase();
}

function hasPendingBilling(order) {
  if (!clean(order?.remitoNumber) || clean(order?.invoiceNumber)) return false;
  if (["facturado", "cancelado", "entregado_no_facturable"].includes(clean(order?.status))) return false;
  const billingRequired = order?.bejermanRemitoGroup?.responseSummary?.billingRequired;
  if (typeof billingRequired === "boolean") return billingRequired;
  if (typeof billingRequired === "string" && ["true", "false"].includes(billingRequired.trim().toLowerCase())) {
    return billingRequired.trim().toLowerCase() === "true";
  }
  return BILLABLE_REMITO_TYPES.has(remitoTypeFromOrder(order));
}

function remitoOperationLabel(item) {
  const code = valueOf(item, ["operationCode", "tipoOperacion"]);
  const label = valueOf(item, ["operationLabel", "origin"]);
  if (code && label && !String(label).startsWith(code)) return `${code} - ${label}`;
  return label || code || "-";
}

function initialRemitoPdfState() {
  return {
    open: false,
    loading: false,
    documentLabel: "",
    status: "",
    detail: "",
    error: "",
    blob: null,
  };
}

function RemitoPdfProgressModal({ state, onClose, onOpenPdf }) {
  if (!state?.open) return null;
  const canClose = !state.loading;
  const hasReadyPdf = Boolean(state.blob);

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-gray-950/45 px-4" role="alertdialog" aria-modal="true">
      <div className="w-full max-w-sm rounded-lg border border-sky-200 bg-white p-5 shadow-xl">
        <div className="flex items-start gap-4">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-sky-50 text-sky-700">
            {state.loading ? (
              <Loader2 className="h-6 w-6 animate-spin" aria-hidden="true" />
            ) : (
              <FileText className="h-6 w-6" aria-hidden="true" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-3">
              <h2 className="text-base font-semibold text-gray-950">Preparando PDF</h2>
              {canClose && (
                <button type="button" onClick={onClose} className="text-gray-500 hover:text-gray-900" aria-label="Cerrar">
                  <X className="h-4 w-4" aria-hidden="true" />
                </button>
              )}
            </div>
            {state.documentLabel && <p className="mt-0.5 text-xs text-gray-500">{state.documentLabel}</p>}
            <p className="mt-2 text-sm font-medium text-sky-800">{state.status || "Consultando Bejerman."}</p>
            {state.detail && <p className="mt-2 text-sm text-gray-600">{state.detail}</p>}
            {state.error && <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{state.error}</p>}
            {hasReadyPdf && (
              <button
                type="button"
                onClick={onOpenPdf}
                className="mt-4 inline-flex items-center gap-2 rounded bg-sky-700 px-3 py-2 text-sm font-medium text-white hover:bg-sky-800"
              >
                <FileText className="h-4 w-4" aria-hidden="true" />
                Abrir e imprimir PDF
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function DetailItem({ label, value, children }) {
  return (
    <div>
      <div className="text-[11px] font-medium uppercase text-gray-500">{label}</div>
      <div className="mt-0.5 text-sm text-gray-900">{children || value || "-"}</div>
    </div>
  );
}

function MissingPriceNotice({ totals }) {
  if (!totals?.hasMissingPrices) return null;
  return (
    <div className="inline-flex items-start gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <span>
        Hay {totals.missingPriceItems} ítem{totals.missingPriceItems === 1 ? "" : "s"} sin precio cargado.
        Revisá esos renglones antes de emitir la factura.
      </span>
    </div>
  );
}

function DeliveryOrderItemsTable({ order }) {
  const items = Array.isArray(order?.items) ? order.items : [];
  const totals = deliveryOrderItemsTotals(order);

  if (!items.length) {
    return <div className="rounded border bg-white px-3 py-4 text-sm text-gray-500">Sin ítems cargados.</div>;
  }

  return (
    <div className="overflow-x-auto rounded border bg-white">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
          <tr>
            <th className="px-2 py-2">Ítem</th>
            <th className="px-2 py-2">Partida</th>
            <th className="px-2 py-2 text-right">Cantidad</th>
            <th className="px-2 py-2 text-right">Precio lista</th>
            <th className="px-2 py-2 text-right">Desc.</th>
            <th className="px-2 py-2 text-right">Subtotal neto</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item, index) => {
            const unitPrice = deliveryOrderItemUnitPrice(item);
            const itemCurrency = deliveryOrderItemPriceCurrency(item, order);
            const discountPercent = deliveryOrderItemDiscountPercent(item);
            const amounts = deliveryOrderItemAmounts(item);
            const subtotal = amounts?.netSubtotal ?? null;
            return (
              <tr key={item.id || index} className="border-t align-top">
                <td className="px-2 py-2">
                  <div className="font-medium text-gray-900">{deliveryOrderItemLabel(item)}</div>
                  {clean(item?.description) && (
                    <div className="mt-1 whitespace-pre-wrap text-xs text-gray-600">{item.description}</div>
                  )}
                </td>
                <td className="px-2 py-2 text-xs text-gray-600">{deliveryOrderItemPartidaLabel(item, order) || "-"}</td>
                <td className="px-2 py-2 text-right">{formatOrderQuantity(item?.quantity)}</td>
                <td className={`px-2 py-2 text-right ${unitPrice === null ? "text-amber-800" : ""}`}>
                  {unitPrice === null ? "Sin precio" : formatOrderMoney(unitPrice, itemCurrency)}
                </td>
                <td className="px-2 py-2 text-right text-gray-700">
                  {discountPercent > 0 ? `${discountPercent.toLocaleString("es-AR", { maximumFractionDigits: 2 })}%` : "-"}
                </td>
                <td className={`px-2 py-2 text-right font-medium ${subtotal === null ? "text-amber-800" : "text-gray-950"}`}>
                  {subtotal === null ? "-" : formatOrderMoney(subtotal, itemCurrency)}
                </td>
              </tr>
            );
          })}
        </tbody>
        <tfoot className="border-t bg-gray-50">
          <tr>
            <td colSpan={5} className="px-2 py-2 text-right font-medium text-gray-700">
              Total estimado
            </td>
            <td className="px-2 py-2 text-right font-semibold text-gray-950">{formatOrderTotalsAmount(totals, "total")}</td>
          </tr>
          {totals.discountTotal > 0 && (
            <>
              <tr>
                <td colSpan={5} className="px-2 py-1 text-right text-xs text-gray-600">
                  Subtotal lista
                </td>
                <td className="px-2 py-1 text-right text-xs text-gray-700">{formatOrderTotalsAmount(totals, "grossTotal")}</td>
              </tr>
              <tr>
                <td colSpan={5} className="px-2 py-1 text-right text-xs text-gray-600">
                  Descuentos
                </td>
                <td className="px-2 py-1 text-right text-xs text-emerald-700">-{formatOrderTotalsAmount(totals, "discountTotal")}</td>
              </tr>
            </>
          )}
        </tfoot>
      </table>
    </div>
  );
}

function DeliveryOrderBillingDetails({ order, showItems = true }) {
  if (!order) return null;
  const totals = deliveryOrderItemsTotals(order);
  const commercial = deliveryOrderCommercialLabel(order);
  const source = deliveryOrderSourceLabel(order);
  const printUrl = remitoPrintUrl(order);

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <DetailItem label="Remito" value={order.remitoNumber || "-"} />
        <DetailItem label="Orden" value={order.orderNumber} />
        <DetailItem label="Cliente" value={order.customerName} />
        <DetailItem label="Código Bejerman" value={order.bejermanCustomerCode || "sin código"} />
        <DetailItem label="Empresa" value={deliveryOrderCompanyLabel(order)} />
        <DetailItem label="Fecha de entrega" value={formatDateTime(order.deliveredAt)} />
        <DetailItem label="Ubicación del remito" value={remitoLocationLabel(order.remitoLocation)} />
        <DetailItem label="Total estimado" value={formatOrderTotalsAmount(totals, "total")} />
        <DetailItem label="Comercial" value={commercial || "-"} />
        <DetailItem label="Origen" value={source || "-"} />
        {printUrl && (
          <DetailItem label="Remito emitido">
            <a href={printUrl} target="_blank" rel="noreferrer" className="text-blue-700 hover:underline">
              Abrir remito
            </a>
          </DetailItem>
        )}
      </div>

      <MissingPriceNotice totals={totals} />

      {clean(order.rawPedido) && (
        <div>
          <div className="text-[11px] font-medium uppercase text-gray-500">Detalle completo de la entrega</div>
          <div className="mt-1 whitespace-pre-wrap rounded border bg-white px-3 py-2 text-sm text-gray-800">
            {order.rawPedido}
          </div>
        </div>
      )}

      {showItems && (
        <div>
          <div className="mb-1 text-[11px] font-medium uppercase text-gray-500">Desglose de ítems</div>
          <DeliveryOrderItemsTable order={order} />
        </div>
      )}
    </div>
  );
}

export default function Billing() {
  const location = useLocation();
  const isRemitosRoute = location.pathname.endsWith("/remitos");
  const shouldRedirectToFacturacion = isRemitosRoute && /\b(?:orderId|serviceOrderId)=/.test(location.search || "");
  const requestedOrderId = useMemo(initialOrderIdFromUrl, []);
  const requestedServiceOrderId = useMemo(initialServiceOrderIdFromUrl, []);
  const [customers, setCustomers] = useState([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [customerQuery, setCustomerQuery] = useState("");
  const [customerSuggestionsOpen, setCustomerSuggestionsOpen] = useState(false);
  const [customerActiveIndex, setCustomerActiveIndex] = useState(0);
  const [filters, setFilters] = useState({ dateFrom: "", dateTo: "", search: "" });
  const [documents, setDocuments] = useState([]);
  const [pagination, setPagination] = useState(EMPTY_PAGINATION);
  const [page, setPage] = useState(1);
  const [selectedRemitoCode, setSelectedRemitoCode] = useState("");
  const [remitoFilters, setRemitoFilters] = useState({
    companyKey: "SEPID",
    dateFrom: "",
    dateTo: "",
    remitoType: "",
    operationType: "",
    search: "",
  });
  const [remitos, setRemitos] = useState([]);
  const [remitosPagination, setRemitosPagination] = useState(EMPTY_PAGINATION);
  const [remitoPage, setRemitoPage] = useState(1);
  const [pendingOrders, setPendingOrders] = useState([]);
  const [selectedPendingOrderId, setSelectedPendingOrderId] = useState("");
  const [pendingDetailOrder, setPendingDetailOrder] = useState(null);
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [remitosLoading, setRemitosLoading] = useState(false);
  const [pendingLoading, setPendingLoading] = useState(false);
  const [savingInvoice, setSavingInvoice] = useState(false);
  const [invoiceOrder, setInvoiceOrder] = useState(null);
  const [invoiceNumber, setInvoiceNumber] = useState("");
  const [invoiceError, setInvoiceError] = useState("");
  const [error, setError] = useState("");
  const [documentsWarning, setDocumentsWarning] = useState("");
  const [remitoError, setRemitoError] = useState("");
  const [remitoWarning, setRemitoWarning] = useState("");
  const [remitoPdf, setRemitoPdf] = useState(initialRemitoPdfState);
  const [loadingDocumentPdfId, setLoadingDocumentPdfId] = useState("");
  const [loadingRemitoPdfId, setLoadingRemitoPdfId] = useState("");
  const [pendingError, setPendingError] = useState("");
  const [savingNotBillableId, setSavingNotBillableId] = useState("");

  const selectedPendingOrder = useMemo(
    () => pendingOrders.find((order) => order.id === selectedPendingOrderId) || null,
    [pendingOrders, selectedPendingOrderId]
  );

  const customerSuggestions = useMemo(() => {
    const query = customerSearchKey(customerQuery);
    const extra = selectedCodeOption(selectedCode, selectedPendingOrder, customers);
    const options = extra ? [extra, ...customers.filter((customer) => customerCode(customer) !== customerCode(extra))] : customers;
    return options.filter((customer) => customerMatchesQuery(customer, query)).slice(0, 20);
  }, [customers, customerQuery, selectedCode, selectedPendingOrder]);

  const resolveCustomerCodeInput = () => {
    const selected = clean(selectedCode);
    if (selected) return selected;
    const typed = clean(customerQuery);
    if (!typed) return "";
    const typedKey = customerSearchKey(typed);
    const exact = customers.find((customer) =>
      [customerCode(customer), customerName(customer), customerOptionLabel(customer)]
        .map(customerSearchKey)
        .some((value) => value === typedKey)
    );
    if (exact) return customerCode(exact);
    if (customerSuggestions.length === 1) return customerCode(customerSuggestions[0]);
    return typed;
  };

  const remitoCustomerOptions = useMemo(
    () => customers.filter((customer) => clean(customer.bejermanCustomerCode)),
    [customers]
  );

  useEffect(() => {
    setCustomerActiveIndex((current) => Math.min(current, Math.max(customerSuggestions.length - 1, 0)));
  }, [customerSuggestions.length]);

  const loadDocuments = async (customerCode = selectedCode, pageNumber = page) => {
    const code = clean(customerCode);
    setDocumentsLoading(true);
    try {
      const params = {
        ...filters,
        page: pageNumber,
        pageSize: BILLING_PAGE_SIZE,
      };
      if (code) params.customerCode = code;
      const data = await getBillingDocuments(params);
      setDocuments(Array.isArray(data?.items) ? data.items : []);
      setPagination(data?.pagination || EMPTY_PAGINATION);
      setPage(data?.pagination?.page || pageNumber);
      setDocumentsWarning(clean(data?.warning));
      setError("");
    } catch (err) {
      setError(err?.message || "No se pudo consultar facturación.");
      setDocumentsWarning("");
      setDocuments([]);
      setPagination(EMPTY_PAGINATION);
    } finally {
      setDocumentsLoading(false);
    }
  };

  const loadRemitos = async (customerCode = selectedRemitoCode, pageNumber = remitoPage) => {
    const code = clean(customerCode);
    setRemitosLoading(true);
    try {
      const params = {
        ...remitoFilters,
        page: pageNumber,
        pageSize: BILLING_PAGE_SIZE,
      };
      if (code) params.customerCode = code;
      const data = await getBillingRemitos(params);
      setRemitos(Array.isArray(data?.items) ? data.items : []);
      setRemitosPagination(data?.pagination || EMPTY_PAGINATION);
      setRemitoPage(data?.pagination?.page || pageNumber);
      setRemitoError("");
      setRemitoWarning(clean(data?.warning));
    } catch (err) {
      setRemitoError(err?.message || "No se pudieron consultar los remitos.");
      setRemitoWarning("");
      setRemitos([]);
      setRemitosPagination(EMPTY_PAGINATION);
    } finally {
      setRemitosLoading(false);
    }
  };

  const loadPendingOrders = async ({ preferredSelectedId = selectedPendingOrderId, allowRequestedSelection = true } = {}) => {
    setPendingLoading(true);
    try {
      const data = await getDeliveryOrders({ pendingBilling: true, limit: 200 });
      let items = Array.isArray(data?.items) ? data.items : [];
      if (allowRequestedSelection && requestedOrderId && !items.some((order) => order.id === requestedOrderId)) {
        try {
          const requestedOrder = await getDeliveryOrder(requestedOrderId);
          if (hasPendingBilling(requestedOrder)) {
            items = [requestedOrder, ...items];
          }
        } catch {
          // Si el vínculo ya no corresponde a un pendiente, la lista general sigue disponible.
        }
      }
      if (
        allowRequestedSelection &&
        requestedServiceOrderId &&
        !items.some((order) => orderMatchesServiceOrderId(order, requestedServiceOrderId))
      ) {
        try {
          const serviceData = await getDeliveryOrders({
            pendingBilling: true,
            q: serviceOrderSearchQuery(requestedServiceOrderId),
            limit: 20,
          });
          const serviceItems = Array.isArray(serviceData?.items) ? serviceData.items : [];
          items = [
            ...serviceItems.filter((order) => orderMatchesServiceOrderId(order, requestedServiceOrderId)),
            ...items,
          ];
        } catch {
          // Si el vínculo de OS no encuentra un remito pendiente, la lista general sigue disponible.
        }
      }

      items = normalizePendingBillingOrders(items);
      setPendingOrders(items);
      const preferredExists = preferredSelectedId && items.some((order) => order.id === preferredSelectedId);
      const requestedExists = allowRequestedSelection && requestedOrderId && items.some((order) => order.id === requestedOrderId);
      const requestedServiceOrder = allowRequestedSelection && requestedServiceOrderId
        ? items.find((order) => orderMatchesServiceOrderId(order, requestedServiceOrderId))
        : null;
      const nextSelectedId = preferredExists ? preferredSelectedId : requestedExists ? requestedOrderId : requestedServiceOrder?.id || "";
      setSelectedPendingOrderId(nextSelectedId);
      setPendingError("");
    } catch {
      setPendingOrders([]);
      setPendingError("No se pudieron cargar los remitos pendientes.");
    } finally {
      setPendingLoading(false);
    }
  };

  useEffect(() => {
    getBillingCustomers()
      .then((data) => {
        const items = Array.isArray(data?.items) ? data.items : [];
        setCustomers(items);
      })
      .catch((err) => setError(err?.message || "No se pudieron cargar clientes."));
  }, []);

  useEffect(() => {
    if (isRemitosRoute) return;
    loadPendingOrders({ preferredSelectedId: "", allowRequestedSelection: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isRemitosRoute]);

  const selectPendingOrder = (order) => {
    setSelectedPendingOrderId(order.id);
    setPendingDetailOrder(order);
  };

  const openInvoiceModal = (order) => {
    setSelectedPendingOrderId(order.id);
    setPendingDetailOrder(null);
    setInvoiceOrder(order);
    setInvoiceNumber("");
    setInvoiceError("");
    setError("");
  };

  const closePendingDetailModal = () => {
    setPendingDetailOrder(null);
  };

  const closeInvoiceModal = () => {
    if (savingInvoice) return;
    setInvoiceOrder(null);
    setInvoiceNumber("");
    setInvoiceError("");
  };

  const submitInvoice = async (event) => {
    event.preventDefault();
    const invoice = invoiceNumber.trim();
    if (!invoice) {
      setInvoiceError("Ingrese el número de factura.");
      return;
    }
    if (!invoiceOrder?.id) return;

    setSavingInvoice(true);
    setInvoiceError("");
    setError("");
    try {
      await postDeliveryOrderInvoiced(invoiceOrder.id, { invoiceNumber: invoice });
      setInvoiceOrder(null);
      setInvoiceNumber("");
      setPendingDetailOrder(null);
      setSelectedPendingOrderId("");
      await loadPendingOrders({ preferredSelectedId: "", allowRequestedSelection: false });
    } catch (err) {
      setInvoiceError(err?.message || "No se pudo registrar la factura.");
    } finally {
      setSavingInvoice(false);
    }
  };

  const markOrderNotBillable = async (order) => {
    if (!order?.id || savingNotBillableId) return;
    const label = order.remitoNumber || order.orderNumber || "este remito";
    if (!window.confirm(`¿Marcar ${label} como no facturable?`)) return;

    setSavingNotBillableId(order.id);
    setPendingError("");
    try {
      await postDeliveryOrderNotBillable(order.id, { note: "No se factura" });
      if (selectedPendingOrderId === order.id) setSelectedPendingOrderId("");
      if (pendingDetailOrder?.id === order.id) setPendingDetailOrder(null);
      await loadPendingOrders({ preferredSelectedId: "", allowRequestedSelection: false });
    } catch (err) {
      setPendingError(err?.message || "No se pudo marcar el remito como no facturable.");
    } finally {
      setSavingNotBillableId("");
    }
  };

  const openPdf = async (item) => {
    const documentId = valueOf(item, ["documentId", "id"]);
    const customerCode = valueOf(item, ["bejermanCustomerCode", "customerCode"]) || selectedCode;
    if (!documentId || loadingDocumentPdfId) return;
    setLoadingDocumentPdfId(documentId);
    setError("");
    try {
      const blob = await getBillingDocumentPdfBlob(documentId, customerCode);
      openBlob(blob, `facturacion-${documentId}.pdf`);
    } catch (err) {
      setError(err?.message || "No se pudo abrir el PDF.");
    } finally {
      setLoadingDocumentPdfId("");
    }
  };

  const openRemitoPdf = async (item) => {
    const documentId = valueOf(item, ["documentId", "id"]);
    const customerCode = valueOf(item, ["bejermanCustomerCode", "customerCode"]) || selectedRemitoCode;
    if (!documentId) return;
    const documentLabel = remitoDocumentNumber(item);
    const pdfUrl = valueOf(item, ["pdfUrl"]) || billingRemitoPdfUrl(documentId, {
      customerCode,
      companyKey: remitoFilters.companyKey,
    });
    const reservedWindow = reservePdfWindow({
      title: "Remito Bejerman",
      message: `Preparando PDF del ${documentLabel || "remito"}...`,
      fallbackMessage:
        "NEXORA sigue esperando el PDF de Bejerman. Si esta pestaña no cambia, puede cerrarla y reintentar desde Cobranzas.",
    });
    setRemitoError("");
    setLoadingRemitoPdfId(documentId);
    setRemitoPdf({
      open: true,
      loading: true,
      documentLabel,
      status: "Buscando PDF del remito...",
      detail: "Consultando Bejerman sin abrir una nueva ventana.",
      error: "",
      blob: null,
    });
    try {
      const blob = await waitForPdfBlob(pdfUrl, {
        label: documentLabel || "remito",
        onProgress: (progress) => {
          setRemitoPdf((current) => ({
            ...current,
            status: progress?.status || "Preparando PDF del remito...",
            detail: progress?.detail || "Esperando el archivo de Bejerman.",
          }));
        },
      });
      setRemitoPdf((current) => ({
        ...current,
        status: "Abriendo impresión",
        detail: "El PDF está listo. Intentando abrir la ventana de impresión.",
      }));
      const opened = reservedWindow.openPrintable(blob, {
        title: "Remito Bejerman",
        documentLabel: documentLabel || "Remito Bejerman",
      }).opened;
      if (opened) {
        setRemitoPdf(initialRemitoPdfState());
      } else {
        setRemitoPdf((current) => ({
          ...current,
          loading: false,
          status: "PDF listo",
          detail: "El navegador bloqueó la ventana automática.",
          error: "El PDF está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir PDF.",
          blob,
        }));
      }
    } catch (err) {
      reservedWindow.close();
      const message = err?.message || "No se pudo abrir el PDF del remito.";
      setRemitoError(message);
      setRemitoPdf((current) => ({
        ...current,
        loading: false,
        status: "No se pudo abrir el PDF",
        detail: "",
        error: message,
        blob: null,
      }));
    } finally {
      setLoadingRemitoPdfId("");
    }
  };

  const closeRemitoPdfModal = () => {
    if (remitoPdf.loading) return;
    setRemitoPdf(initialRemitoPdfState());
  };

  const openManualRemitoPdf = () => {
    if (!remitoPdf.blob) return;
    const opened = openPrintablePdf(remitoPdf.blob, {
      title: "Remito Bejerman",
      documentLabel: remitoPdf.documentLabel || "Remito Bejerman",
    }).opened;
    if (opened) {
      setRemitoError("");
      setRemitoPdf(initialRemitoPdfState());
    }
  };

  const selectBillingCustomer = (customer, shouldLoad = true) => {
    const code = customerCode(customer);
    setSelectedCode(code);
    setCustomerQuery(customerOptionLabel(customer));
    setCustomerSuggestionsOpen(false);
    setCustomerActiveIndex(0);
    if (shouldLoad) {
      setPage(1);
      loadDocuments(code, 1);
    }
  };

  const clearBillingCustomer = () => {
    setSelectedCode("");
    setCustomerQuery("");
    setCustomerSuggestionsOpen(false);
    setCustomerActiveIndex(0);
    setPage(1);
    loadDocuments("", 1);
  };

  const currentPage = pagination?.page || page || 1;
  const totalPages = pagination?.totalPages || 1;
  const totalDocuments = pagination?.total || documents.length;
  const currentRemitoPage = remitosPagination?.page || remitoPage || 1;
  const totalRemitoPages = remitosPagination?.totalPages || 1;
  const totalRemitos = remitosPagination?.total || remitos.length;

  const runSearch = () => {
    const customerCode = resolveCustomerCodeInput();
    setPage(1);
    setSelectedCode(customerCode);
    loadDocuments(customerCode, 1);
  };

  const goToPage = (nextPage) => {
    const safePage = Math.max(1, Math.min(totalPages, nextPage));
    if (safePage === currentPage || documentsLoading) return;
    setPage(safePage);
    loadDocuments(resolveCustomerCodeInput(), safePage);
  };

  const handleBillingCustomerKeyDown = (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setCustomerSuggestionsOpen(true);
      if (!customerSuggestions.length) return;
      const delta = event.key === "ArrowDown" ? 1 : -1;
      setCustomerActiveIndex((current) => (current + delta + customerSuggestions.length) % customerSuggestions.length);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (customerSuggestionsOpen && customerSuggestions.length) {
        selectBillingCustomer(customerSuggestions[Math.max(0, Math.min(customerActiveIndex, customerSuggestions.length - 1))]);
      } else {
        runSearch();
      }
      return;
    }
    if (event.key === "Escape") {
      setCustomerSuggestionsOpen(false);
    }
  };

  const runRemitoSearch = () => {
    setRemitoPage(1);
    loadRemitos(selectedRemitoCode, 1);
  };

  const goToRemitoPage = (nextPage) => {
    const safePage = Math.max(1, Math.min(totalRemitoPages, nextPage));
    if (safePage === currentRemitoPage || remitosLoading) return;
    setRemitoPage(safePage);
    loadRemitos(selectedRemitoCode, safePage);
  };

  if (shouldRedirectToFacturacion) {
    return <Navigate to={`/cobranzas/facturacion${location.search || ""}`} replace />;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{isRemitosRoute ? "Remitos" : "Facturación"}</h1>
          <p className="text-sm text-gray-600">
            {isRemitosRoute ? "Consulta de remitos Bejerman." : "Consulta de facturación y remitos pendientes."}
          </p>
        </div>
      </div>

      <div className={isRemitosRoute ? "contents" : "grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]"}>
        {!isRemitosRoute && (
        <section className="border">
          <div className="border-b px-3 py-2 text-sm font-semibold">Consulta de facturación</div>

          <div className="grid gap-2 p-3 sm:grid-cols-2 xl:grid-cols-[minmax(220px,1fr)_150px_150px_minmax(180px,1fr)_auto] xl:items-end">
            <label className="relative text-sm sm:col-span-2 xl:col-span-1">
              <span className="mb-1 block text-xs uppercase text-gray-500">Cliente</span>
              <input
                value={customerQuery}
                onFocus={() => {
                  setCustomerSuggestionsOpen(true);
                  setCustomerActiveIndex(0);
                }}
                onBlur={() => window.setTimeout(() => setCustomerSuggestionsOpen(false), 120)}
                onKeyDown={handleBillingCustomerKeyDown}
                onChange={(event) => {
                  setCustomerQuery(event.target.value);
                  setSelectedCode("");
                  setCustomerActiveIndex(0);
                  setCustomerSuggestionsOpen(true);
                }}
                className="h-9 w-full rounded border px-2 pr-8"
                placeholder="Escriba cliente o código"
                role="combobox"
                aria-expanded={customerSuggestionsOpen}
                aria-controls="billing-customer-options"
                autoComplete="off"
              />
              {(customerQuery || selectedCode) && (
                <button
                  type="button"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={clearBillingCustomer}
                  className="absolute right-1 top-6 inline-flex h-7 w-7 items-center justify-center rounded text-gray-500 hover:bg-gray-100 hover:text-gray-800"
                  aria-label="Limpiar cliente"
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                </button>
              )}
              {customerSuggestionsOpen && (
                <div
                  id="billing-customer-options"
                  role="listbox"
                  className="absolute z-30 mt-1 max-h-64 w-full overflow-y-auto rounded border bg-white shadow-lg"
                >
                  {customerSuggestions.length ? (
                    customerSuggestions.map((customer, index) => (
                      <button
                        key={customer.id || customerCode(customer)}
                        type="button"
                        role="option"
                        aria-selected={index === customerActiveIndex}
                        onMouseDown={(event) => event.preventDefault()}
                        onMouseEnter={() => setCustomerActiveIndex(index)}
                        onClick={() => selectBillingCustomer(customer)}
                        className={`w-full px-3 py-2 text-left text-sm ${
                          index === customerActiveIndex ? "bg-slate-100" : "hover:bg-gray-50"
                        }`}
                      >
                        <span className="block truncate font-medium">{customerName(customer) || "Cliente sin nombre"}</span>
                        <span className="block truncate text-xs text-gray-500">{customerCode(customer) || "sin código Bejerman"}</span>
                      </button>
                    ))
                  ) : (
                    <div className="px-3 py-2 text-xs text-gray-500">Sin coincidencias.</div>
                  )}
                </div>
              )}
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-xs uppercase text-gray-500">Desde</span>
              <input
                type="date"
                value={filters.dateFrom}
                onChange={(event) => setFilters((prev) => ({ ...prev, dateFrom: event.target.value }))}
                className="h-9 w-full rounded border px-2"
              />
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-xs uppercase text-gray-500">Hasta</span>
              <input
                type="date"
                value={filters.dateTo}
                onChange={(event) => setFilters((prev) => ({ ...prev, dateTo: event.target.value }))}
                className="h-9 w-full rounded border px-2"
              />
            </label>
            <label className="text-sm sm:col-span-2 xl:col-span-1">
              <span className="mb-1 block text-xs uppercase text-gray-500">Buscar</span>
              <input
                value={filters.search}
                onChange={(event) => setFilters((prev) => ({ ...prev, search: event.target.value }))}
                className="h-9 w-full rounded border px-2"
                placeholder="Número o tipo"
              />
            </label>
            <button
              type="button"
              onClick={runSearch}
              disabled={documentsLoading}
              className="inline-flex h-9 items-center justify-center gap-2 rounded border px-3 text-sm hover:bg-gray-50 disabled:opacity-50 sm:col-span-2 xl:col-span-1"
            >
              {documentsLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <Search className="h-4 w-4" aria-hidden="true" />
              )}
              Consultar
            </button>
          </div>

          {error && <div className="border-t px-3 py-2 text-sm text-red-700">{error}</div>}
          {documentsWarning && <div className="border-t px-3 py-2 text-sm text-amber-800">{documentsWarning}</div>}

          <MobileDataList className="p-3">
            {documentsLoading && <MobileDataCard className="text-center text-gray-500">Cargando...</MobileDataCard>}
            {!documentsLoading &&
              documents.map((item, index) => {
                const documentId = valueOf(item, ["documentId", "id"]);
                const pdfLoading = loadingDocumentPdfId === documentId;
                const total = valueOf(item, ["totalAmount", "total", "importeTotal", "amount"]);
                return (
                  <MobileDataCard key={documentId || index} className="space-y-3">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-semibold text-gray-900">{valueOf(item, ["numero", "number", "comprobanteNumero"]) || "-"}</div>
                        <div className="text-xs text-gray-500">{valueOf(item, ["tipo", "type", "comprobanteTipo"]) || "-"}</div>
                      </div>
                      <div className="text-right font-semibold text-gray-900">{formatAmount(total)}</div>
                    </div>
                    <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                      <MobileDataField label="Fecha" value={valueOf(item, ["fecha", "date", "issueDate"]) || "-"} />
                      <MobileDataField label="Cliente" value={valueOf(item, ["customerName", "cliente", "razonSocial"]) || "-"} />
                    </div>
                    {documentId && (
                      <button
                        type="button"
                        onClick={() => openPdf(item)}
                        disabled={Boolean(loadingDocumentPdfId)}
                        className="inline-flex h-9 w-full items-center justify-center gap-1 rounded border px-2 text-xs hover:bg-gray-50 disabled:opacity-50"
                      >
                        {pdfLoading ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                        ) : (
                          <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                        )}
                        {pdfLoading ? "Preparando..." : "PDF"}
                      </button>
                    )}
                  </MobileDataCard>
                );
              })}
            {!documentsLoading && !documents.length && <MobileDataCard className="text-center text-gray-500">Sin documentos.</MobileDataCard>}
          </MobileDataList>
          <DesktopTableWrap>
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-2">Fecha</th>
                  <th className="px-2 py-2">Cliente</th>
                  <th className="px-2 py-2">Comprobante</th>
                  <th className="px-2 py-2">Número</th>
                  <th className="px-2 py-2 text-right">Total</th>
                  <th className="px-2 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {documentsLoading ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-8 text-center text-gray-500">
                      Cargando...
                    </td>
                  </tr>
                ) : (
                  documents.map((item, index) => {
                    const documentId = valueOf(item, ["documentId", "id"]);
                    const pdfLoading = loadingDocumentPdfId === documentId;
                    const total = valueOf(item, ["totalAmount", "total", "importeTotal", "amount"]);
                    return (
                      <tr key={documentId || index} className="border-t">
                        <td className="px-2 py-2">{valueOf(item, ["fecha", "date", "issueDate"]) || "-"}</td>
                        <td className="px-2 py-2">{valueOf(item, ["customerName", "cliente", "razonSocial"]) || "-"}</td>
                        <td className="px-2 py-2">{valueOf(item, ["tipo", "type", "comprobanteTipo"]) || "-"}</td>
                        <td className="px-2 py-2">{valueOf(item, ["numero", "number", "comprobanteNumero"]) || "-"}</td>
                        <td className="px-2 py-2 text-right">{formatAmount(total)}</td>
                        <td className="px-2 py-2 text-right">
                          {documentId && (
                            <button
                              type="button"
                              onClick={() => openPdf(item)}
                              disabled={Boolean(loadingDocumentPdfId)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                            >
                              {pdfLoading ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                              ) : (
                                <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                              )}
                              {pdfLoading ? "Preparando..." : "PDF"}
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
                {!documentsLoading && !documents.length && (
                  <tr>
                    <td colSpan={6} className="px-3 py-8 text-center text-gray-500">
                      Sin documentos.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </DesktopTableWrap>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t px-3 py-2 text-sm text-gray-600">
            <span>
              {totalDocuments ? `${totalDocuments} comprobantes · página ${currentPage} de ${totalPages}` : "Sin comprobantes"}
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => goToPage(currentPage - 1)}
                disabled={!pagination?.hasPreviousPage || documentsLoading}
                className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
              >
                Anterior
              </button>
              <button
                type="button"
                onClick={() => goToPage(currentPage + 1)}
                disabled={!pagination?.hasNextPage || documentsLoading}
                className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
              >
                Siguiente
              </button>
            </div>
          </div>
        </section>
        )}

        {!isRemitosRoute && (
        <section className="border">
          <div className="flex items-center justify-between border-b px-3 py-2 text-sm font-semibold">
            <span>Remitos pendientes</span>
            <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-normal text-gray-700">
              {pendingOrders.length}
            </span>
          </div>
          {pendingError && <div className="border-b px-3 py-2 text-sm text-red-700">{pendingError}</div>}
          <div className="max-h-[560px] overflow-y-auto">
            {pendingLoading ? (
              <div className="flex items-center justify-center gap-2 px-3 py-8 text-sm text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Cargando...
              </div>
            ) : (
              pendingOrders.map((order) => {
                const totals = deliveryOrderItemsTotals(order);
                return (
                  <div
                    key={order.id}
                    className={`border-b px-3 py-2 text-sm ${
                      selectedPendingOrderId === order.id ? "bg-emerald-50" : ""
                    }`}
                  >
                    <button type="button" onClick={() => selectPendingOrder(order)} className="block w-full text-left">
                      <div className="font-medium text-gray-950">{order.remitoNumber || order.orderNumber}</div>
                      <div className="text-gray-600">{order.customerName}</div>
                      <div className="text-xs text-gray-500">{orderSubtitle(order)}</div>
                      <div className="mt-1 text-xs font-medium text-gray-800">
                        Total estimado: {formatOrderTotalsAmount(totals, "total")}
                      </div>
                    </button>
                    {totals.hasMissingPrices && (
                      <div className="mt-2 flex items-start gap-1.5 text-xs text-amber-800">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                        Ítems sin precio
                      </div>
                    )}
                    {!order.bejermanCustomerCode && (
                      <div className="mt-2 flex items-start gap-1.5 text-xs text-amber-800">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                        Sin código Bejerman
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => openInvoiceModal(order)}
                      disabled={savingInvoice}
                      className="mt-2 inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                      Registrar factura
                    </button>
                    <button
                      type="button"
                      onClick={() => markOrderNotBillable(order)}
                      disabled={savingNotBillableId === order.id}
                      className="ml-2 mt-2 inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                    >
                      {savingNotBillableId === order.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                      ) : (
                        <X className="h-3.5 w-3.5" aria-hidden="true" />
                      )}
                      No se factura
                    </button>
                  </div>
                );
              })
            )}
            {!pendingLoading && !pendingOrders.length && (
              <div className="px-3 py-8 text-center text-sm text-gray-500">Sin remitos pendientes.</div>
            )}
          </div>
        </section>
        )}
      </div>

      {isRemitosRoute && (
      <section className="border">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-3 py-2">
          <div>
            <div className="text-sm font-semibold">Consulta de remitos Bejerman</div>
            <div className="text-xs text-gray-500">Remitos emitidos en Bejerman por empresa, tipo, operación, cliente y fecha.</div>
          </div>
          <span className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-700">{totalRemitos}</span>
        </div>

        <div className="grid gap-2 p-3 sm:grid-cols-2 xl:grid-cols-[150px_170px_150px_150px_minmax(150px,1fr)_minmax(180px,1fr)_auto] xl:items-end">
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase text-gray-500">Empresa</span>
            <select
              value={remitoFilters.companyKey}
              onChange={(event) => {
                setRemitoPage(1);
                setRemitoFilters((prev) => ({ ...prev, companyKey: event.target.value }));
              }}
              className="h-9 w-full rounded border px-2"
            >
              {BEJERMAN_COMPANY_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase text-gray-500">Cliente</span>
            <input
              value={selectedRemitoCode}
              onChange={(event) => {
                setRemitoPage(1);
                setSelectedRemitoCode(event.target.value);
              }}
              list="billing-remito-customers"
              className="h-9 w-full rounded border px-2"
              placeholder="Código Bejerman"
              autoComplete="off"
            />
            <datalist id="billing-remito-customers">
              {remitoCustomerOptions.map((customer) => (
                <option key={customer.id} value={customer.bejermanCustomerCode}>
                  {customer.name}
                </option>
              ))}
            </datalist>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase text-gray-500">Desde</span>
            <input
              type="date"
              value={remitoFilters.dateFrom}
              onChange={(event) => setRemitoFilters((prev) => ({ ...prev, dateFrom: event.target.value }))}
              className="h-9 w-full rounded border px-2"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase text-gray-500">Hasta</span>
            <input
              type="date"
              value={remitoFilters.dateTo}
              onChange={(event) => setRemitoFilters((prev) => ({ ...prev, dateTo: event.target.value }))}
              className="h-9 w-full rounded border px-2"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase text-gray-500">Tipo remito</span>
            <select
              value={remitoFilters.remitoType}
              onChange={(event) => {
                setRemitoPage(1);
                setRemitoFilters((prev) => ({ ...prev, remitoType: event.target.value }));
              }}
              className="h-9 w-full rounded border px-2"
            >
              <option value="">Todos</option>
              {REMITO_TYPE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase text-gray-500">Operación</span>
            <select
              value={remitoFilters.operationType}
              onChange={(event) => {
                setRemitoPage(1);
                setRemitoFilters((prev) => ({ ...prev, operationType: event.target.value }));
              }}
              className="h-9 w-full rounded border px-2"
            >
              <option value="">Todas</option>
              {REMITO_OPERATION_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={runRemitoSearch}
            disabled={remitosLoading}
            className="inline-flex h-9 items-center justify-center gap-2 rounded border px-3 text-sm hover:bg-gray-50 disabled:opacity-50 sm:col-span-2 xl:col-span-1"
          >
            {remitosLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Search className="h-4 w-4" aria-hidden="true" />
            )}
            Consultar
          </button>
          <label className="text-sm sm:col-span-2 xl:col-span-7">
            <span className="mb-1 block text-xs uppercase text-gray-500">Buscar</span>
            <input
              value={remitoFilters.search}
              onChange={(event) => setRemitoFilters((prev) => ({ ...prev, search: event.target.value }))}
              className="h-9 w-full rounded border px-2"
              placeholder="Número, cliente, código u operación"
            />
          </label>
        </div>

        {remitoWarning && <div className="border-t px-3 py-2 text-sm text-amber-800">{remitoWarning}</div>}
        {remitoError && <div className="border-t px-3 py-2 text-sm text-red-700">{remitoError}</div>}

        <MobileDataList className="p-3">
          {remitosLoading && <MobileDataCard className="text-center text-gray-500">Cargando...</MobileDataCard>}
          {!remitosLoading &&
            remitos.map((item, index) => {
              const documentId = valueOf(item, ["documentId", "id"]);
              const pdfLoading = loadingRemitoPdfId === documentId;
              return (
                <MobileDataCard key={documentId || index} className="space-y-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="font-semibold text-gray-900">{remitoDocumentNumber(item)}</div>
                      <div className="text-xs text-gray-500">{valueOf(item, ["type", "comprobanteTipo"]) || "-"}</div>
                    </div>
                    <div className="text-right text-sm font-semibold text-gray-900">
                      {formatAmount(valueOf(item, ["totalAmount", "total", "importeTotal", "amount"]))}
                    </div>
                  </div>
                  <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                    <MobileDataField label="Fecha" value={valueOf(item, ["date", "issueDate"]) || "-"} />
                    <MobileDataField label="Cliente" value={valueOf(item, ["customerName", "cliente", "razonSocial"]) || "-"} />
                    <MobileDataField label="Código" value={valueOf(item, ["bejermanCustomerCode", "customerCode"]) || "-"} />
                    <MobileDataField label="Operación" value={remitoOperationLabel(item)} />
                  </div>
                  {documentId && (
                    <button
                      type="button"
                      onClick={() => openRemitoPdf(item)}
                      disabled={Boolean(loadingRemitoPdfId)}
                      className="inline-flex h-9 w-full items-center justify-center gap-1 rounded border px-2 text-xs hover:bg-gray-50 disabled:opacity-50"
                    >
                      {pdfLoading ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                      ) : (
                        <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                      )}
                      {pdfLoading ? "Preparando..." : "PDF"}
                    </button>
                  )}
                </MobileDataCard>
              );
            })}
          {!remitosLoading && !remitos.length && <MobileDataCard className="text-center text-gray-500">Sin remitos.</MobileDataCard>}
        </MobileDataList>

        <DesktopTableWrap>
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
              <tr>
                <th className="px-2 py-2">Fecha</th>
                <th className="px-2 py-2">Cliente</th>
                <th className="px-2 py-2">Código</th>
                <th className="px-2 py-2">Tipo</th>
                <th className="px-2 py-2">Operación</th>
                <th className="px-2 py-2">Número</th>
                <th className="px-2 py-2 text-right">Total</th>
                <th className="px-2 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {remitosLoading ? (
                <tr>
                  <td colSpan={8} className="px-3 py-8 text-center text-gray-500">
                    Cargando...
                  </td>
                </tr>
              ) : (
                remitos.map((item, index) => {
                  const documentId = valueOf(item, ["documentId", "id"]);
                  const pdfLoading = loadingRemitoPdfId === documentId;
                  const total = valueOf(item, ["totalAmount", "total", "importeTotal", "amount"]);
                  return (
                    <tr key={documentId || index} className="border-t">
                      <td className="px-2 py-2">{valueOf(item, ["date", "issueDate"]) || "-"}</td>
                      <td className="px-2 py-2">{valueOf(item, ["customerName", "cliente", "razonSocial"]) || "-"}</td>
                      <td className="px-2 py-2">{valueOf(item, ["bejermanCustomerCode", "customerCode"]) || "-"}</td>
                      <td className="px-2 py-2">{valueOf(item, ["type", "comprobanteTipo"]) || "-"}</td>
                      <td className="px-2 py-2">{remitoOperationLabel(item)}</td>
                      <td className="px-2 py-2">{remitoDocumentNumber(item)}</td>
                      <td className="px-2 py-2 text-right">{formatAmount(total)}</td>
                      <td className="px-2 py-2 text-right">
                        {documentId && (
                          <button
                            type="button"
                            onClick={() => openRemitoPdf(item)}
                            disabled={Boolean(loadingRemitoPdfId)}
                            className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                          >
                            {pdfLoading ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                            ) : (
                              <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                            )}
                            {pdfLoading ? "Preparando..." : "PDF"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
              {!remitosLoading && !remitos.length && (
                <tr>
                  <td colSpan={8} className="px-3 py-8 text-center text-gray-500">
                    Sin remitos.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </DesktopTableWrap>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t px-3 py-2 text-sm text-gray-600">
          <span>
            {totalRemitos ? `${totalRemitos} remitos · página ${currentRemitoPage} de ${totalRemitoPages}` : "Sin remitos"}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => goToRemitoPage(currentRemitoPage - 1)}
              disabled={!remitosPagination?.hasPreviousPage || remitosLoading}
              className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
            >
              Anterior
            </button>
            <button
              type="button"
              onClick={() => goToRemitoPage(currentRemitoPage + 1)}
              disabled={!remitosPagination?.hasNextPage || remitosLoading}
              className="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
            >
              Siguiente
            </button>
          </div>
        </div>
      </section>
      )}

      <RemitoPdfProgressModal
        state={remitoPdf}
        onClose={closeRemitoPdfModal}
        onOpenPdf={openManualRemitoPdf}
      />

      {pendingDetailOrder && (
        <ResponsiveModalOverlay className="bg-black/35">
          <ResponsiveModalPanel className="max-w-5xl" role="dialog" aria-modal="true">
            <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
              <div>
                <h2 className="text-base font-semibold">Detalle del remito pendiente</h2>
                <p className="mt-0.5 text-sm text-gray-600">
                  {pendingDetailOrder.remitoNumber || pendingDetailOrder.orderNumber} · {pendingDetailOrder.customerName || "-"}
                </p>
              </div>
              <button
                type="button"
                onClick={closePendingDetailModal}
                className="inline-flex h-8 w-8 items-center justify-center rounded border text-gray-700 hover:bg-gray-50"
                aria-label="Cerrar"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="max-h-[calc(100vh-9rem)] space-y-4 overflow-y-auto p-4">
              <DeliveryOrderBillingDetails order={pendingDetailOrder} />
              {pendingError && <div className="text-sm text-red-700">{pendingError}</div>}
            </div>
            <div className="flex justify-end gap-2 border-t px-4 py-3">
              <button
                type="button"
                onClick={closePendingDetailModal}
                className="rounded border px-3 py-2 text-sm hover:bg-gray-50"
              >
                Cerrar
              </button>
              <button
                type="button"
                onClick={() => markOrderNotBillable(pendingDetailOrder)}
                disabled={savingNotBillableId === pendingDetailOrder.id}
                className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50"
              >
                {savingNotBillableId === pendingDetailOrder.id ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                ) : (
                  <X className="h-4 w-4" aria-hidden="true" />
                )}
                No se factura
              </button>
              <button
                type="button"
                onClick={() => openInvoiceModal(pendingDetailOrder)}
                disabled={savingInvoice}
                className="inline-flex items-center gap-2 rounded bg-emerald-600 px-3 py-2 text-sm text-white hover:bg-emerald-700 disabled:opacity-50"
              >
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                Registrar factura
              </button>
            </div>
          </ResponsiveModalPanel>
        </ResponsiveModalOverlay>
      )}

      {invoiceOrder && (
        <ResponsiveModalOverlay className="bg-black/35">
          <ResponsiveModalPanel className="max-w-4xl" role="dialog" aria-modal="true">
            <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
              <div>
                <h2 className="text-base font-semibold">Registrar factura</h2>
                <p className="mt-0.5 text-sm text-gray-600">
                  {invoiceOrder.remitoNumber || invoiceOrder.orderNumber} · {invoiceOrder.customerName || "-"}
                </p>
              </div>
              <button
                type="button"
                onClick={closeInvoiceModal}
                disabled={savingInvoice}
                className="inline-flex h-8 w-8 items-center justify-center rounded border text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                aria-label="Cerrar"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <form onSubmit={submitInvoice} className="max-h-[calc(100vh-9rem)] space-y-4 overflow-y-auto p-4">
              <DeliveryOrderBillingDetails order={invoiceOrder} />
              <label className="block text-sm">
                <span className="mb-1 block text-xs uppercase text-gray-500">Número de factura</span>
                <input
                  autoFocus
                  value={invoiceNumber}
                  onChange={(event) => setInvoiceNumber(event.target.value)}
                  className="h-9 w-full rounded border px-2"
                  placeholder="FC A 0001-00000000"
                />
              </label>
              {invoiceError && <div className="text-sm text-red-700">{invoiceError}</div>}
              <div className="flex justify-end gap-2 pt-1">
                <button
                  type="button"
                  onClick={closeInvoiceModal}
                  disabled={savingInvoice}
                  className="rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50"
                >
                  Cancelar
                </button>
                <button
                  type="submit"
                  disabled={savingInvoice}
                  className="inline-flex items-center gap-2 rounded bg-emerald-600 px-3 py-2 text-sm text-white hover:bg-emerald-700 disabled:opacity-50"
                >
                  {savingInvoice ? (
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  )}
                  Guardar
                </button>
              </div>
            </form>
          </ResponsiveModalPanel>
        </ResponsiveModalOverlay>
      )}
    </div>
  );
}
