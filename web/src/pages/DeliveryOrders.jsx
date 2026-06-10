import { useEffect, useMemo, useState } from "react";
import {
  getClientesBasico,
  getDeliveryOrders,
  patchDeliveryOrderRemitoLocation,
  postDeliveryOrder,
  postDeliveryOrderBejermanRemito,
  postDeliveryOrderCancel,
  postDeliveryOrderDelivered,
  postDeliveryOrderInvoiced,
  postDeliveryOrderPrepared,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";

const STATUS_LABELS = {
  pendiente_armado: "Pendiente de armado",
  armado_pendiente_entrega: "Listo para retiro",
  entregado_pendiente_facturacion: "Pendiente de facturación",
  facturado: "Facturado",
  cancelado: "Cancelado",
};

const TYPE_LABELS = {
  sale: "Venta",
  service_release: "Servicio técnico",
  rental: "Alquiler",
};

const emptyForm = {
  customerId: "",
  deliveryType: "sale",
  priority: "normal",
  sellerName: "",
  operationCompanyLabel: "",
  equipmentModel: "",
  equipmentSerial: "",
  equipmentInternalNumber: "",
  rawPedido: "",
  itemDescription: "",
  articleCode: "",
  quantity: "1",
};

function downloadUrl(url) {
  window.open(url, "_blank", "noopener,noreferrer");
}

export default function DeliveryOrders() {
  const { user } = useAuth();
  const [filters, setFilters] = useState({ status: "", q: "", deliveryType: "" });
  const [orders, setOrders] = useState([]);
  const [customers, setCustomers] = useState([]);
  const [selected, setSelected] = useState([]);
  const [form, setForm] = useState(emptyForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const canCreate = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_CREATE);
  const canPrepare = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_PREPARE);
  const canDeliver = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_DELIVER);
  const canInvoice = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_INVOICE);
  const canCancel = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_CANCEL);
  const canMoveRemito = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_UPDATE_REMITO_LOCATION);
  const canGenerateRemito = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_GENERATE_BEJERMAN_REMITO);

  const selectedOrders = useMemo(
    () => orders.filter((order) => selected.includes(order.id)),
    [orders, selected]
  );

  const load = () => {
    setLoading(true);
    getDeliveryOrders({
      status: filters.status,
      q: filters.q,
      deliveryType: filters.deliveryType,
      limit: 120,
    })
      .then((data) => {
        setOrders(Array.isArray(data?.items) ? data.items : []);
        setError("");
      })
      .catch((err) => setError(err?.message || "No se pudieron cargar las órdenes."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, [filters.status, filters.deliveryType]);

  useEffect(() => {
    getClientesBasico()
      .then((data) => setCustomers(Array.isArray(data) ? data : []))
      .catch(() => setCustomers([]));
  }, []);

  const customerById = useMemo(() => {
    const out = new Map();
    customers.forEach((customer) => out.set(String(customer.id), customer));
    return out;
  }, [customers]);

  const createOrder = async (event) => {
    event.preventDefault();
    const customer = customerById.get(String(form.customerId));
    setSaving(true);
    try {
      await postDeliveryOrder({
        customerId: form.customerId ? Number(form.customerId) : null,
        customerName: customer?.razon_social || customer?.nombre || "",
        bejermanCustomerCode: customer?.cod_empresa || "",
        deliveryType: form.deliveryType,
        priority: form.priority,
        sellerName: form.sellerName,
        operationCompanyLabel: form.operationCompanyLabel,
        equipmentModel: form.equipmentModel,
        equipmentSerial: form.equipmentSerial,
        equipmentInternalNumber: form.equipmentInternalNumber,
        rawPedido: form.rawPedido,
        items: [
          {
            description: form.itemDescription || form.rawPedido || "Equipo",
            articleCode: form.articleCode,
            quantity: form.quantity || 1,
          },
        ],
      });
      setForm(emptyForm);
      setError("");
      load();
    } catch (err) {
      setError(err?.message || "No se pudo crear la orden.");
    } finally {
      setSaving(false);
    }
  };

  const runAction = async (action) => {
    setSaving(true);
    try {
      await action();
      setError("");
      load();
    } catch (err) {
      setError(err?.message || "No se pudo completar la acción.");
    } finally {
      setSaving(false);
    }
  };

  const toggleSelected = (orderId) => {
    setSelected((current) =>
      current.includes(orderId) ? current.filter((id) => id !== orderId) : [...current, orderId]
    );
  };

  const generateRemito = () => {
    if (!selectedOrders.length) return;
    const notes = window.prompt("Observaciones para el remito", "") || "";
    runAction(async () => {
      const result = await postDeliveryOrderBejermanRemito({
        orderIds: selectedOrders.map((order) => order.id),
        notes,
      });
      if (result?.pdfUrl) downloadUrl(result.pdfUrl);
      setSelected([]);
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Órdenes de entrega</h1>
          <p className="text-sm text-gray-600">Preparación, remitos y cierre administrativo.</p>
        </div>
        {canGenerateRemito && (
          <button
            type="button"
            onClick={generateRemito}
            disabled={!selectedOrders.length || saving}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
          >
            Generar remito Bejerman
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-2 border p-3">
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Estado</span>
          <select
            value={filters.status}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
            className="h-9 rounded border px-2"
          >
            <option value="">Todos</option>
            {Object.entries(STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Tipo</span>
          <select
            value={filters.deliveryType}
            onChange={(event) => setFilters((prev) => ({ ...prev, deliveryType: event.target.value }))}
            className="h-9 rounded border px-2"
          >
            <option value="">Todos</option>
            {Object.entries(TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="min-w-[220px] flex-1 text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Buscar</span>
          <input
            value={filters.q}
            onChange={(event) => setFilters((prev) => ({ ...prev, q: event.target.value }))}
            onKeyDown={(event) => {
              if (event.key === "Enter") load();
            }}
            className="h-9 w-full rounded border px-2"
            placeholder="Orden, cliente, remito, equipo"
          />
        </label>
        <button type="button" onClick={load} className="h-9 rounded border px-3 text-sm hover:bg-gray-50">
          Aplicar
        </button>
      </div>

      {canCreate && (
        <form onSubmit={createOrder} className="grid gap-2 border p-3 md:grid-cols-6">
          <select
            value={form.customerId}
            onChange={(event) => setForm((prev) => ({ ...prev, customerId: event.target.value }))}
            className="h-9 rounded border px-2 md:col-span-2"
            required
          >
            <option value="">Cliente</option>
            {customers.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.razon_social || customer.nombre}
              </option>
            ))}
          </select>
          <select
            value={form.deliveryType}
            onChange={(event) => setForm((prev) => ({ ...prev, deliveryType: event.target.value }))}
            className="h-9 rounded border px-2"
          >
            {Object.entries(TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <select
            value={form.priority}
            onChange={(event) => setForm((prev) => ({ ...prev, priority: event.target.value }))}
            className="h-9 rounded border px-2"
          >
            <option value="normal">Normal</option>
            <option value="urgente">Urgente</option>
          </select>
          <input
            value={form.sellerName}
            onChange={(event) => setForm((prev) => ({ ...prev, sellerName: event.target.value }))}
            className="h-9 rounded border px-2"
            placeholder="Vendedor"
          />
          <input
            value={form.operationCompanyLabel}
            onChange={(event) => setForm((prev) => ({ ...prev, operationCompanyLabel: event.target.value }))}
            className="h-9 rounded border px-2"
            placeholder="Empresa"
          />
          <input
            value={form.equipmentModel}
            onChange={(event) => setForm((prev) => ({ ...prev, equipmentModel: event.target.value }))}
            className="h-9 rounded border px-2 md:col-span-2"
            placeholder="Equipo/modelo"
          />
          <input
            value={form.equipmentSerial}
            onChange={(event) => setForm((prev) => ({ ...prev, equipmentSerial: event.target.value }))}
            className="h-9 rounded border px-2"
            placeholder="N/S"
          />
          <input
            value={form.equipmentInternalNumber}
            onChange={(event) => setForm((prev) => ({ ...prev, equipmentInternalNumber: event.target.value }))}
            className="h-9 rounded border px-2"
            placeholder="Interno"
          />
          <input
            value={form.articleCode}
            onChange={(event) => setForm((prev) => ({ ...prev, articleCode: event.target.value }))}
            className="h-9 rounded border px-2"
            placeholder="Artículo"
          />
          <input
            value={form.quantity}
            onChange={(event) => setForm((prev) => ({ ...prev, quantity: event.target.value }))}
            className="h-9 rounded border px-2"
            placeholder="Cantidad"
          />
          <input
            value={form.rawPedido}
            onChange={(event) => setForm((prev) => ({ ...prev, rawPedido: event.target.value }))}
            className="h-9 rounded border px-2 md:col-span-3"
            placeholder="Pedido"
            required
          />
          <input
            value={form.itemDescription}
            onChange={(event) => setForm((prev) => ({ ...prev, itemDescription: event.target.value }))}
            className="h-9 rounded border px-2 md:col-span-2"
            placeholder="Descripción del ítem"
          />
          <button type="submit" disabled={saving} className="h-9 rounded bg-blue-600 px-3 text-sm text-white disabled:opacity-50">
            Crear orden
          </button>
        </form>
      )}

      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      <div className="overflow-x-auto border">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
            <tr>
              <th className="px-2 py-2"></th>
              <th className="px-2 py-2">Orden</th>
              <th className="px-2 py-2">Cliente</th>
              <th className="px-2 py-2">Tipo</th>
              <th className="px-2 py-2">Estado</th>
              <th className="px-2 py-2">Equipo</th>
              <th className="px-2 py-2">Remito</th>
              <th className="px-2 py-2">Factura</th>
              <th className="px-2 py-2">Acciones</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={9} className="px-3 py-8 text-center text-gray-500">
                  Cargando...
                </td>
              </tr>
            ) : (
              orders.map((order) => (
                <tr key={order.id} className="border-t align-top">
                  <td className="px-2 py-2">
                    <input
                      type="checkbox"
                      checked={selected.includes(order.id)}
                      onChange={() => toggleSelected(order.id)}
                      disabled={order.remitoNumber || order.status === "facturado" || order.status === "cancelado"}
                    />
                  </td>
                  <td className="px-2 py-2 font-medium">{order.orderNumber}</td>
                  <td className="px-2 py-2">{order.customerName || "-"}</td>
                  <td className="px-2 py-2">{TYPE_LABELS[order.deliveryType] || order.deliveryType}</td>
                  <td className="px-2 py-2">{STATUS_LABELS[order.status] || order.status}</td>
                  <td className="px-2 py-2">
                    <div>{order.equipmentModel || "-"}</div>
                    <div className="text-xs text-gray-500">
                      {[order.equipmentSerial, order.equipmentInternalNumber].filter(Boolean).join(" / ")}
                    </div>
                  </td>
                  <td className="px-2 py-2">
                    <div>{order.remitoNumber || "-"}</div>
                    {order.remitoNumber && canMoveRemito && (
                      <select
                        value={order.remitoLocation || ""}
                        onChange={(event) =>
                          runAction(() =>
                            patchDeliveryOrderRemitoLocation(order.id, { remitoLocation: event.target.value })
                          )
                        }
                        className="mt-1 h-8 rounded border px-2 text-xs"
                      >
                        <option value="">Ubicación</option>
                        <option value="recepcion">Recepción</option>
                        <option value="oficina">Oficina</option>
                      </select>
                    )}
                  </td>
                  <td className="px-2 py-2">{order.invoiceNumber || "-"}</td>
                  <td className="px-2 py-2">
                    <div className="flex flex-wrap gap-1">
                      {canPrepare && order.status === "pendiente_armado" && (
                        <button
                          type="button"
                          onClick={() => runAction(() => postDeliveryOrderPrepared(order.id))}
                          className="rounded border px-2 py-1 text-xs hover:bg-gray-50"
                        >
                          Preparar
                        </button>
                      )}
                      {canDeliver && order.status !== "facturado" && order.status !== "cancelado" && !order.remitoNumber && (
                        <button
                          type="button"
                          onClick={() => {
                            const remitoNumber = window.prompt("Número de remito", "");
                            if (remitoNumber) runAction(() => postDeliveryOrderDelivered(order.id, { remitoNumber }));
                          }}
                          className="rounded border px-2 py-1 text-xs hover:bg-gray-50"
                        >
                          Cargar remito
                        </button>
                      )}
                      {canInvoice && order.status === "entregado_pendiente_facturacion" && (
                        <button
                          type="button"
                          onClick={() => {
                            const invoiceNumber = window.prompt("Número de factura", "");
                            if (invoiceNumber) runAction(() => postDeliveryOrderInvoiced(order.id, { invoiceNumber }));
                          }}
                          className="rounded border px-2 py-1 text-xs hover:bg-gray-50"
                        >
                          Facturar
                        </button>
                      )}
                      {canCancel && order.status !== "facturado" && order.status !== "cancelado" && (
                        <button
                          type="button"
                          onClick={() => runAction(() => postDeliveryOrderCancel(order.id))}
                          className="rounded border px-2 py-1 text-xs text-red-700 hover:bg-red-50"
                        >
                          Cancelar
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))
            )}
            {!loading && !orders.length && (
              <tr>
                <td colSpan={9} className="px-3 py-8 text-center text-gray-500">
                  Sin órdenes.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
