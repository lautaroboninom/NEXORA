import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getDeliveryOrders } from "../lib/api";
import { deliveryOrderItemsSummary, deliveryOrderSourceLabel } from "../lib/delivery-orders";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import {
  DesktopTableWrap,
  MobileDataCard,
  MobileDataField,
  MobileDataList,
  ResponsiveActionBar,
  fullWidthButtonClass,
} from "../components/Responsive.jsx";

const STATUS_LABELS = {
  pendiente_armado: "Pendiente de armado",
  armado_pendiente_entrega: "Listo para retiro",
  entregado_pendiente_facturacion: "Pendiente de facturación",
  entregado_no_facturable: "Entregado",
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
  const { user } = useAuth();
  const canBejermanPurchases = can(user, PERMISSION_CODES.PAGE_BEJERMAN_PURCHASE_ENTRIES);
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    getDeliveryOrders({
      status: "pendiente_armado,armado_pendiente_entrega,entregado_pendiente_facturacion,entregado_no_facturable",
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
  const pendingDeliveryTotal = (summary.pendiente_armado || 0) + (summary.armado_pendiente_entrega || 0);
  const remitoTotal = (summary.entregado_pendiente_facturacion || 0) + (summary.entregado_no_facturable || 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Órdenes de entrega</h1>
          <p className="text-sm text-gray-600">Resumen reducido de preparación, entrega y remitos.</p>
        </div>
        <ResponsiveActionBar>
          <Link to="/administracion/ordenes-entrega" className={`rounded bg-blue-600 px-3 py-2 text-center text-sm text-white hover:bg-blue-700 ${fullWidthButtonClass}`}>
            Abrir órdenes de entrega
          </Link>
          <Link to="/ingresos/nuevo" className={`rounded border px-3 py-2 text-center text-sm hover:bg-gray-50 ${fullWidthButtonClass}`}>
            Nuevo ingreso
          </Link>
          {canBejermanPurchases && (
            <Link to="/ingresos/nuevo?tab=mercaderia" className={`rounded border border-emerald-300 px-3 py-2 text-center text-sm text-emerald-800 hover:bg-emerald-50 ${fullWidthButtonClass}`}>
              Ingreso de mercadería
            </Link>
          )}
        </ResponsiveActionBar>
      </div>

      <div className="grid gap-2 md:grid-cols-4">
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">Sin entregar</div>
          <div className="text-2xl font-semibold">{pendingDeliveryTotal}</div>
        </div>
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">A preparar</div>
          <div className="text-2xl font-semibold">{summary.pendiente_armado || 0}</div>
        </div>
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">Listas para entrega</div>
          <div className="text-2xl font-semibold">{summary.armado_pendiente_entrega || 0}</div>
        </div>
        <div className="border p-3">
          <div className="text-xs uppercase text-gray-500">Con remito</div>
          <div className="text-2xl font-semibold">{remitoTotal}</div>
        </div>
      </div>

      <section className="border">
        <div className="flex items-center justify-between gap-3 border-b px-3 py-2">
          <div>
            <div className="text-sm font-semibold">Resumen de órdenes</div>
            <div className="text-xs text-gray-500">Vista reducida de las órdenes activas.</div>
          </div>
          <Link to="/administracion/ordenes-entrega" className="shrink-0 text-sm text-blue-700 hover:underline">
            Vista completa
          </Link>
        </div>
        {error && <div className="px-3 py-2 text-sm text-red-700">{error}</div>}
        {loading ? (
          <div className="px-3 py-6 text-sm text-gray-500">Cargando...</div>
        ) : (
          <>
            <MobileDataList className="p-3">
              {orders.map((order) => {
                const articles = deliveryOrderItemsSummary(order, 1);
                return (
                  <MobileDataCard key={order.id}>
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-semibold text-gray-900">{order.orderNumber}</div>
                        <div className="mt-1 text-sm text-gray-600">{order.customerName || "-"}</div>
                      </div>
                      <Link to="/administracion/ordenes-entrega" className="shrink-0 text-sm text-blue-700 hover:underline">
                        Abrir
                      </Link>
                    </div>
                    <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                      <MobileDataField label="Artículos">
                        <div className="font-medium text-gray-900">{articles.primary}</div>
                        <div className="text-xs text-gray-500">{articles.secondary || deliveryOrderSourceLabel(order) || "-"}</div>
                      </MobileDataField>
                      <MobileDataField label="Estado" value={STATUS_LABELS[order.status] || order.status} />
                      <MobileDataField label="Remito" value={order.remitoNumber || "-"} />
                    </div>
                  </MobileDataCard>
                );
              })}
              {!orders.length && <MobileDataCard className="text-center text-gray-500">Sin órdenes activas.</MobileDataCard>}
            </MobileDataList>
            <DesktopTableWrap>
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
            </DesktopTableWrap>
          </>
        )}
      </section>
    </div>
  );
}
