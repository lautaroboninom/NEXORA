import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, FileText, Loader2, Search, X } from "lucide-react";
import {
  getBillingCustomers,
  getBillingDocumentPdfBlob,
  getBillingDocuments,
  getDeliveryOrders,
  postDeliveryOrderInvoiced,
} from "../lib/api";
import { deliveryOrderItemsSummary } from "../lib/delivery-orders";

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
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) return raw;
  return parsed.toLocaleString("es-AR", {
    style: "currency",
    currency: "ARS",
    maximumFractionDigits: 2,
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

function DetailItem({ label, value }) {
  return (
    <div>
      <div className="text-[11px] font-medium uppercase text-gray-500">{label}</div>
      <div className="mt-0.5 text-sm text-gray-900">{value || "-"}</div>
    </div>
  );
}

export default function Billing() {
  const [customers, setCustomers] = useState([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [customerQuery, setCustomerQuery] = useState("");
  const [filters, setFilters] = useState({ dateFrom: "", dateTo: "", search: "" });
  const [documents, setDocuments] = useState([]);
  const [pagination, setPagination] = useState(EMPTY_PAGINATION);
  const [page, setPage] = useState(1);
  const [pendingOrders, setPendingOrders] = useState([]);
  const [selectedPendingOrderId, setSelectedPendingOrderId] = useState("");
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [pendingLoading, setPendingLoading] = useState(false);
  const [savingInvoice, setSavingInvoice] = useState(false);
  const [invoiceOrder, setInvoiceOrder] = useState(null);
  const [invoiceNumber, setInvoiceNumber] = useState("");
  const [invoiceError, setInvoiceError] = useState("");
  const [error, setError] = useState("");

  const selectedPendingOrder = useMemo(
    () => pendingOrders.find((order) => order.id === selectedPendingOrderId) || null,
    [pendingOrders, selectedPendingOrderId]
  );

  const selectedSummary = useMemo(
    () => (selectedPendingOrder ? deliveryOrderItemsSummary(selectedPendingOrder) : null),
    [selectedPendingOrder]
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

  const loadPendingOrders = async () => {
    setPendingLoading(true);
    try {
      const data = await getDeliveryOrders({ status: PENDING_BILLING_STATUS, limit: 200 });
      const items = Array.isArray(data?.items) ? data.items : [];
      setPendingOrders(items);
      setSelectedPendingOrderId((current) =>
        current && items.some((order) => order.id === current) ? current : ""
      );
    } catch {
      setPendingOrders([]);
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
    loadPendingOrders();
  }, []);

  useEffect(() => {
    loadDocuments(selectedCode, 1);
  }, [selectedCode]);

  const selectPendingOrder = (order) => {
    setSelectedPendingOrderId(order.id);
    const orderCode = clean(order.bejermanCustomerCode);
    setPage(1);
    if (orderCode) {
      if (orderCode === selectedCode) loadDocuments(orderCode, 1);
      setSelectedCode(orderCode);
      setCustomerQuery("");
      return;
    }
    if (!selectedCode) loadDocuments("", 1);
    setSelectedCode("");
    setCustomerQuery(order.customerName || "");
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
      await loadPendingOrders();
      setPage(1);
      await loadDocuments(codeToRefresh, 1);
    } catch (err) {
      setInvoiceError(err?.message || "No se pudo registrar la factura.");
    } finally {
      setSavingInvoice(false);
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

      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_340px]">
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
                <DetailItem label="Orden" value={selectedPendingOrder.orderNumber} />
                <DetailItem label="Código Bejerman" value={selectedPendingOrder.bejermanCustomerCode} />
                <DetailItem label="Artículo" value={selectedSummary?.primary} />
                <DetailItem label="Referencia" value={selectedSummary?.secondary} />
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

          <div className="flex flex-wrap items-end gap-2 p-3">
            <label className="min-w-[260px] flex-1 text-sm">
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
                  setPage(1);
                  setSelectedCode(event.target.value);
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
                className="h-9 rounded border px-2"
              />
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-xs uppercase text-gray-500">Hasta</span>
              <input
                type="date"
                value={filters.dateTo}
                onChange={(event) => setFilters((prev) => ({ ...prev, dateTo: event.target.value }))}
                className="h-9 rounded border px-2"
              />
            </label>
            <label className="min-w-[180px] flex-1 text-sm">
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
              className="inline-flex h-9 items-center gap-2 rounded border px-3 text-sm hover:bg-gray-50 disabled:opacity-50"
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

          <div className="overflow-x-auto">
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
          </div>
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
              pendingOrders.map((order) => (
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
                  </button>
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
              ))
            )}
            {!pendingLoading && !pendingOrders.length && (
              <div className="px-3 py-8 text-center text-sm text-gray-500">Sin remitos pendientes.</div>
            )}
          </div>
        </section>
      </div>

      {invoiceOrder && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4">
          <div className="w-full max-w-md rounded bg-white shadow-xl" role="dialog" aria-modal="true">
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
            <form onSubmit={submitInvoice} className="space-y-3 p-4">
              <div className="grid grid-cols-2 gap-3 text-sm">
                <DetailItem label="Orden" value={invoiceOrder.orderNumber} />
                <DetailItem label="Código Bejerman" value={invoiceOrder.bejermanCustomerCode || "sin código"} />
              </div>
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
          </div>
        </div>
      )}
    </div>
  );
}
