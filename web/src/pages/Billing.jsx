import { useEffect, useMemo, useState } from "react";
import { getBillingCustomers, getBillingDocumentPdfBlob, getBillingDocuments, getDeliveryOrders } from "../lib/api";

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

function valueOf(item, keys) {
  for (const key of keys) {
    const value = item?.[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return "";
}

export default function Billing() {
  const [customers, setCustomers] = useState([]);
  const [selectedCode, setSelectedCode] = useState("");
  const [filters, setFilters] = useState({ desde: "", hasta: "", search: "" });
  const [documents, setDocuments] = useState([]);
  const [pendingOrders, setPendingOrders] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getBillingCustomers()
      .then((data) => {
        const items = Array.isArray(data?.items) ? data.items : [];
        setCustomers(items);
        if (items.length) setSelectedCode(items[0].bejermanCustomerCode);
      })
      .catch((err) => setError(err?.message || "No se pudieron cargar clientes."));
  }, []);

  useEffect(() => {
    getDeliveryOrders({ status: "entregado_pendiente_facturacion", limit: 80 })
      .then((data) => setPendingOrders(Array.isArray(data?.items) ? data.items : []))
      .catch(() => setPendingOrders([]));
  }, []);

  const selectedCustomer = useMemo(
    () => customers.find((customer) => customer.bejermanCustomerCode === selectedCode),
    [customers, selectedCode]
  );

  const loadDocuments = () => {
    if (!selectedCode) return;
    setLoading(true);
    getBillingDocuments({ customerCode: selectedCode, ...filters })
      .then((data) => {
        setDocuments(Array.isArray(data?.items) ? data.items : []);
        setError("");
      })
      .catch((err) => setError(err?.message || "No se pudo consultar facturación."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (selectedCode) loadDocuments();
  }, [selectedCode]);

  const openPdf = async (documentId) => {
    try {
      const blob = await getBillingDocumentPdfBlob(documentId, selectedCode);
      openBlob(blob, `facturacion-${documentId}.pdf`);
    } catch (err) {
      setError(err?.message || "No se pudo abrir el PDF.");
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Cobranzas</h1>
          <p className="text-sm text-gray-600">Facturación Bejerman y remitos pendientes.</p>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_320px]">
        <section className="border">
          <div className="border-b px-3 py-2 text-sm font-semibold">Consulta de facturación</div>
          <div className="flex flex-wrap items-end gap-2 p-3">
            <label className="min-w-[260px] flex-1 text-sm">
              <span className="mb-1 block text-xs uppercase text-gray-500">Cliente</span>
              <select
                value={selectedCode}
                onChange={(event) => setSelectedCode(event.target.value)}
                className="h-9 w-full rounded border px-2"
              >
                {customers.map((customer) => (
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
                value={filters.desde}
                onChange={(event) => setFilters((prev) => ({ ...prev, desde: event.target.value }))}
                className="h-9 rounded border px-2"
              />
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-xs uppercase text-gray-500">Hasta</span>
              <input
                type="date"
                value={filters.hasta}
                onChange={(event) => setFilters((prev) => ({ ...prev, hasta: event.target.value }))}
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
            <button type="button" onClick={loadDocuments} className="h-9 rounded border px-3 text-sm hover:bg-gray-50">
              Consultar
            </button>
          </div>
          {error && <div className="border-t px-3 py-2 text-sm text-red-700">{error}</div>}
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-2">Fecha</th>
                  <th className="px-2 py-2">Comprobante</th>
                  <th className="px-2 py-2">Número</th>
                  <th className="px-2 py-2 text-right">Total</th>
                  <th className="px-2 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={5} className="px-3 py-8 text-center text-gray-500">
                      Cargando...
                    </td>
                  </tr>
                ) : (
                  documents.map((item, index) => {
                    const documentId = valueOf(item, ["documentId", "id"]);
                    return (
                      <tr key={documentId || index} className="border-t">
                        <td className="px-2 py-2">{valueOf(item, ["fecha", "date", "issueDate"]) || "-"}</td>
                        <td className="px-2 py-2">{valueOf(item, ["tipo", "type", "comprobanteTipo"]) || "-"}</td>
                        <td className="px-2 py-2">{valueOf(item, ["numero", "number", "comprobanteNumero"]) || "-"}</td>
                        <td className="px-2 py-2 text-right">
                          {valueOf(item, ["total", "importeTotal", "amount"]) || "-"}
                        </td>
                        <td className="px-2 py-2 text-right">
                          {documentId && (
                            <button
                              type="button"
                              onClick={() => openPdf(documentId)}
                              className="rounded border px-2 py-1 text-xs hover:bg-gray-50"
                            >
                              PDF
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
                {!loading && !documents.length && (
                  <tr>
                    <td colSpan={5} className="px-3 py-8 text-center text-gray-500">
                      Sin documentos.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="border">
          <div className="border-b px-3 py-2 text-sm font-semibold">Remitos pendientes</div>
          <div className="max-h-[520px] overflow-y-auto">
            {pendingOrders.map((order) => (
              <div key={order.id} className="border-b px-3 py-2 text-sm">
                <div className="font-medium">{order.remitoNumber || order.orderNumber}</div>
                <div className="text-gray-600">{order.customerName}</div>
                <div className="text-xs text-gray-500">{order.orderNumber}</div>
              </div>
            ))}
            {!pendingOrders.length && <div className="px-3 py-8 text-center text-sm text-gray-500">Sin remitos pendientes.</div>}
          </div>
        </section>
      </div>
    </div>
  );
}
