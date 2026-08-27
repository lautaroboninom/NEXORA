import { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  FileText,
  Loader2,
  Pencil,
  Search,
  X,
} from "lucide-react";
import {
  billingRemitoPdfUrl,
  getBillingCustomers,
  getBillingDocumentPdfBlob,
  getBillingDocuments,
  getBillingRemitos,
  getDeliveryOrder,
  getDeliveryOrders,
  postDeliveryOrderBillingPostponement,
  postDeliveryOrderBillingReactivation,
  postDeliveryOrderInvoiced,
  postDeliveryOrderNotBillable,
  postDeliveryOrdersInvoicedBatch,
} from "../lib/api";
import { openPrintablePdf, PDF_REPRINT_STEPS, reservePdfWindow, waitForPdfBlob } from "../lib/pdf";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import DeliveryOrderCommercialCorrectionModal from "../components/DeliveryOrderCommercialCorrectionModal.jsx";
import { remitoDocumentNumber } from "../lib/remitos";
import {
  deliveryOrderBejermanCustomerLabel,
  deliveryOrderCommercialLabel,
  deliveryOrderIsOccasionalCustomer,
  deliveryOrderOccasionalCustomerDocument,
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
import { MobileSortControl, SortableTh } from "../components/TableSortControls.jsx";
import useQueryState from "../hooks/useQueryState";

const BILLABLE_REMITO_TYPES = new Set(["RT"]);
const BILLING_PAGE_SIZE = 25;
const BILLING_SORT_OPTIONS = [
  { value: "fecha", label: "Fecha" },
  { value: "cliente", label: "Cliente" },
  { value: "comprobante", label: "Comprobante" },
  { value: "numero", label: "Número" },
  { value: "total", label: "Total" },
];
const REMITO_SORT_OPTIONS = [
  { value: "fecha", label: "Fecha" },
  { value: "cliente", label: "Cliente" },
  { value: "codigo", label: "Código" },
  { value: "tipo", label: "Tipo" },
  { value: "operacion", label: "Operación" },
  { value: "numero", label: "Número" },
  { value: "total", label: "Total" },
];
const PENDING_BILLING_PAGE_SIZE = 200;
const BILLING_COMPANY_BY_POINT_OF_SALE = {
  "00004": "SEPID",
  "00007": "MGBIO",
};
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

function orderIdFromSearch(search) {
  try {
    return clean(new URLSearchParams(search || "").get("orderId"));
  } catch {
    return "";
  }
}

function serviceOrderIdFromSearch(search) {
  try {
    return clean(new URLSearchParams(search || "").get("serviceOrderId"));
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

function tomorrowDateInputValue() {
  const date = new Date();
  date.setDate(date.getDate() + 1);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
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
  const uniqueItems = [];
  const seenIds = new Set();
  for (const order of items) {
    const orderId = clean(order?.id);
    if (orderId && seenIds.has(orderId)) continue;
    if (orderId) seenIds.add(orderId);
    uniqueItems.push(order);
  }
  return uniqueItems
    .filter((order) => !clean(order?.invoiceNumber))
    .sort((a, b) => {
      const byDate = pendingRemitoEmissionTime(a) - pendingRemitoEmissionTime(b);
      if (byDate) return byDate;
      const byOrder = clean(a?.orderNumber).localeCompare(clean(b?.orderNumber), "es-AR", { numeric: true });
      if (byOrder) return byOrder;
      return clean(a?.id).localeCompare(clean(b?.id), "es-AR", { numeric: true });
    });
}

function pendingBillingUnitKey(order) {
  const remitoGroupId = clean(order?.bejermanRemitoGroupId);
  return remitoGroupId ? `remito:${remitoGroupId}` : `orden:${clean(order?.id)}`;
}

function pendingBillingCustomerCode(order) {
  return clean(order?.bejermanRemitoGroup?.customerCode) || clean(order?.bejermanCustomerCode);
}

function normalizeBillingCompanyKey(value) {
  const companyKey = clean(value).replace(/[\s_-]/g, "").toUpperCase();
  if (companyKey === "SEPID" || companyKey === "SEP" || companyKey === "SEPISA") return "SEPID";
  if (["MG", "MGB", "MGBI", "MGBIO", "MGBIOSA", "PORMG"].includes(companyKey)) return "MGBIO";
  return "";
}

function normalizeBillingPointOfSale(value) {
  const raw = clean(value);
  if (!raw) return "";
  if (/^\d{1,5}$/.test(raw)) return raw.padStart(5, "0");
  return raw;
}

function billingPointOfSaleFromRemitoNumber(value) {
  const match = clean(value).match(/(?:^|\s)(\d{1,5})\s*-\s*\d{1,8}(?:\b|$)/);
  return match ? normalizeBillingPointOfSale(match[1]) : "";
}

function pendingBillingPointOfSale(order) {
  const remitoGroup = order?.bejermanRemitoGroup || {};
  const responseSummary = remitoGroup?.responseSummary || {};
  return (
    normalizeBillingPointOfSale(order?.billingPointOfSale) ||
    normalizeBillingPointOfSale(remitoGroup?.comprobantePtoVenta) ||
    normalizeBillingPointOfSale(responseSummary?.comprobantePtoVenta) ||
    normalizeBillingPointOfSale(responseSummary?.comprobante_pto_venta) ||
    normalizeBillingPointOfSale(responseSummary?.pointOfSale) ||
    billingPointOfSaleFromRemitoNumber(remitoGroup?.remitoNumber || order?.remitoNumber)
  );
}

function pendingBillingCompanyKey(order) {
  const pointCompanyKey = BILLING_COMPANY_BY_POINT_OF_SALE[pendingBillingPointOfSale(order)];
  if (pointCompanyKey) return pointCompanyKey;

  const resolvedCompanyKey = normalizeBillingCompanyKey(order?.billingCompanyKey);
  if (resolvedCompanyKey) return resolvedCompanyKey;

  const remitoCompanyKey = normalizeBillingCompanyKey(order?.bejermanRemitoGroup?.companyKey);
  if (remitoCompanyKey) return remitoCompanyKey;

  const orderCompanyKey = normalizeBillingCompanyKey(order?.companyKey);
  if (orderCompanyKey) return orderCompanyKey;

  return normalizeBillingCompanyKey(order?.operationCompanyLabel || order?.sourceCompanyId);
}

function billingCompanyLabel(companyKey) {
  if (companyKey === "MGBIO") return "MG BIO";
  if (companyKey === "SEPID") return "SEPID";
  return "Empresa sin determinar";
}

function pendingBillingCompanyLabel(order) {
  return billingCompanyLabel(pendingBillingCompanyKey(order));
}

// Un particular no tiene código Bejerman y comparte la fila `customers` con todos los demás:
// agrupar por customerId los mezclaría. El documento del propietario es la identidad real, y es
// además lo que el backend exige para dejar facturar en grupo.
function pendingBillingOccasionalDocument(order) {
  return deliveryOrderOccasionalCustomerDocument(order);
}

function pendingBillingCustomerGroupKey(order) {
  const customerCode = pendingBillingCustomerCode(order);
  if (!customerCode) {
    const occasionalDocument = pendingBillingOccasionalDocument(order);
    return occasionalDocument ? `particular-doc:${occasionalDocument}` : `individual:${clean(order?.id)}`;
  }
  const customerId = clean(order?.customerId);
  return customerId ? `cliente-id:${customerId}` : `cliente-codigo:${customerCode.toUpperCase()}`;
}

function pendingBillingCompanyGroupKey(order, customerGroupKey = pendingBillingCustomerGroupKey(order)) {
  const customerCode = pendingBillingCustomerCode(order).toUpperCase();
  const occasionalDocument = pendingBillingOccasionalDocument(order);
  const companyKey = pendingBillingCompanyKey(order) || "SIN_EMPRESA";
  const individualSuffix = customerCode || occasionalDocument ? "" : `:${clean(order?.id)}`;
  const codeLabel = customerCode || (occasionalDocument ? `OCASIONAL-${occasionalDocument}` : "SIN_CODIGO");
  return `${customerGroupKey}:empresa:${companyKey}:codigo:${codeLabel}${individualSuffix}`;
}

function aggregateDeliveryOrderTotals(orders) {
  const totalsByCurrency = {};
  let grossTotal = 0;
  let discountTotal = 0;
  let total = 0;
  let itemCount = 0;
  let pricedItems = 0;
  let missingPriceItems = 0;

  for (const order of orders) {
    const orderTotals = deliveryOrderItemsTotals(order);
    itemCount += orderTotals.itemCount;
    pricedItems += orderTotals.pricedItems;
    missingPriceItems += orderTotals.missingPriceItems;
    grossTotal += orderTotals.grossTotal;
    discountTotal += orderTotals.discountTotal;
    total += orderTotals.total;
    for (const currency of orderTotals.currencies || []) {
      const source = orderTotals.totalsByCurrency?.[currency];
      if (!source) continue;
      const bucket = totalsByCurrency[currency] || {
        currency,
        grossTotal: 0,
        discountTotal: 0,
        total: 0,
        pricedItems: 0,
        missingPriceItems: 0,
      };
      bucket.grossTotal += source.grossTotal;
      bucket.discountTotal += source.discountTotal;
      bucket.total += source.total;
      bucket.pricedItems += source.pricedItems;
      bucket.missingPriceItems += source.missingPriceItems;
      totalsByCurrency[currency] = bucket;
    }
  }

  const currencies = ["ARS", "USD"].filter((currency) => totalsByCurrency[currency]);
  return {
    itemCount,
    pricedItems,
    missingPriceItems,
    hasMissingPrices: missingPriceItems > 0,
    currency: currencies.length > 1 ? "MIXED" : currencies[0] || "ARS",
    currencies: currencies.length ? currencies : ["ARS"],
    mixedCurrency: currencies.length > 1,
    totalsByCurrency,
    grossTotal,
    discountTotal,
    total,
  };
}

function pendingBillingUnits(orders) {
  const byKey = new Map();
  for (const order of orders) {
    const key = pendingBillingUnitKey(order);
    const unit = byKey.get(key) || { key, orders: [] };
    unit.orders.push(order);
    byKey.set(key, unit);
  }
  return [...byKey.values()].map((unit) => ({
    ...unit,
    remitoNumber: unit.orders.find((order) => clean(order?.remitoNumber))?.remitoNumber || "",
    totals: aggregateDeliveryOrderTotals(unit.orders),
  }));
}

function groupPendingBillingOrders(orders) {
  const customerGroupsByKey = new Map();
  for (const order of orders) {
    const key = pendingBillingCustomerGroupKey(order);
    const customerGroup = customerGroupsByKey.get(key) || {
      key,
      customerId: clean(order?.customerId),
      customerName: clean(order?.customerName) || "Cliente sin nombre",
      orders: [],
    };
    customerGroup.orders.push(order);
    customerGroupsByKey.set(key, customerGroup);
  }

  return [...customerGroupsByKey.values()]
    .map((customerGroup) => {
      const companyGroupsByKey = new Map();
      for (const order of customerGroup.orders) {
        const key = pendingBillingCompanyGroupKey(order, customerGroup.key);
        const companyKey = pendingBillingCompanyKey(order);
        const companyGroup = companyGroupsByKey.get(key) || {
          key,
          customerName: customerGroup.customerName,
          customerCode: pendingBillingCustomerCode(order),
          occasionalDocument: pendingBillingOccasionalDocument(order),
          occasionalLabel: deliveryOrderIsOccasionalCustomer(order) ? deliveryOrderBejermanCustomerLabel(order) : "",
          companyKey,
          companyLabel: billingCompanyLabel(companyKey),
          orders: [],
        };
        companyGroup.orders.push(order);
        companyGroupsByKey.set(key, companyGroup);
      }

      const companyGroups = [...companyGroupsByKey.values()]
        .map((companyGroup) => {
          const pointOfSales = [
            ...new Set(companyGroup.orders.map(pendingBillingPointOfSale).filter(Boolean)),
          ].sort((a, b) => a.localeCompare(b, "es-AR", { numeric: true }));
          return {
            ...companyGroup,
            pointOfSales,
            batchEligible: Boolean(
              (companyGroup.customerCode || companyGroup.occasionalDocument) && companyGroup.companyKey
            ),
            oldestPendingTime: Math.min(...companyGroup.orders.map(pendingRemitoEmissionTime)),
            units: pendingBillingUnits(companyGroup.orders),
            totals: aggregateDeliveryOrderTotals(companyGroup.orders),
          };
        })
        .sort((a, b) => {
          const byDate = a.oldestPendingTime - b.oldestPendingTime;
          if (byDate) return byDate;
          const byCompany = a.companyLabel.localeCompare(b.companyLabel, "es-AR", { sensitivity: "base" });
          if (byCompany) return byCompany;
          return a.key.localeCompare(b.key, "es-AR", { numeric: true });
        });
      const customerCodes = [
        ...new Set(customerGroup.orders.map(pendingBillingCustomerCode).filter(Boolean)),
      ];
      const occasionalLabel = customerCodes.length
        ? ""
        : deliveryOrderBejermanCustomerLabel(customerGroup.orders[0]);
      return {
        ...customerGroup,
        customerCodes,
        occasionalLabel,
        companyGroups,
        oldestPendingTime: Math.min(...customerGroup.orders.map(pendingRemitoEmissionTime)),
        units: pendingBillingUnits(customerGroup.orders),
        totals: aggregateDeliveryOrderTotals(customerGroup.orders),
      };
    })
    .sort((a, b) => {
      const byDate = a.oldestPendingTime - b.oldestPendingTime;
      if (byDate) return byDate;
      const byCustomer = a.customerName.localeCompare(b.customerName, "es-AR", { sensitivity: "base" });
      if (byCustomer) return byCustomer;
      return a.key.localeCompare(b.key, "es-AR", { numeric: true });
    });
}

function findPendingBillingContext(groups, orderId) {
  const requestedId = clean(orderId);
  if (!requestedId) return null;
  for (const customerGroup of groups) {
    for (const companyGroup of customerGroup.companyGroups || []) {
      if (companyGroup.orders.some((order) => clean(order?.id) === requestedId)) {
        return { customerGroup, companyGroup };
      }
    }
  }
  return null;
}

async function getAllPendingBillingOrders() {
  const items = [];
  let offset = 0;
  let pageCount = 0;
  let expectedTotal = 0;

  while (true) {
    const data = await getDeliveryOrders({
      pendingBilling: true,
      limit: PENDING_BILLING_PAGE_SIZE,
      offset,
    });
    if (!Array.isArray(data?.items)) {
      throw new Error("No se pudo confirmar la lista completa de remitos pendientes.");
    }
    const pageItems = data.items;
    const reportedTotal = Number.parseInt(data?.total, 10);
    const hasReportedTotal = Number.isFinite(reportedTotal) && reportedTotal >= 0;
    if (!hasReportedTotal) {
      throw new Error("No se pudo confirmar la cantidad total de remitos pendientes.");
    }
    expectedTotal = reportedTotal;
    items.push(...pageItems);
    offset += pageItems.length;
    pageCount += 1;

    if (offset >= reportedTotal) break;
    if (!pageItems.length) {
      throw new Error("La carga de remitos pendientes quedó incompleta.");
    }
    if (pageCount >= 1000) throw new Error("La carga de remitos pendientes excedió el límite seguro.");
  }

  const uniqueItems = new Map();
  for (const order of items) {
    const orderId = clean(order?.id);
    if (orderId) uniqueItems.set(orderId, order);
  }
  const normalizedItems = normalizePendingBillingOrders([...uniqueItems.values()]);
  if (normalizedItems.length !== expectedTotal) {
    throw new Error("La carga de remitos pendientes quedó incompleta.");
  }
  return normalizedItems;
}

function orderMatchesServiceOrderId(order, serviceOrderId) {
  const requested = clean(serviceOrderId);
  if (!requested) return false;
  const numeric = /^\d+$/.test(requested) ? String(Number(requested)) : requested;
  return [order?.ingresoId, order?.sourceReference]
    .map((value) => clean(value))
    .some((value) => value === requested || value === numeric || value === `OS-${numeric.padStart(5, "0")}`);
}

function orderSubtitle(order) {
  return [
    clean(order?.orderNumber),
    pendingBillingCustomerCode(order) || deliveryOrderBejermanCustomerLabel(order) || "sin código Bejerman",
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
  const billingRequiredText = clean(billingRequired).toLowerCase();
  return (
    billingRequired === true ||
    billingRequiredText === "true" ||
    BILLABLE_REMITO_TYPES.has(remitoTypeFromOrder(order))
  );
}

function canCorrectCommercialTerms(user, order) {
  return can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_CORRECT_COMMERCIAL_TERMS) && hasPendingBilling(order);
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
        <DetailItem
          label="Código Bejerman"
          value={pendingBillingCustomerCode(order) || deliveryOrderBejermanCustomerLabel(order) || "sin código"}
        />
        <DetailItem label="Empresa" value={pendingBillingCompanyLabel(order)} />
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

function BillingTotalsByCurrency({ totals, label = "Total estimado" }) {
  const currencies = ["ARS", "USD"].filter((currency) => totals?.totalsByCurrency?.[currency]);
  if (!currencies.length) return <span>{label}: -</span>;
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1">
      {currencies.map((currency) => (
        <span key={currency}>
          {label} {currency}: {formatOrderMoney(totals.totalsByCurrency[currency].total, currency)}
        </span>
      ))}
    </div>
  );
}

function PendingBillingOrderRow({
  order,
  user,
  selectedOrderId,
  savingInvoice,
  savingPostponement,
  postponementOrder,
  savingNotBillableId,
  onSelect,
  onCorrectCommercial,
  onInvoice,
  onPostpone,
  onNotBillable,
}) {
  const totals = deliveryOrderItemsTotals(order);
  const selected = clean(selectedOrderId) === clean(order?.id);
  return (
    <div
      id={`pending-order-${order.id}`}
      className={`px-3 py-3 text-sm ${selected ? "bg-emerald-50" : "bg-white"}`}
    >
      <button type="button" onClick={() => onSelect(order)} className="block w-full text-left">
        <div className="font-medium text-gray-950">{order.remitoNumber || order.orderNumber}</div>
        <div className="mt-0.5 text-xs text-gray-500">{orderSubtitle(order)}</div>
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
      {!pendingBillingCustomerCode(order) && !deliveryOrderIsOccasionalCustomer(order) && (
        <div className="mt-2 flex items-start gap-1.5 text-xs text-amber-800">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          Sin código Bejerman
        </div>
      )}
      {!pendingBillingCompanyKey(order) && (
        <div className="mt-2 flex items-start gap-1.5 text-xs text-amber-800">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          Empresa sin determinar
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {canCorrectCommercialTerms(user, order) && (
          <button
            type="button"
            onClick={() => onCorrectCommercial(order)}
            className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-gray-50"
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
            Corregir precio/plazo
          </button>
        )}
        <button
          type="button"
          onClick={() => onInvoice(order)}
          disabled={savingInvoice}
          className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
        >
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
          Registrar factura
        </button>
        <button
          type="button"
          onClick={() => onPostpone(order)}
          disabled={savingPostponement}
          className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
        >
          {savingPostponement && postponementOrder?.id === order.id ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          Posponer
        </button>
        <button
          type="button"
          onClick={() => onNotBillable(order)}
          disabled={savingNotBillableId === order.id}
          className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
        >
          {savingNotBillableId === order.id ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          No se factura
        </button>
      </div>
    </div>
  );
}

export default function Billing() {
  const location = useLocation();
  const { user } = useAuth();
  const isRemitosRoute = location.pathname.endsWith("/remitos");
  const shouldRedirectToFacturacion = isRemitosRoute && /\b(?:orderId|serviceOrderId)=/.test(location.search || "");
  const requestedOrderId = useMemo(() => orderIdFromSearch(location.search), [location.search]);
  const requestedServiceOrderId = useMemo(() => serviceOrderIdFromSearch(location.search), [location.search]);
  const [customers, setCustomers] = useState([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [customerQuery, setCustomerQuery] = useState("");
  const [customerSuggestionsOpen, setCustomerSuggestionsOpen] = useState(false);
  const [customerActiveIndex, setCustomerActiveIndex] = useState(0);
  const [filters, setFilters] = useState({ dateFrom: "", dateTo: "", search: "" });
  const [documents, setDocuments] = useState([]);
  const [pagination, setPagination] = useState(EMPTY_PAGINATION);
  const [page, setPage] = useState(1);
  const [documentSort, setDocumentSort] = useQueryState("sort_facturacion", "");
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
  const [remitoSort, setRemitoSort] = useQueryState("sort_remitos", "");
  const [pendingOrders, setPendingOrders] = useState([]);
  const [postponedOrders, setPostponedOrders] = useState([]);
  const [pendingListComplete, setPendingListComplete] = useState(false);
  const [expandedPendingCustomerKeys, setExpandedPendingCustomerKeys] = useState([]);
  const [expandedPendingCompanyGroupKeys, setExpandedPendingCompanyGroupKeys] = useState([]);
  const [selectedPendingOrderId, setSelectedPendingOrderId] = useState("");
  const [pendingDetailOrder, setPendingDetailOrder] = useState(null);
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [remitosLoading, setRemitosLoading] = useState(false);
  const [pendingLoading, setPendingLoading] = useState(false);
  const [postponedLoading, setPostponedLoading] = useState(false);
  const [savingInvoice, setSavingInvoice] = useState(false);
  const [invoiceOrder, setInvoiceOrder] = useState(null);
  const [commercialCorrectionOrder, setCommercialCorrectionOrder] = useState(null);
  const [invoiceNumber, setInvoiceNumber] = useState("");
  const [invoiceError, setInvoiceError] = useState("");
  const [batchInvoiceGroupKey, setBatchInvoiceGroupKey] = useState("");
  const [batchInvoiceUnitKeys, setBatchInvoiceUnitKeys] = useState([]);
  const [batchInvoiceNumber, setBatchInvoiceNumber] = useState("");
  const [batchInvoiceError, setBatchInvoiceError] = useState("");
  const [savingBatchInvoice, setSavingBatchInvoice] = useState(false);
  const [error, setError] = useState("");
  const [documentsWarning, setDocumentsWarning] = useState("");
  const [remitoError, setRemitoError] = useState("");
  const [remitoWarning, setRemitoWarning] = useState("");
  const [remitoPdf, setRemitoPdf] = useState(initialRemitoPdfState);
  const [loadingDocumentPdfId, setLoadingDocumentPdfId] = useState("");
  const [loadingRemitoPdfId, setLoadingRemitoPdfId] = useState("");
  const [pendingError, setPendingError] = useState("");
  const [pendingSuccess, setPendingSuccess] = useState("");
  const [savingNotBillableId, setSavingNotBillableId] = useState("");
  const [postponementOrder, setPostponementOrder] = useState(null);
  const [postponementDate, setPostponementDate] = useState("");
  const [postponementError, setPostponementError] = useState("");
  const [savingPostponement, setSavingPostponement] = useState(false);
  const [savingReactivationId, setSavingReactivationId] = useState("");
  const [postponedOpen, setPostponedOpen] = useState(false);

  const selectedPendingOrder = useMemo(
    () => pendingOrders.find((order) => clean(order?.id) === clean(selectedPendingOrderId)) || null,
    [pendingOrders, selectedPendingOrderId]
  );

  const pendingGroups = useMemo(() => groupPendingBillingOrders(pendingOrders), [pendingOrders]);
  const batchInvoiceGroup = useMemo(
    () =>
      pendingGroups
        .flatMap((customerGroup) => customerGroup.companyGroups || [])
        .find((companyGroup) => companyGroup.key === batchInvoiceGroupKey) || null,
    [batchInvoiceGroupKey, pendingGroups]
  );
  const selectedBatchInvoiceUnitKeys = useMemo(() => new Set(batchInvoiceUnitKeys), [batchInvoiceUnitKeys]);
  const selectedBatchInvoiceOrders = useMemo(
    () =>
      (batchInvoiceGroup?.units || [])
        .filter((unit) => selectedBatchInvoiceUnitKeys.has(unit.key))
        .flatMap((unit) => unit.orders),
    [batchInvoiceGroup, selectedBatchInvoiceUnitKeys]
  );
  const selectedBatchInvoiceTotals = useMemo(
    () => aggregateDeliveryOrderTotals(selectedBatchInvoiceOrders),
    [selectedBatchInvoiceOrders]
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

  const loadDocuments = async (
    customerCode = selectedCode,
    pageNumber = page,
    sortValue = documentSort
  ) => {
    const code = clean(customerCode);
    setDocumentsLoading(true);
    try {
      const params = {
        ...filters,
        page: pageNumber,
        pageSize: BILLING_PAGE_SIZE,
        sort: sortValue || undefined,
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

  const loadRemitos = async (
    customerCode = selectedRemitoCode,
    pageNumber = remitoPage,
    sortValue = remitoSort
  ) => {
    const code = clean(customerCode);
    setRemitosLoading(true);
    try {
      const params = {
        ...remitoFilters,
        page: pageNumber,
        pageSize: BILLING_PAGE_SIZE,
        sort: sortValue || undefined,
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
    setPendingListComplete(false);
    try {
      let items = await getAllPendingBillingOrders();
      items = normalizePendingBillingOrders(items);
      setPendingOrders(items);
      const preferredExists =
        preferredSelectedId && items.some((order) => clean(order?.id) === clean(preferredSelectedId));
      const requestedExists =
        allowRequestedSelection &&
        requestedOrderId &&
        items.some((order) => clean(order?.id) === clean(requestedOrderId));
      const requestedServiceOrder = allowRequestedSelection && requestedServiceOrderId
        ? items.find((order) => orderMatchesServiceOrderId(order, requestedServiceOrderId))
        : null;
      const nextSelectedId = preferredExists ? preferredSelectedId : requestedExists ? requestedOrderId : requestedServiceOrder?.id || "";
      setSelectedPendingOrderId(nextSelectedId);
      setPendingError("");
      setPendingListComplete(true);
    } catch (err) {
      setPendingOrders([]);
      setPendingError(err?.message || "No se pudieron cargar todos los remitos pendientes.");
      setPendingListComplete(false);
    } finally {
      setPendingLoading(false);
    }
  };

  const loadPostponedOrders = async () => {
    setPostponedLoading(true);
    try {
      const data = await getDeliveryOrders({ postponedBilling: true, limit: 200 });
      setPostponedOrders(Array.isArray(data?.items) ? data.items : []);
    } catch (err) {
      setPendingError(err?.message || "No se pudieron cargar los remitos pospuestos.");
      setPostponedOrders([]);
    } finally {
      setPostponedLoading(false);
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
    loadPostponedOrders();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isRemitosRoute, requestedOrderId, requestedServiceOrderId]);

  useEffect(() => {
    if (!isRemitosRoute) return;
    loadRemitos("", 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isRemitosRoute]);

  const remitoSearchSkipNextRef = useRef(true);
  useEffect(() => {
    if (!isRemitosRoute) return;
    if (remitoSearchSkipNextRef.current) {
      remitoSearchSkipNextRef.current = false;
      return;
    }
    const timeoutId = setTimeout(() => {
      setRemitoPage(1);
      loadRemitos(selectedRemitoCode, 1);
    }, 400);
    return () => clearTimeout(timeoutId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remitoFilters.search]);

  useEffect(() => {
    const selectedId = clean(selectedPendingOrderId);
    if (!selectedId) return;
    const selectedContext = findPendingBillingContext(pendingGroups, selectedId);
    if (!selectedContext) return;
    setExpandedPendingCustomerKeys((current) =>
      current.includes(selectedContext.customerGroup.key) ? current : [...current, selectedContext.customerGroup.key]
    );
    setExpandedPendingCompanyGroupKeys((current) =>
      current.includes(selectedContext.companyGroup.key) ? current : [...current, selectedContext.companyGroup.key]
    );
  }, [pendingGroups, selectedPendingOrderId]);

  useEffect(() => {
    if (pendingLoading || (!requestedOrderId && !requestedServiceOrderId)) return undefined;
    const selectedId = clean(selectedPendingOrderId);
    if (!selectedId) return undefined;
    const selectedContext = findPendingBillingContext(pendingGroups, selectedId);
    if (
      !selectedContext ||
      !expandedPendingCustomerKeys.includes(selectedContext.customerGroup.key) ||
      !expandedPendingCompanyGroupKeys.includes(selectedContext.companyGroup.key)
    ) return undefined;
    const timer = window.setTimeout(() => {
      document.getElementById(`pending-order-${selectedId}`)?.scrollIntoView({ block: "nearest" });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    expandedPendingCompanyGroupKeys,
    expandedPendingCustomerKeys,
    pendingGroups,
    pendingLoading,
    requestedOrderId,
    requestedServiceOrderId,
    selectedPendingOrderId,
  ]);

  const selectPendingOrder = (order) => {
    setSelectedPendingOrderId(order.id);
    setPendingDetailOrder(order);
  };

  const togglePendingCustomerGroup = (groupKey) => {
    setExpandedPendingCustomerKeys((current) =>
      current.includes(groupKey) ? current.filter((key) => key !== groupKey) : [...current, groupKey]
    );
  };

  const togglePendingCompanyGroup = (groupKey) => {
    setExpandedPendingCompanyGroupKeys((current) =>
      current.includes(groupKey) ? current.filter((key) => key !== groupKey) : [...current, groupKey]
    );
  };

  const openBatchInvoiceModal = (group) => {
    if (!pendingListComplete || !group?.batchEligible) return;
    setBatchInvoiceGroupKey(group.key);
    setBatchInvoiceUnitKeys(group.units.map((unit) => unit.key));
    setBatchInvoiceNumber("");
    setBatchInvoiceError("");
    setPendingSuccess("");
  };

  const closeBatchInvoiceModal = () => {
    if (savingBatchInvoice) return;
    setBatchInvoiceGroupKey("");
    setBatchInvoiceUnitKeys([]);
    setBatchInvoiceNumber("");
    setBatchInvoiceError("");
  };

  const toggleBatchInvoiceUnit = (unitKey) => {
    setBatchInvoiceUnitKeys((current) =>
      current.includes(unitKey) ? current.filter((key) => key !== unitKey) : [...current, unitKey]
    );
    setBatchInvoiceError("");
  };

  const toggleAllBatchInvoiceUnits = () => {
    if (!batchInvoiceGroup) return;
    const allSelected = batchInvoiceUnitKeys.length === batchInvoiceGroup.units.length;
    setBatchInvoiceUnitKeys(allSelected ? [] : batchInvoiceGroup.units.map((unit) => unit.key));
    setBatchInvoiceError("");
  };

  const submitBatchInvoice = async (event) => {
    event.preventDefault();
    const invoice = batchInvoiceNumber.trim();
    if (!invoice) {
      setBatchInvoiceError("Ingrese el número de factura.");
      return;
    }
    if (!selectedBatchInvoiceOrders.length) {
      setBatchInvoiceError("Seleccione al menos un remito para facturar.");
      return;
    }

    const orderIds = selectedBatchInvoiceOrders.map((order) => order.id);
    const selectedRemitoCount = batchInvoiceUnitKeys.length;
    setSavingBatchInvoice(true);
    setBatchInvoiceError("");
    try {
      const result = await postDeliveryOrdersInvoicedBatch({ orderIds, invoiceNumber: invoice });
      const updatedCount = Number.parseInt(result?.count, 10);
      const orderCount = Number.isFinite(updatedCount) ? updatedCount : orderIds.length;
      setBatchInvoiceGroupKey("");
      setBatchInvoiceUnitKeys([]);
      setBatchInvoiceNumber("");
      setPendingDetailOrder(null);
      setSelectedPendingOrderId("");
      setPendingSuccess(
        `Factura ${invoice} registrada para ${selectedRemitoCount} remito${selectedRemitoCount === 1 ? "" : "s"} y ${orderCount} OE.`
      );
      await loadPendingOrders({ preferredSelectedId: "", allowRequestedSelection: false });
    } catch (err) {
      setBatchInvoiceError(err?.message || "No se pudo registrar la factura agrupada.");
    } finally {
      setSavingBatchInvoice(false);
    }
  };

  const openInvoiceModal = (order) => {
    setSelectedPendingOrderId(order.id);
    setPendingDetailOrder(null);
    setInvoiceOrder(order);
    setInvoiceNumber("");
    setInvoiceError("");
    setError("");
    setPendingSuccess("");
  };

  const closePendingDetailModal = () => {
    setPendingDetailOrder(null);
  };

  const openPostponementModal = (order) => {
    if (!order?.id) return;
    setPostponementOrder(order);
    setPostponementDate(tomorrowDateInputValue());
    setPostponementError("");
    setPendingError("");
  };

  const closePostponementModal = () => {
    if (savingPostponement) return;
    setPostponementOrder(null);
    setPostponementDate("");
    setPostponementError("");
  };

  const submitPostponement = async (event) => {
    event.preventDefault();
    if (!postponementOrder?.id) return;
    if (!postponementDate) {
      setPostponementError("Seleccione una fecha para reanudar la facturación.");
      return;
    }
    setSavingPostponement(true);
    setPostponementError("");
    try {
      await postDeliveryOrderBillingPostponement(postponementOrder.id, { returnDate: postponementDate });
      if (selectedPendingOrderId === postponementOrder.id) setSelectedPendingOrderId("");
      if (pendingDetailOrder?.id === postponementOrder.id) setPendingDetailOrder(null);
      setPostponementOrder(null);
      setPostponementDate("");
      await Promise.all([
        loadPendingOrders({ preferredSelectedId: "", allowRequestedSelection: false }),
        loadPostponedOrders(),
      ]);
    } catch (err) {
      setPostponementError(err?.message || "No se pudo posponer el remito.");
    } finally {
      setSavingPostponement(false);
    }
  };

  const reactivatePostponedOrder = async (order) => {
    if (!order?.id || savingReactivationId) return;
    setSavingReactivationId(order.id);
    setPendingError("");
    try {
      await postDeliveryOrderBillingReactivation(order.id);
      await Promise.all([
        loadPendingOrders({ preferredSelectedId: order.id, allowRequestedSelection: false }),
        loadPostponedOrders(),
      ]);
    } catch (err) {
      setPendingError(err?.message || "No se pudo reactivar el remito.");
    } finally {
      setSavingReactivationId("");
    }
  };

  const openCommercialCorrection = async (order) => {
    if (!order?.id) return;
    setPendingError("");
    try {
      const refreshed = await getDeliveryOrder(order.id);
      setSelectedPendingOrderId(refreshed?.id || order.id);
      setPendingDetailOrder(null);
      setCommercialCorrectionOrder(refreshed);
    } catch (err) {
      setPendingError(err?.message || "No se pudo abrir la corrección comercial.");
    }
  };

  const closeCommercialCorrection = () => {
    setCommercialCorrectionOrder(null);
  };

  const handleCommercialCorrectionSaved = async (saved) => {
    setCommercialCorrectionOrder(null);
    if (saved?.id) {
      setSelectedPendingOrderId(saved.id);
      setPendingDetailOrder(saved);
    }
    await loadPendingOrders({
      preferredSelectedId: saved?.id || selectedPendingOrderId,
      allowRequestedSelection: false,
    });
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
      setPendingSuccess(`Factura ${invoice} registrada para ${invoiceOrder.remitoNumber || invoiceOrder.orderNumber}.`);
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
      title: "Remito - NEXORA",
      heading: `Abriendo ${documentLabel || "el remito"}`,
      message: "Buscando el PDF en Bejerman",
      detail: "Esta pestaña abre la impresión cuando el archivo esté listo.",
      fallbackMessage:
        "NEXORA sigue preparando el PDF del remito. Si esta pestaña no cambia, puede cerrarla y reintentar desde Cobranzas.",
      steps: PDF_REPRINT_STEPS,
    });
    setRemitoError("");
    setLoadingRemitoPdfId(documentId);
    setRemitoPdf({
      open: true,
      loading: true,
      documentLabel,
      status: "Buscando PDF del remito...",
      detail: "NEXORA está comprobando el PDF sin abrir una nueva ventana.",
      error: "",
      blob: null,
    });
    try {
      const blob = await waitForPdfBlob(pdfUrl, {
        label: documentLabel || "remito",
        reservedWindow,
        onProgress: (progress) => {
          setRemitoPdf((current) => ({
            ...current,
            status: progress?.status || "Preparando PDF del remito...",
            detail: progress?.detail || "NEXORA sigue preparando el PDF del remito.",
          }));
        },
      });
      setRemitoPdf((current) => ({
        ...current,
        status: "Abriendo impresión",
        detail: "El PDF está listo. Intentando abrir la ventana de impresión.",
      }));
      reservedWindow.update({
        status: "Abriendo impresión",
        detail: "El PDF está listo. Intentando abrir la ventana de impresión.",
        step: "print",
        state: "ready",
      });
      const opened = reservedWindow.openPrintable(blob, {
        title: "Remito - NEXORA",
        documentLabel: documentLabel || "Remito",
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

  const updateDocumentSort = (value) => {
    setDocumentSort(value);
    setPage(1);
    loadDocuments(resolveCustomerCodeInput(), 1, value);
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

  const updateRemitoSort = (value) => {
    setRemitoSort(value);
    setRemitoPage(1);
    loadRemitos(selectedRemitoCode, 1, value);
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

      <div className={isRemitosRoute ? "contents" : "grid gap-4 xl:grid-cols-[minmax(0,1fr)_440px]"}>
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

          <MobileSortControl
            sort={documentSort}
            onSortChange={updateDocumentSort}
            options={BILLING_SORT_OPTIONS}
            className="px-3 pt-3"
          />
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
                  <SortableTh field="fecha" sort={documentSort} onSortChange={updateDocumentSort} className="px-2 py-2">Fecha</SortableTh>
                  <SortableTh field="cliente" sort={documentSort} onSortChange={updateDocumentSort} className="px-2 py-2">Cliente</SortableTh>
                  <SortableTh field="comprobante" sort={documentSort} onSortChange={updateDocumentSort} className="px-2 py-2">Comprobante</SortableTh>
                  <SortableTh field="numero" sort={documentSort} onSortChange={updateDocumentSort} className="px-2 py-2">Número</SortableTh>
                  <SortableTh field="total" sort={documentSort} onSortChange={updateDocumentSort} className="px-2 py-2 text-right">Total</SortableTh>
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
          <aside className="flex min-w-0 flex-col gap-4">
            <section className="border">
              <div className="flex items-center justify-between border-b px-3 py-2 text-sm font-semibold">
                <span>Remitos pendientes por cliente</span>
                <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-normal text-gray-700">
                  {pendingOrders.length} OE
                </span>
              </div>
              {pendingError && <div className="border-b bg-red-50 px-3 py-2 text-sm text-red-700">{pendingError}</div>}
              {pendingSuccess && (
                <div className="border-b bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{pendingSuccess}</div>
              )}
              <div className="max-h-[720px] overflow-y-auto bg-gray-50/50 p-2">
                {pendingLoading ? (
                  <div className="flex items-center justify-center gap-2 px-3 py-8 text-sm text-gray-500">
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                    Cargando todos los pendientes...
                  </div>
                ) : (
                  <div className="space-y-2">
                    {pendingGroups.map((customerGroup) => {
                      const expanded = expandedPendingCustomerKeys.includes(customerGroup.key);
                      const containsSelectedOrder = customerGroup.orders.some(
                        (order) => clean(order?.id) === clean(selectedPendingOrderId)
                      );
                      const customerCodeLabel = !customerGroup.customerCodes.length
                        ? customerGroup.occasionalLabel || "Sin código Bejerman"
                        : customerGroup.customerCodes.length === 1
                          ? `Código Bejerman ${customerGroup.customerCodes[0]}`
                          : `${customerGroup.customerCodes.length} códigos Bejerman`;
                      return (
                        <article
                          key={customerGroup.key}
                          className={`overflow-hidden rounded border bg-white ${
                            containsSelectedOrder ? "border-emerald-300 ring-1 ring-emerald-200" : ""
                          }`}
                        >
                          <div className={containsSelectedOrder ? "bg-emerald-50" : ""}>
                            <button
                              type="button"
                              onClick={() => togglePendingCustomerGroup(customerGroup.key)}
                              className="flex w-full items-start gap-2 px-3 py-3 text-left hover:bg-gray-50/80"
                              aria-expanded={expanded}
                            >
                              {expanded ? (
                                <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-gray-500" aria-hidden="true" />
                              ) : (
                                <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-gray-500" aria-hidden="true" />
                              )}
                              <span className="min-w-0 flex-1">
                                <span className="block font-semibold text-gray-950">{customerGroup.customerName}</span>
                                <span className="mt-0.5 block text-xs text-gray-600">
                                  {customerCodeLabel}
                                </span>
                                <span className="mt-1 block text-xs text-gray-500">
                                  {customerGroup.companyGroups.length} empresa{customerGroup.companyGroups.length === 1 ? "" : "s"} ·{" "}
                                  {customerGroup.units.length} remito{customerGroup.units.length === 1 ? "" : "s"} ·{" "}
                                  {customerGroup.orders.length} OE
                                </span>
                                <span className="mt-1 block text-xs font-medium text-gray-800">
                                  <BillingTotalsByCurrency totals={customerGroup.totals} />
                                </span>
                              </span>
                            </button>
                          </div>

                          {expanded && (
                            <div className="space-y-2 border-t bg-gray-50 p-2">
                              {customerGroup.companyGroups.map((companyGroup) => {
                                const companyExpanded = expandedPendingCompanyGroupKeys.includes(companyGroup.key);
                                const companyContainsSelectedOrder = companyGroup.orders.some(
                                  (order) => clean(order?.id) === clean(selectedPendingOrderId)
                                );
                                const pointOfSaleLabel = companyGroup.pointOfSales.length
                                  ? `PV ${companyGroup.pointOfSales.join(", ")}`
                                  : "PV sin informar";
                                return (
                                  <section
                                    key={companyGroup.key}
                                    className={`overflow-hidden rounded border bg-white ${
                                      companyContainsSelectedOrder ? "border-emerald-300 ring-1 ring-emerald-100" : ""
                                    }`}
                                  >
                                    <div className={companyContainsSelectedOrder ? "bg-emerald-50" : ""}>
                                      <button
                                        type="button"
                                        onClick={() => togglePendingCompanyGroup(companyGroup.key)}
                                        className="flex w-full items-start gap-2 px-3 py-3 text-left hover:bg-gray-50/80"
                                        aria-expanded={companyExpanded}
                                      >
                                        {companyExpanded ? (
                                          <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-gray-500" aria-hidden="true" />
                                        ) : (
                                          <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-gray-500" aria-hidden="true" />
                                        )}
                                        <span className="min-w-0 flex-1">
                                          <span className="block font-semibold text-gray-950">{companyGroup.companyLabel}</span>
                                          <span className="mt-0.5 block text-xs text-gray-600">
                                            {companyGroup.customerCode ||
                                              companyGroup.occasionalLabel ||
                                              "Sin código Bejerman"}{" "}
                                            · {pointOfSaleLabel}
                                          </span>
                                          <span className="mt-1 block text-xs text-gray-500">
                                            {companyGroup.units.length} remito{companyGroup.units.length === 1 ? "" : "s"} ·{" "}
                                            {companyGroup.orders.length} OE
                                          </span>
                                          <span className="mt-1 block text-xs font-medium text-gray-800">
                                            <BillingTotalsByCurrency totals={companyGroup.totals} />
                                          </span>
                                        </span>
                                      </button>
                                      <div className="flex flex-wrap items-center gap-2 px-3 pb-3 pl-9">
                                        {companyGroup.totals.hasMissingPrices && (
                                          <span className="inline-flex items-center gap-1 text-xs text-amber-800">
                                            <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
                                            {companyGroup.totals.missingPriceItems} ítem
                                            {companyGroup.totals.missingPriceItems === 1 ? "" : "s"} sin precio
                                          </span>
                                        )}
                                        {!companyGroup.batchEligible && (
                                          <span className="inline-flex items-center gap-1 text-xs text-amber-800">
                                            <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
                                            {!companyGroup.customerCode && !companyGroup.occasionalDocument
                                              ? "Sin código Bejerman: sólo facturación individual"
                                              : "Empresa sin determinar: sólo facturación individual"}
                                          </span>
                                        )}
                                        {companyGroup.batchEligible && (
                                          <button
                                            type="button"
                                            onClick={() => openBatchInvoiceModal(companyGroup)}
                                            disabled={!pendingListComplete || pendingLoading || savingBatchInvoice}
                                            className="ml-auto inline-flex items-center gap-1.5 rounded bg-emerald-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                                          >
                                            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                                            Facturar selección
                                          </button>
                                        )}
                                      </div>
                                    </div>

                                    {companyExpanded && (
                                      <div className="divide-y border-t">
                                        {companyGroup.orders.map((order) => (
                                          <PendingBillingOrderRow
                                            key={order.id}
                                            order={order}
                                            user={user}
                                            selectedOrderId={selectedPendingOrderId}
                                            savingInvoice={savingInvoice}
                                            savingPostponement={savingPostponement}
                                            postponementOrder={postponementOrder}
                                            savingNotBillableId={savingNotBillableId}
                                            onSelect={selectPendingOrder}
                                            onCorrectCommercial={openCommercialCorrection}
                                            onInvoice={openInvoiceModal}
                                            onPostpone={openPostponementModal}
                                            onNotBillable={markOrderNotBillable}
                                          />
                                        ))}
                                      </div>
                                    )}
                                  </section>
                                );
                              })}
                            </div>
                          )}
                        </article>
                      );
                    })}
                  </div>
                )}
                {!pendingLoading && !pendingGroups.length && (
                  <div className="px-3 py-8 text-center text-sm text-gray-500">Sin remitos pendientes.</div>
                )}
              </div>
            </section>
            <section className="border">
            <button
              type="button"
              onClick={() => setPostponedOpen((open) => !open)}
              className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-semibold hover:bg-gray-50"
              aria-expanded={postponedOpen}
            >
              <span>Pospuestos</span>
              <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-normal text-gray-700">
                {postponedOrders.length}
              </span>
            </button>
            {postponedOpen && (
              <div className="max-h-[360px] overflow-y-auto border-t">
                {postponedLoading ? (
                  <div className="flex items-center justify-center gap-2 px-3 py-6 text-sm text-gray-500">
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                    Cargando...
                  </div>
                ) : (
                  postponedOrders.map((order) => (
                    <div key={order.id} className="border-b px-3 py-2 text-sm">
                      <div className="font-medium text-gray-950">{order.remitoNumber || order.orderNumber}</div>
                      <div className="text-gray-600">{order.customerName}</div>
                      <div className="mt-1 text-xs text-gray-500">
                        Vuelve a Pendientes: {formatDateTime(order.billingPostponedUntil)}
                      </div>
                      <button
                        type="button"
                        onClick={() => reactivatePostponedOrder(order)}
                        disabled={savingReactivationId === order.id}
                        className="mt-2 inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                      >
                        {savingReactivationId === order.id ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                        ) : (
                          <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
                        )}
                        Reactivar
                      </button>
                    </div>
                  ))
                )}
                {!postponedLoading && !postponedOrders.length && (
                  <div className="px-3 py-6 text-center text-sm text-gray-500">Sin remitos pospuestos.</div>
                )}
              </div>
            )}
            </section>
          </aside>
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
              onKeyDown={(event) => {
                if (event.key === "Enter") runRemitoSearch();
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
              onChange={(event) => {
                setRemitoPage(1);
                setRemitoFilters((prev) => ({ ...prev, search: event.target.value }));
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") runRemitoSearch();
              }}
              className="h-9 w-full rounded border px-2"
              placeholder="Número, cliente, código u operación"
            />
          </label>
        </div>

        {remitoWarning && <div className="border-t px-3 py-2 text-sm text-amber-800">{remitoWarning}</div>}
        {remitoError && <div className="border-t px-3 py-2 text-sm text-red-700">{remitoError}</div>}

        <MobileSortControl
          sort={remitoSort}
          onSortChange={updateRemitoSort}
          options={REMITO_SORT_OPTIONS}
          className="px-3 pt-3"
        />
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
                <SortableTh field="fecha" sort={remitoSort} onSortChange={updateRemitoSort} className="px-2 py-2">Fecha</SortableTh>
                <SortableTh field="cliente" sort={remitoSort} onSortChange={updateRemitoSort} className="px-2 py-2">Cliente</SortableTh>
                <SortableTh field="codigo" sort={remitoSort} onSortChange={updateRemitoSort} className="px-2 py-2">Código</SortableTh>
                <SortableTh field="tipo" sort={remitoSort} onSortChange={updateRemitoSort} className="px-2 py-2">Tipo</SortableTh>
                <SortableTh field="operacion" sort={remitoSort} onSortChange={updateRemitoSort} className="px-2 py-2">Operación</SortableTh>
                <SortableTh field="numero" sort={remitoSort} onSortChange={updateRemitoSort} className="px-2 py-2">Número</SortableTh>
                <SortableTh field="total" sort={remitoSort} onSortChange={updateRemitoSort} className="px-2 py-2 text-right">Total</SortableTh>
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
              {canCorrectCommercialTerms(user, pendingDetailOrder) && (
                <button
                  type="button"
                  onClick={() => openCommercialCorrection(pendingDetailOrder)}
                  className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50"
                >
                  <Pencil className="h-4 w-4" aria-hidden="true" />
                  Corregir precio/plazo
                </button>
              )}
              <button
                type="button"
                onClick={() => openPostponementModal(pendingDetailOrder)}
                disabled={savingPostponement}
                className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50"
              >
                <Clock3 className="h-4 w-4" aria-hidden="true" />
                Posponer
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

      {postponementOrder && (
        <ResponsiveModalOverlay className="bg-black/35">
          <ResponsiveModalPanel className="max-w-lg" role="dialog" aria-modal="true">
            <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
              <div>
                <h2 className="text-base font-semibold">Posponer remito</h2>
                <p className="mt-0.5 text-sm text-gray-600">
                  {postponementOrder.remitoNumber || postponementOrder.orderNumber} · {postponementOrder.customerName || "-"}
                </p>
              </div>
              <button
                type="button"
                onClick={closePostponementModal}
                disabled={savingPostponement}
                className="inline-flex h-8 w-8 items-center justify-center rounded border text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                aria-label="Cerrar"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <form onSubmit={submitPostponement} className="space-y-4 p-4">
              <p className="text-sm text-gray-700">
                El remito dejará de figurar en Pendientes y volverá a las 09:00 de la fecha elegida.
              </p>
              <label className="block text-sm">
                <span className="mb-1 block text-xs uppercase text-gray-500">Fecha de reanudación</span>
                <input
                  type="date"
                  value={postponementDate}
                  min={tomorrowDateInputValue()}
                  onChange={(event) => setPostponementDate(event.target.value)}
                  className="h-9 w-full rounded border px-2"
                  autoFocus
                  required
                />
              </label>
              {postponementError && <div className="text-sm text-red-700">{postponementError}</div>}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={closePostponementModal}
                  disabled={savingPostponement}
                  className="rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50"
                >
                  Cancelar
                </button>
                <button
                  type="submit"
                  disabled={savingPostponement}
                  className="inline-flex items-center gap-2 rounded bg-gray-900 px-3 py-2 text-sm text-white hover:bg-black disabled:opacity-50"
                >
                  {savingPostponement && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
                  Posponer
                </button>
              </div>
            </form>
          </ResponsiveModalPanel>
        </ResponsiveModalOverlay>
      )}

      <DeliveryOrderCommercialCorrectionModal
        open={Boolean(commercialCorrectionOrder)}
        order={commercialCorrectionOrder}
        onClose={closeCommercialCorrection}
        onSaved={handleCommercialCorrectionSaved}
      />

      {batchInvoiceGroup && (
        <ResponsiveModalOverlay className="bg-black/35">
          <ResponsiveModalPanel className="max-w-6xl" role="dialog" aria-modal="true">
            <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
              <div className="min-w-0">
                <h2 className="text-base font-semibold">Registrar factura agrupada</h2>
                <p className="mt-0.5 text-sm text-gray-600">
                  {batchInvoiceGroup.customerName} · {batchInvoiceGroup.customerCode} · {batchInvoiceGroup.companyLabel}
                </p>
                <p className="mt-0.5 text-xs text-gray-500">
                  {batchInvoiceGroup.pointOfSales.length
                    ? `Punto${batchInvoiceGroup.pointOfSales.length === 1 ? "" : "s"} de venta: ${batchInvoiceGroup.pointOfSales.join(", ")}`
                    : "Punto de venta sin informar"}
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  Una sola factura se asignará a todos los remitos seleccionados.
                </p>
              </div>
              <button
                type="button"
                onClick={closeBatchInvoiceModal}
                disabled={savingBatchInvoice}
                className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded border text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                aria-label="Cerrar"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <form onSubmit={submitBatchInvoice} className="max-h-[calc(100vh-8rem)] overflow-y-auto">
              <div className="sticky top-0 z-10 space-y-3 border-b bg-white p-4 shadow-sm">
                <div className="grid gap-3 md:grid-cols-[minmax(240px,1fr)_auto] md:items-end">
                  <label className="block text-sm">
                    <span className="mb-1 block text-xs uppercase text-gray-500">Número de factura</span>
                    <input
                      autoFocus
                      value={batchInvoiceNumber}
                      onChange={(event) => {
                        setBatchInvoiceNumber(event.target.value);
                        setBatchInvoiceError("");
                      }}
                      className="h-9 w-full rounded border px-2"
                      placeholder="FC A 0001-00000000"
                    />
                  </label>
                  <label className="inline-flex h-9 cursor-pointer items-center gap-2 rounded border px-3 text-sm hover:bg-gray-50">
                    <input
                      type="checkbox"
                      checked={
                        batchInvoiceGroup.units.length > 0 &&
                        batchInvoiceUnitKeys.length === batchInvoiceGroup.units.length
                      }
                      onChange={toggleAllBatchInvoiceUnits}
                      className="h-4 w-4 rounded border-gray-300"
                    />
                    Seleccionar todos
                  </label>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                  <span className="text-gray-600">
                    {batchInvoiceUnitKeys.length} de {batchInvoiceGroup.units.length} remitos · {selectedBatchInvoiceOrders.length} OE
                  </span>
                  <span className="font-medium text-gray-900">
                    <BillingTotalsByCurrency totals={selectedBatchInvoiceTotals} label="Seleccionado" />
                  </span>
                </div>
                <MissingPriceNotice totals={selectedBatchInvoiceTotals} />
                {batchInvoiceError && (
                  <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                    {batchInvoiceError}
                  </div>
                )}
              </div>

              <div className="space-y-3 bg-gray-50 p-3 sm:p-4">
                {batchInvoiceGroup.units.map((unit) => {
                  const selected = selectedBatchInvoiceUnitKeys.has(unit.key);
                  return (
                    <section
                      key={unit.key}
                      className={`overflow-hidden rounded border bg-white ${
                        selected ? "border-emerald-300 ring-1 ring-emerald-100" : "opacity-70"
                      }`}
                    >
                      <label className="flex cursor-pointer items-start gap-3 border-b bg-gray-50 px-3 py-3 sm:px-4">
                        <input
                          type="checkbox"
                          checked={selected}
                          onChange={() => toggleBatchInvoiceUnit(unit.key)}
                          className="mt-0.5 h-4 w-4 shrink-0 rounded border-gray-300"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block font-semibold text-gray-950">
                            {unit.remitoNumber || unit.orders[0]?.orderNumber || "Remito sin número"}
                          </span>
                          <span className="mt-0.5 block text-xs text-gray-600">
                            {unit.orders.length} OE · {formatOrderTotalsAmount(unit.totals, "total")}
                          </span>
                          {unit.orders.length > 1 && (
                            <span className="mt-1 block text-xs text-blue-700">
                              Estas OE pertenecen al mismo remito y se seleccionan juntas.
                            </span>
                          )}
                        </span>
                      </label>
                      <div className="divide-y">
                        {unit.orders.map((order) => (
                          <div key={order.id} className="space-y-3 p-3 sm:p-4">
                            {unit.orders.length > 1 && (
                              <div className="text-sm font-semibold text-gray-900">{order.orderNumber || `OE ${order.id}`}</div>
                            )}
                            <DeliveryOrderBillingDetails order={order} />
                          </div>
                        ))}
                      </div>
                    </section>
                  );
                })}
              </div>

              <div className="sticky bottom-0 flex flex-col-reverse gap-2 border-t bg-white px-4 py-3 shadow-[0_-2px_8px_rgba(0,0,0,0.06)] sm:flex-row sm:justify-end">
                <button
                  type="button"
                  onClick={closeBatchInvoiceModal}
                  disabled={savingBatchInvoice}
                  className="rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50"
                >
                  Cancelar
                </button>
                <button
                  type="submit"
                  disabled={savingBatchInvoice}
                  className="inline-flex items-center justify-center gap-2 rounded bg-emerald-600 px-3 py-2 text-sm text-white hover:bg-emerald-700 disabled:opacity-50"
                >
                  {savingBatchInvoice ? (
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  )}
                  Registrar factura en {batchInvoiceUnitKeys.length} remito{batchInvoiceUnitKeys.length === 1 ? "" : "s"}
                </button>
              </div>
            </form>
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
