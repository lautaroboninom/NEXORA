import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getDeliveryOrders } from "../lib/api";
import { deliveryOrderItemsSummary, deliveryOrderSourceLabel } from "../lib/delivery-orders";

const STATUS_LABELS = {
  pendiente_armado: "Pendiente de armado",
  armado_pendiente_entrega: "Listo para retiro",
  entregado_pendiente_facturacion: "Pendiente de facturación",
  facturado: "Facturado",
  cancelado: "Cancelado",
};

function OrderRow({ order }) {
  const articles = deliveryOrderItemsSummary(order, 1);
  return (
    <tr className="border-t">
      <td className="px-2 py-2 font-medium">{order.orderNumber}</td>
      <td className="px-2 py-2">{order.customerName || "-"}</td>
      <td className="px-2 py-2">
        <div className="font-medium text-gray-900">{articles.primary}</div>
        <div className="text-xs text-gray-500">{articles.secondary || deliveryOrderSourceLabel(order) || "-"}</div>
      </td>
      <td className="px-2 py-2">{STATUS_LABELS[order.status] || order.status}</td>
      <td className="px-2 py-2">{order.remitoNumber || "-"}</td>
      <td className="px-2 py-2 text-right">
        <Link to="/administracion/ordenes-entrega" className="text-blue-700 hover:underline">
          Abrir
        </Link>
      </td>
    </tr>
  );
}

export default function RecepcionDashboard() {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    getDeliveryOrders({
      status: "pendiente_armado,armado_pendiente_entrega,entregado_pendiente_facturacion",
      limit: 40,
    })
      .then((data) => {
        if (!active) return;
        setOrders(Array.isArray(data?.items) ? data.items : []);
        setError("");
      })
      .catch((err) => {
        if (!active) return;
        setError(err?.message || "No se pudieron cargar las órdenes.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const summary = useMemo(() => {
    return orders.reduce(
      (acc, order) => {
        acc.total += 1;
        acc[order.status] = (acc[order.status] || 0) + 1;
        return acc;
      },
      { total: 0 }
    );
  }, [orders]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Recepción</h1>
          <p className="text-sm text-gray-600">Ingresos, órdenes de entrega y remitos.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link to="/ingresos/nuevo" className="rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700">
            Nuevo ingreso
          </Link>
          <Link to="/administracion/ordenes-entrega" className="rounded border px-3 py-2 text-sm hover:bg-gray-50">
            Órdenes de entrega
          </Link>
        </div>
      </div>

      <div className="grid gap-2 md:grid-cols-4">
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">Abiertas</div>
          <div className="text-2xl font-semibold">{summary.total}</div>
        </div>
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">Pendientes</div>
          <div className="text-2xl font-semibold">{summary.pendiente_armado || 0}</div>
        </div>
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">Listas</div>
          <div className="text-2xl font-semibold">{summary.armado_pendiente_entrega || 0}</div>
        </div>
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">Con remito</div>
          <div className="text-2xl font-semibold">{summary.entregado_pendiente_facturacion || 0}</div>
        </div>
      </div>

      <section className="border">
        <div className="border-b px-3 py-2 text-sm font-semibold">Órdenes activas</div>
        {error && <div className="px-3 py-2 text-sm text-red-700">{error}</div>}
        {loading ? (
          <div className="px-3 py-6 text-sm text-gray-500">Cargando...</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-2">Orden</th>
                  <th className="px-2 py-2">Cliente</th>
                  <th className="px-2 py-2">Artículos</th>
                  <th className="px-2 py-2">Estado</th>
                  <th className="px-2 py-2">Remito</th>
                  <th className="px-2 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {orders.map((order) => (
                  <OrderRow key={order.id} order={order} />
                ))}
                {!orders.length && (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                      Sin órdenes activas.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
