import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, FileText, Loader2, Search, X } from "lucide-react";
import {
  getBillingCustomers,
  getBillingDocumentPdfBlob,
  getBillingDocuments,
  getDeliveryOrder,
  getDeliveryOrders,
  getServiceOrderBillingPdfBlob,
  getServiceOrdersToBill,
  postDeliveryOrderInvoiced,
  postServiceOrderInvoice,
} from "../lib/api";
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

const PENDING_BILLING_STATUS = "entregado_pendiente_facturacion";
const BILLING_PAGE_SIZE = 25;
const EMPTY_PAGINATION = {
  page: 1,
  pageSize: BILLING_PAGE_SIZE,
  total: 0,
  totalPages: 1,
  hasNextPage: false,
  hasPreviousPage: false,
};
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

function orderSubtitle(order) {
  return [
    clean(order?.orderNumber),
    clean(order?.bejermanCustomerCode) || "sin código Bejerman",
  ]
    .filter(Boolean)
    .join(" · ");
}

function selectedCodeOption(selectedCode, selectedOrder, customers) {
  if (!selectedCode) return null;
  if (customers.some((customer) => customer.bejermanCustomerCode === selectedCode)) return null;
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

function ServiceOrderBillingDetails({ item }) {
  if (!item) return null;
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <DetailItem label="OS" value={item.os} />
        <DetailItem label="Cliente" value={item.cliente} />
        <DetailItem label="Código Bejerman" value={item.bejermanCustomerCode || "sin código"} />
        <DetailItem label="RSS" value={item.rss || "RSS pendiente"} />
        <DetailItem label="Equipo" value={item.equipo} />
        <DetailItem label="Serie" value={item.numeroSerie} />
        <DetailItem label="Interno" value={item.numeroInterno} />
        <DetailItem label="Fecha de liberación" value={formatDateTime(item.fechaLiberacion)} />
        <DetailItem label="Concepto sugerido" value={`${item.conceptCode || "-"} - ${item.conceptDescription || "-"}`} />
        <DetailItem label="Resolución" value={item.resolucion || "-"} />
        <DetailItem label="Técnico" value={item.tecnico || "-"} />
        <DetailItem label="Factura" value={item.facturaNumero || "-"} />
      </div>
      {(clean(item.descripcionProblema) || clean(item.trabajosRealizados)) && (
        <div className="grid gap-3 lg:grid-cols-2">
          {clean(item.descripcionProblema) && (
            <div>
              <div className="text-[11px] font-medium uppercase text-gray-500">Descripción</div>
              <div className="mt-1 whitespace-pre-wrap rounded border bg-white px-3 py-2 text-sm text-gray-800">
                {item.descripcionProblema}
              </div>
            </div>
          )}
          {clean(item.trabajosRealizados) && (
            <div>
              <div className="text-[11px] font-medium uppercase text-gray-500">Trabajos realizados</div>
              <div className="mt-1 whitespace-pre-wrap rounded border bg-white px-3 py-2 text-sm text-gray-800">
                {item.trabajosRealizados}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function Billing() {
  const requestedOrderId = useMemo(initialOrderIdFromUrl, []);
  const requestedServiceOrderId = useMemo(initialServiceOrderIdFromUrl, []);
  const [customers, setCustomers] = useState([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [customerQuery, setCustomerQuery] = useState("");
  const [filters, setFilters] = useState({ dateFrom: "", dateTo: "", search: "" });
  const [documents, setDocuments] = useState([]);
  const [pagination, setPagination] = useState(EMPTY_PAGINATION);
  const [page, setPage] = useState(1);
  const [pendingOrders, setPendingOrders] = useState([]);
  const [selectedPendingOrderId, setSelectedPendingOrderId] = useState("");
  const [serviceOrders, setServiceOrders] = useState([]);
  const [selectedServiceOrderId, setSelectedServiceOrderId] = useState("");
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [pendingLoading, setPendingLoading] = useState(false);
  const [serviceOrdersLoading, setServiceOrdersLoading] = useState(false);
  const [savingInvoice, setSavingInvoice] = useState(false);
  const [savingServiceInvoice, setSavingServiceInvoice] = useState(false);
  const [invoiceOrder, setInvoiceOrder] = useState(null);
  const [serviceInvoiceOrder, setServiceInvoiceOrder] = useState(null);
  const [invoiceNumber, setInvoiceNumber] = useState("");
  const [serviceInvoiceNumber, setServiceInvoiceNumber] = useState("");
  const [invoiceError, setInvoiceError] = useState("");
  const [serviceInvoiceError, setServiceInvoiceError] = useState("");
  const [error, setError] = useState("");
  const [serviceError, setServiceError] = useState("");

  const selectedPendingOrder = useMemo(
    () => pendingOrders.find((order) => order.id === selectedPendingOrderId) || null,
    [pendingOrders, selectedPendingOrderId]
  );

  const selectedSummary = useMemo(
    () => (selectedPendingOrder ? deliveryOrderItemsSummary(selectedPendingOrder) : null),
    [selectedPendingOrder]
  );
  const selectedTotals = useMemo(
    () => (selectedPendingOrder ? deliveryOrderItemsTotals(selectedPendingOrder) : null),
    [selectedPendingOrder]
  );

  const selectedServiceOrder = useMemo(
    () => serviceOrders.find((item) => String(item.ingresoId || item.id) === String(selectedServiceOrderId)) || null,
    [serviceOrders, selectedServiceOrderId]
  );

  const filteredCustomers = useMemo(() => {
    const query = customerQuery.trim().toLowerCase();
    if (!query) return customers;
    return customers.filter((customer) =>
      [customer.name, customer.bejermanCustomerCode]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(query)
    );
  }, [customers, customerQuery]);

  const customersForSelect = useMemo(() => {
    const extra = selectedCodeOption(selectedCode, selectedPendingOrder, customers);
    return extra ? [extra, ...filteredCustomers] : filteredCustomers;
  }, [customers, filteredCustomers, selectedCode, selectedPendingOrder]);

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
      setError("");
    } catch (err) {
      setError(err?.message || "No se pudo consultar facturación.");
      setDocuments([]);
      setPagination(EMPTY_PAGINATION);
    } finally {
      setDocumentsLoading(false);
    }
  };

  const syncCustomerSelectionForOrder = (order) => {
    const orderCode = clean(order?.bejermanCustomerCode);
    setPage(1);
    if (orderCode) {
      setSelectedCode(orderCode);
      setCustomerQuery("");
      loadDocuments(orderCode, 1);
      return;
    }
    setDocuments([]);
    setPagination(EMPTY_PAGINATION);
    setSelectedCode("");
    setCustomerQuery(order?.customerName || "");
  };

  const loadPendingOrders = async ({ preferredSelectedId = selectedPendingOrderId, allowRequestedSelection = true } = {}) => {
    setPendingLoading(true);
    try {
      const data = await getDeliveryOrders({ status: PENDING_BILLING_STATUS, limit: 200 });
      let items = Array.isArray(data?.items) ? data.items : [];
      if (allowRequestedSelection && requestedOrderId && !items.some((order) => order.id === requestedOrderId)) {
        try {
          const requestedOrder = await getDeliveryOrder(requestedOrderId);
          if (requestedOrder?.status === PENDING_BILLING_STATUS) {
            items = [requestedOrder, ...items];
          }
        } catch {
          // Si el vínculo ya no corresponde a un pendiente, la lista general sigue disponible.
        }
      }

      setPendingOrders(items);
      const preferredExists = preferredSelectedId && items.some((order) => order.id === preferredSelectedId);
      const requestedExists = allowRequestedSelection && requestedOrderId && items.some((order) => order.id === requestedOrderId);
      const nextSelectedId = preferredExists ? preferredSelectedId : requestedExists ? requestedOrderId : "";
      setSelectedPendingOrderId(nextSelectedId);
      const nextOrder = items.find((order) => order.id === nextSelectedId);
      if (nextOrder && nextSelectedId !== selectedPendingOrderId) {
        syncCustomerSelectionForOrder(nextOrder);
      }
    } catch {
      setPendingOrders([]);
    } finally {
      setPendingLoading(false);
    }
  };

  const loadServiceOrders = async ({ preferredSelectedId = selectedServiceOrderId, allowRequestedSelection = true } = {}) => {
    setServiceOrdersLoading(true);
    try {
      const data = await getServiceOrdersToBill({ limit: 200 });
      const items = Array.isArray(data?.items) ? data.items : [];
      setServiceOrders(items);
      const preferredExists = preferredSelectedId && items.some((item) => String(item.ingresoId || item.id) === String(preferredSelectedId));
      const requestedExists = allowRequestedSelection && requestedServiceOrderId && items.some((item) => String(item.ingresoId || item.id) === String(requestedServiceOrderId));
      setSelectedServiceOrderId(preferredExists ? preferredSelectedId : requestedExists ? requestedServiceOrderId : "");
      setServiceError("");
    } catch (err) {
      setServiceOrders([]);
      setServiceError(err?.message || "No se pudieron cargar las OS a facturar.");
    } finally {
      setServiceOrdersLoading(false);
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
    loadPendingOrders({ preferredSelectedId: "", allowRequestedSelection: true });
    loadServiceOrders({ preferredSelectedId: "", allowRequestedSelection: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectPendingOrder = (order) => {
    setSelectedPendingOrderId(order.id);
    syncCustomerSelectionForOrder(order);
  };

  const openInvoiceModal = (order) => {
    selectPendingOrder(order);
    setInvoiceOrder(order);
    setInvoiceNumber("");
    setInvoiceError("");
    setError("");
  };

  const closeInvoiceModal = () => {
    if (savingInvoice) return;
    setInvoiceOrder(null);
    setInvoiceNumber("");
    setInvoiceError("");
  };

  const openServiceInvoiceModal = (item) => {
    setSelectedServiceOrderId(String(item.ingresoId || item.id || ""));
    setServiceInvoiceOrder(item);
    setServiceInvoiceNumber("");
    setServiceInvoiceError("");
    setServiceError("");
  };

  const closeServiceInvoiceModal = () => {
    if (savingServiceInvoice) return;
    setServiceInvoiceOrder(null);
    setServiceInvoiceNumber("");
    setServiceInvoiceError("");
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
      const codeToRefresh = clean(invoiceOrder.bejermanCustomerCode) || selectedCode;
      setInvoiceOrder(null);
      setInvoiceNumber("");
      setSelectedPendingOrderId("");
      await loadPendingOrders({ preferredSelectedId: "", allowRequestedSelection: false });
      setPage(1);
      await loadDocuments(codeToRefresh, 1);
    } catch (err) {
      setInvoiceError(err?.message || "No se pudo registrar la factura.");
    } finally {
      setSavingInvoice(false);
    }
  };

  const submitServiceInvoice = async (event) => {
    event.preventDefault();
    const invoice = serviceInvoiceNumber.trim();
    if (!invoice) {
      setServiceInvoiceError("Ingrese el número de factura.");
      return;
    }
    const ingresoId = serviceInvoiceOrder?.ingresoId || serviceInvoiceOrder?.id;
    if (!ingresoId) return;

    setSavingServiceInvoice(true);
    setServiceInvoiceError("");
    setServiceError("");
    try {
      await postServiceOrderInvoice(ingresoId, { facturaNumero: invoice });
      setServiceInvoiceOrder(null);
      setServiceInvoiceNumber("");
      setSelectedServiceOrderId("");
      await loadServiceOrders({ preferredSelectedId: "", allowRequestedSelection: false });
    } catch (err) {
      setServiceInvoiceError(err?.message || "No se pudo registrar la factura de la OS.");
    } finally {
      setSavingServiceInvoice(false);
    }
  };

  const openPdf = async (item) => {
    const documentId = valueOf(item, ["documentId", "id"]);
    const customerCode = valueOf(item, ["bejermanCustomerCode", "customerCode"]) || selectedCode;
    if (!documentId) return;
    try {
      const blob = await getBillingDocumentPdfBlob(documentId, customerCode);
      openBlob(blob, `facturacion-${documentId}.pdf`);
    } catch (err) {
      setError(err?.message || "No se pudo abrir el PDF.");
    }
  };

  const openServicePdf = async (item) => {
    const ingresoId = item?.ingresoId || item?.id;
    if (!ingresoId) return;
    try {
      const blob = await getServiceOrderBillingPdfBlob(ingresoId);
      openBlob(blob, `OS-${item?.os || ingresoId}-facturacion.pdf`);
    } catch (err) {
      setServiceError(err?.message || "No se pudo abrir el PDF de la OS.");
    }
  };

  const selectedHasCustomerCode = Boolean(clean(selectedPendingOrder?.bejermanCustomerCode));
  const currentPage = pagination?.page || page || 1;
  const totalPages = pagination?.totalPages || 1;
  const totalDocuments = pagination?.total || documents.length;

  const runSearch = () => {
    setPage(1);
    loadDocuments(selectedCode, 1);
  };

  const goToPage = (nextPage) => {
    const safePage = Math.max(1, Math.min(totalPages, nextPage));
    if (safePage === currentPage || documentsLoading) return;
    setPage(safePage);
    loadDocuments(selectedCode, safePage);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Cobranzas</h1>
          <p className="text-sm text-gray-600">Facturación Bejerman y remitos pendientes.</p>
        </div>
      </div>

      <section className="border">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-3 py-2">
          <div>
            <div className="text-sm font-semibold">OS a facturar</div>
            <div className="text-xs text-gray-500">Reparaciones liberadas para facturar como concepto.</div>
          </div>
          <span className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-700">{serviceOrders.length}</span>
        </div>

        {serviceError && <div className="border-b px-3 py-2 text-sm text-red-700">{serviceError}</div>}

        {selectedServiceOrder && (
          <div className="border-b bg-gray-50 px-3 py-3">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-xs uppercase text-gray-500">OS seleccionada</div>
                <div className="mt-0.5 text-base font-semibold text-gray-950">OS {selectedServiceOrder.os}</div>
                <div className="mt-1 text-sm text-gray-600">{selectedServiceOrder.cliente || "-"}</div>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => openServicePdf(selectedServiceOrder)}
                  className="inline-flex h-9 items-center gap-2 rounded border px-3 text-sm hover:bg-white"
                >
                  <FileText className="h-4 w-4" aria-hidden="true" />
                  PDF OS
                </button>
                <button
                  type="button"
                  onClick={() => openServiceInvoiceModal(selectedServiceOrder)}
                  className="inline-flex h-9 items-center gap-2 rounded bg-emerald-600 px-3 text-sm text-white hover:bg-emerald-700"
                >
                  <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  Registrar factura OS
                </button>
              </div>
            </div>
            <ServiceOrderBillingDetails item={selectedServiceOrder} />
          </div>
        )}

        <MobileDataList className="p-3">
          {serviceOrdersLoading && <MobileDataCard className="text-center text-gray-500">Cargando...</MobileDataCard>}
          {!serviceOrdersLoading &&
            serviceOrders.map((item) => {
              const itemId = String(item.ingresoId || item.id || "");
              return (
                <MobileDataCard key={itemId} className={selectedServiceOrderId === itemId ? "border-emerald-300 bg-emerald-50" : ""}>
                  <button type="button" onClick={() => setSelectedServiceOrderId(itemId)} className="block w-full text-left">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-semibold text-gray-900">OS {item.os}</div>
                        <div className="text-sm text-gray-600">{item.cliente || "-"}</div>
                      </div>
                      <div className="text-xs text-gray-500">{formatDateTime(item.fechaLiberacion)}</div>
                    </div>
                    <div className="mt-2 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                      <MobileDataField label="Equipo" value={item.equipo || "-"} />
                      <MobileDataField label="Concepto" value={`${item.conceptCode || "-"} - ${item.conceptDescription || "-"}`} />
                      <MobileDataField label="Serie" value={item.numeroSerie || "-"} />
                      <MobileDataField label="RSS" value={item.rss || "RSS pendiente"} />
                    </div>
                  </button>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => openServicePdf(item)}
                      className="inline-flex h-9 items-center gap-1 rounded border px-2 text-xs hover:bg-gray-50"
                    >
                      <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                      PDF
                    </button>
                    <button
                      type="button"
                      onClick={() => openServiceInvoiceModal(item)}
                      disabled={savingServiceInvoice}
                      className="inline-flex h-9 items-center gap-1 rounded border px-2 text-xs hover:bg-gray-50 disabled:opacity-50"
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                      Registrar factura
                    </button>
                  </div>
                </MobileDataCard>
              );
            })}
          {!serviceOrdersLoading && !serviceOrders.length && <MobileDataCard className="text-center text-gray-500">Sin OS a facturar.</MobileDataCard>}
        </MobileDataList>

        <DesktopTableWrap>
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
              <tr>
                <th className="px-2 py-2">OS</th>
                <th className="px-2 py-2">Cliente</th>
                <th className="px-2 py-2">Equipo</th>
                <th className="px-2 py-2">Concepto sugerido</th>
                <th className="px-2 py-2">RSS</th>
                <th className="px-2 py-2">Liberación</th>
                <th className="px-2 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {serviceOrdersLoading ? (
                <tr>
                  <td colSpan={7} className="px-3 py-8 text-center text-gray-500">
                    Cargando...
                  </td>
                </tr>
              ) : (
                serviceOrders.map((item) => {
                  const itemId = String(item.ingresoId || item.id || "");
                  return (
                    <tr key={itemId} className={`border-t align-top ${selectedServiceOrderId === itemId ? "bg-emerald-50" : ""}`}>
                      <td className="px-2 py-2 font-medium">
                        <button type="button" onClick={() => setSelectedServiceOrderId(itemId)} className="text-left text-blue-700 hover:underline">
                          OS {item.os}
                        </button>
                      </td>
                      <td className="px-2 py-2">{item.cliente || "-"}</td>
                      <td className="px-2 py-2">{item.equipo || "-"}</td>
                      <td className="px-2 py-2">
                        <div className="font-medium">{item.conceptCode || "-"}</div>
                        <div className="text-xs text-gray-600">{item.conceptDescription || "-"}</div>
                      </td>
                      <td className="px-2 py-2">{item.rss || "RSS pendiente"}</td>
                      <td className="px-2 py-2">{formatDateTime(item.fechaLiberacion)}</td>
                      <td className="px-2 py-2 text-right">
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => openServicePdf(item)}
                            className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-gray-50"
                          >
                            <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                            PDF
                          </button>
                          <button
                            type="button"
                            onClick={() => openServiceInvoiceModal(item)}
                            disabled={savingServiceInvoice}
                            className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                          >
                            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                            Registrar
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
              {!serviceOrdersLoading && !serviceOrders.length && (
                <tr>
                  <td colSpan={7} className="px-3 py-8 text-center text-gray-500">
                    Sin OS a facturar.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </DesktopTableWrap>
      </section>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_340px]">
        <section className="border">
          <div className="border-b px-3 py-2 text-sm font-semibold">Consulta de facturación</div>

          {selectedPendingOrder ? (
            <div className="border-b bg-gray-50 px-3 py-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="text-xs uppercase text-gray-500">Remito seleccionado</div>
                  <div className="mt-0.5 text-base font-semibold text-gray-950">
                    {selectedPendingOrder.remitoNumber || selectedPendingOrder.orderNumber}
                  </div>
                  <div className="mt-1 text-sm text-gray-600">{selectedPendingOrder.customerName || "-"}</div>
                </div>
                <button
                  type="button"
                  onClick={() => openInvoiceModal(selectedPendingOrder)}
                  className="inline-flex h-9 items-center gap-2 rounded bg-emerald-600 px-3 text-sm text-white hover:bg-emerald-700"
                >
                  <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  Registrar factura
                </button>
              </div>

              <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <DetailItem label="Artículo" value={selectedSummary?.primary} />
                <DetailItem label="Referencia" value={selectedSummary?.secondary} />
                <DetailItem label="Total estimado" value={formatOrderTotalsAmount(selectedTotals, "total")} />
                <DetailItem label="Fecha de entrega" value={formatDateTime(selectedPendingOrder.deliveredAt)} />
              </div>

              <div className="mt-3">
                <DeliveryOrderBillingDetails order={selectedPendingOrder} />
              </div>

              {!selectedHasCustomerCode && (
                <div className="mt-3 inline-flex items-start gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                  <span>
                    Cliente sin código Bejerman. La consulta no está disponible para este remito; podés
                    registrar la factura igual.
                  </span>
                </div>
              )}
            </div>
          ) : (
            <div className="border-b px-3 py-3 text-sm text-gray-500">Sin remito seleccionado.</div>
          )}

          <div className="grid gap-2 p-3 sm:grid-cols-2 xl:grid-cols-[minmax(220px,1fr)_150px_150px_minmax(180px,1fr)_auto] xl:items-end">
            <label className="text-sm sm:col-span-2 xl:col-span-1">
              <span className="mb-1 block text-xs uppercase text-gray-500">Cliente</span>
              <input
                value={customerQuery}
                onChange={(event) => setCustomerQuery(event.target.value)}
                className="mb-2 h-9 w-full rounded border px-2"
                placeholder="Buscar cliente o código"
              />
              <select
                value={selectedCode}
                onChange={(event) => {
                  const nextCode = event.target.value;
                  setPage(1);
                  setSelectedCode(nextCode);
                  loadDocuments(nextCode, 1);
                }}
                className="h-9 w-full rounded border px-2"
              >
                <option value="">Todas las facturaciones</option>
                {customersForSelect.map((customer) => (
                  <option key={customer.id} value={customer.bejermanCustomerCode}>
                    {customer.name} · {customer.bejermanCustomerCode}
                  </option>
                ))}
              </select>
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

          <MobileDataList className="p-3">
            {documentsLoading && <MobileDataCard className="text-center text-gray-500">Cargando...</MobileDataCard>}
            {!documentsLoading &&
              documents.map((item, index) => {
                const documentId = valueOf(item, ["documentId", "id"]);
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
                        className="inline-flex h-9 w-full items-center justify-center gap-1 rounded border px-2 text-xs hover:bg-gray-50"
                      >
                        <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                        PDF
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
                              className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-gray-50"
                            >
                              <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                              PDF
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

        <section className="border">
          <div className="flex items-center justify-between border-b px-3 py-2 text-sm font-semibold">
            <span>Remitos pendientes</span>
            <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-normal text-gray-700">
              {pendingOrders.length}
            </span>
          </div>
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
                  </div>
                );
              })
            )}
            {!pendingLoading && !pendingOrders.length && (
              <div className="px-3 py-8 text-center text-sm text-gray-500">Sin remitos pendientes.</div>
            )}
          </div>
        </section>
      </div>

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

      {serviceInvoiceOrder && (
        <ResponsiveModalOverlay className="bg-black/35">
          <ResponsiveModalPanel className="max-w-4xl" role="dialog" aria-modal="true">
            <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
              <div>
                <h2 className="text-base font-semibold">Registrar factura OS</h2>
                <p className="mt-0.5 text-sm text-gray-600">
                  OS {serviceInvoiceOrder.os || serviceInvoiceOrder.ingresoId} · {serviceInvoiceOrder.cliente || "-"}
                </p>
              </div>
              <button
                type="button"
                onClick={closeServiceInvoiceModal}
                disabled={savingServiceInvoice}
                className="inline-flex h-8 w-8 items-center justify-center rounded border text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                aria-label="Cerrar"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <form onSubmit={submitServiceInvoice} className="max-h-[calc(100vh-9rem)] space-y-4 overflow-y-auto p-4">
              <ServiceOrderBillingDetails item={serviceInvoiceOrder} />
              <label className="block text-sm">
                <span className="mb-1 block text-xs uppercase text-gray-500">Número de factura</span>
                <input
                  autoFocus
                  value={serviceInvoiceNumber}
                  onChange={(event) => setServiceInvoiceNumber(event.target.value)}
                  className="h-9 w-full rounded border px-2"
                  placeholder="FC A 0001-00000000"
                />
              </label>
              {serviceInvoiceError && <div className="text-sm text-red-700">{serviceInvoiceError}</div>}
              <div className="flex justify-end gap-2 pt-1">
                <button
                  type="button"
                  onClick={closeServiceInvoiceModal}
                  disabled={savingServiceInvoice}
                  className="rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50"
                >
                  Cancelar
                </button>
                <button
                  type="submit"
                  disabled={savingServiceInvoice}
                  className="inline-flex items-center gap-2 rounded bg-emerald-600 px-3 py-2 text-sm text-white hover:bg-emerald-700 disabled:opacity-50"
                >
                  {savingServiceInvoice ? (
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
