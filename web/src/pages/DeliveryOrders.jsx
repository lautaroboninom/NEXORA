import { useEffect, useMemo, useState } from "react";
import {
  getBlob,
  getDeliveryOrders,
  getDeliveryOrderRemitoHistory,
  patchDeliveryOrderRemitoLocation,
  postDeliveryOrderCancel,
  postDeliveryOrderDelivered,
  postDeliveryOrderInvoiced,
  postDeliveryOrderPrepared,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import DeliveryOrderCreateForm from "../components/DeliveryOrderCreateForm.jsx";
import DeliveryOrderRemitoModal from "../components/DeliveryOrderRemitoModal.jsx";
import {
  deliveryOrderCommercialLabel,
  deliveryOrderItemsSummary,
} from "../lib/delivery-orders";
import { Plus, Printer, X } from "lucide-react";

const STATUS_LABELS = {
  pendiente_armado: "Pendiente de armado",
  armado_pendiente_entrega: "Listo para retiro",
  entregado_pendiente_facturacion: "Pendiente de facturación",
  facturado: "Facturado",
  cancelado: "Cancelado",
};

const STATUS_CHIP_LABELS = {
  ...STATUS_LABELS,
  entregado_pendiente_facturacion: "Entregado",
};

const STATUS_CHIP_CLASSES = {
  pendiente_armado: "border-amber-300 bg-amber-100 text-amber-900",
  armado_pendiente_entrega: "border-emerald-200 bg-emerald-50 text-emerald-800",
  entregado_pendiente_facturacion: "border-gray-200 bg-gray-100 text-gray-600",
  facturado: "border-slate-200 bg-slate-50 text-slate-600",
  cancelado: "border-red-200 bg-red-50 text-red-700",
};

const TYPE_LABELS = {
  sale: "Venta",
  service_release: "Servicio técnico",
  rental: "Alquiler",
};

const PAGE_SIZE = 25;

function StatusChip({ status }) {
  const chipClass = STATUS_CHIP_CLASSES[status] || "border-gray-200 bg-gray-50 text-gray-700";
  return (
    <span
      className={`inline-flex whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${chipClass}`}
      title={STATUS_LABELS[status] || status || ""}
    >
      {STATUS_CHIP_LABELS[status] || status || "-"}
    </span>
  );
}

function ArticlesCell({ order }) {
  const summary = deliveryOrderItemsSummary(order);
  return (
    <div className="max-w-[360px]">
      <div className="font-medium text-gray-900">{summary.primary}</div>
      {summary.secondary && <div className="mt-0.5 text-xs text-gray-500">{summary.secondary}</div>}
    </div>
  );
}

function OrderNumberCell({ value }) {
  const text = String(value || "-").trim() || "-";
  const parts = text.split("-");
  if (parts.length >= 3) {
    return (
      <div className="w-[116px] leading-tight">
        <div className="font-semibold text-gray-900">{parts.slice(0, 2).join("-")}</div>
        <div className="mt-0.5 text-xs font-semibold text-gray-900">{parts.slice(2).join("-")}</div>
      </div>
    );
  }
  return <div className="w-[116px] break-words font-semibold leading-tight text-gray-900">{text}</div>;
}

const remitoPrintUrlFromGroupId = (groupId) =>
  `/api/ordenes-entrega/remito-bejerman/${encodeURIComponent(groupId)}/print/`;

function remitoPrintUrl(order) {
  return order?.remitoNumber && order?.bejermanRemitoGroupId
    ? remitoPrintUrlFromGroupId(order.bejermanRemitoGroupId)
    : "";
}

function exitRemitoIngresoId(order) {
  if (order?.deliveryType !== "service_release") return "";
  if (order?.ingresoId) return String(order.ingresoId);
  const reference = String(order?.sourceReference || "").trim();
  const match = reference.match(/\d+/);
  return match ? match[0] : "";
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("es-AR", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

export default function DeliveryOrders() {
  const { user } = useAuth();
  const [filters, setFilters] = useState({ status: "", q: "", deliveryType: "" });
  const [searchText, setSearchText] = useState("");
  const [orders, setOrders] = useState([]);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [remitoModalOpen, setRemitoModalOpen] = useState(false);
  const [historyModalOpen, setHistoryModalOpen] = useState(false);
  const [remitoHistory, setRemitoHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const canCreate = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_CREATE);
  const canPrepare = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_PREPARE);
  const canDeliver = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_DELIVER);
  const canInvoice = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_INVOICE);
  const canCancel = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_CANCEL);
  const canMoveRemito = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_UPDATE_REMITO_LOCATION);
  const canGenerateRemito = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_GENERATE_BEJERMAN_REMITO);
  const canAssignArticles = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_ASSIGN_ARTICLES);

  const openExitRemito = async (order) => {
    const ingresoId = exitRemitoIngresoId(order);
    if (!ingresoId) {
      setError("La orden no tiene un ingreso técnico vinculado para imprimir el remito de salida.");
      return;
    }
    setSaving(true);
    try {
      const blob = await getBlob(`/api/ordenes-entrega/${encodeURIComponent(order.id)}/remito-salida/`);
      if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setError("");
    } catch (err) {
      setError(err?.message || "No se pudo imprimir el remito de salida.");
    } finally {
      setSaving(false);
    }
  };

  const selectedOrders = useMemo(
    () => orders.filter((order) => selected.includes(order.id)),
    [orders, selected]
  );

  const loadRemitoHistory = async () => {
    setHistoryLoading(true);
    try {
      const data = await getDeliveryOrderRemitoHistory({ limit: 20 });
      setRemitoHistory(Array.isArray(data?.items) ? data.items : []);
      setError("");
    } catch (err) {
      setError(err?.message || "No se pudo cargar el historial de remitos.");
    } finally {
      setHistoryLoading(false);
    }
  };

  const visibleOrdersByRemitoGroup = useMemo(() => {
    const grouped = new Map();
    orders.forEach((order) => {
      const groupId = order.bejermanRemitoGroupId;
      if (!groupId) return;
      const group = grouped.get(groupId) || [];
      group.push(order);
      grouped.set(groupId, group);
    });
    return grouped;
  }, [orders]);

  const remitoGroupSummary = (order) => {
    const group = order.bejermanRemitoGroup;
    if (group?.orders?.length) {
      return {
        count: Number(group.orderCount || group.orders.length),
        labels: group.orders.map((item) => item.sourceReference || item.orderNumber).filter(Boolean),
      };
    }
    if (!order.bejermanRemitoGroupId) return null;
    const visibleGroup = visibleOrdersByRemitoGroup.get(order.bejermanRemitoGroupId) || [];
    return {
      count: visibleGroup.length,
      labels: visibleGroup.map((item) => item.sourceReference || item.orderNumber).filter(Boolean),
    };
  };

  const load = (pageToLoad = page) => {
    setLoading(true);
    getDeliveryOrders({
      status: filters.status,
      q: filters.q,
      deliveryType: filters.deliveryType,
      limit: PAGE_SIZE,
      offset: pageToLoad * PAGE_SIZE,
    })
      .then((data) => {
        setOrders(Array.isArray(data?.items) ? data.items : []);
        setTotal(Number(data?.total || 0));
        setSelected([]);
        setError("");
      })
      .catch((err) => setError(err?.message || "No se pudieron cargar las órdenes."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, [filters.status, filters.deliveryType, filters.q, page]);

  const updateFilter = (field) => (event) => {
    setFilters((prev) => ({ ...prev, [field]: event.target.value }));
    if (field !== "q") setPage(0);
  };

  const applySearch = () => {
    const nextQuery = searchText.trim();
    setSearchText(nextQuery);
    if (filters.q === nextQuery && page === 0) {
      load(0);
      return;
    }
    setFilters((prev) => ({ ...prev, q: nextQuery }));
    if (page === 0) {
      return;
    }
    setPage(0);
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

  const openRemitoModal = () => {
    if (!selectedOrders.length) return;
    setRemitoModalOpen(true);
  };

  const openHistoryModal = () => {
    setHistoryModalOpen(true);
    loadRemitoHistory();
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Órdenes de entrega</h1>
          <p className="text-sm text-gray-600">Artículos, preparación, remitos y cierre administrativo.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={openHistoryModal}
            className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50"
          >
            <Printer className="h-4 w-4" aria-hidden="true" />
            Historial remitos
          </button>
          {canCreate && (
            <button
              type="button"
              onClick={() => setCreateModalOpen(true)}
              className="inline-flex items-center gap-2 rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Nueva orden de entrega
            </button>
          )}
          {canGenerateRemito && (
            <button
              type="button"
              onClick={openRemitoModal}
              disabled={!selectedOrders.length || saving}
              className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
            >
              Generar remito Bejerman
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-2 border p-3">
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Estado</span>
          <select
            value={filters.status}
            onChange={updateFilter("status")}
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
            onChange={updateFilter("deliveryType")}
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
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") applySearch();
            }}
            className="h-9 w-full rounded border px-2"
            placeholder="Orden, cliente, artículo, partida o vendedor"
          />
        </label>
        <button type="button" onClick={applySearch} className="h-9 rounded border px-3 text-sm hover:bg-gray-50">
          Aplicar
        </button>
      </div>

      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      <div className="overflow-x-auto border">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
            <tr>
              <th className="px-2 py-2"></th>
              <th className="w-[116px] px-2 py-2">Orden</th>
              <th className="px-2 py-2">Cliente</th>
              <th className="px-2 py-2">Tipo</th>
              <th className="px-2 py-2">Estado</th>
              <th className="px-2 py-2">Artículos</th>
              <th className="px-2 py-2">Comercial</th>
              <th className="px-2 py-2">Remito</th>
              <th className="px-2 py-2">Factura</th>
              <th className="px-2 py-2">Acciones</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={10} className="px-3 py-8 text-center text-gray-500">
                  Cargando...
                </td>
              </tr>
            ) : (
              orders.map((order) => {
                const printUrl = remitoPrintUrl(order);
                const ingresoRemitoId = exitRemitoIngresoId(order);
                const groupSummary = remitoGroupSummary(order);
                const groupLabels = (groupSummary?.labels || []).slice(0, 4).join(", ");
                return (
                <tr
                  key={order.id}
                  className={`border-t align-top ${order.status === "pendiente_armado" ? "bg-amber-50/80" : ""}`}
                >
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.includes(order.id)}
                      onChange={() => toggleSelected(order.id)}
                      disabled={order.remitoNumber || order.status === "facturado" || order.status === "cancelado"}
                      className="h-5 w-5 cursor-pointer rounded border-gray-300 align-middle accent-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
                    />
                  </td>
                  <td className="w-[116px] px-2 py-2">
                    <OrderNumberCell value={order.orderNumber} />
                  </td>
                  <td className="px-2 py-2">{order.customerName || "-"}</td>
                  <td className="px-2 py-2">{TYPE_LABELS[order.deliveryType] || order.deliveryType}</td>
                  <td className="px-2 py-2">
                    <StatusChip status={order.status} />
                  </td>
                  <td className="px-2 py-2"><ArticlesCell order={order} /></td>
                  <td className="px-2 py-2 text-xs text-gray-600">{deliveryOrderCommercialLabel(order) || "-"}</td>
                  <td className="px-2 py-2">
                    <div>{order.remitoNumber || "-"}</div>
                    {groupSummary?.count > 1 && (
                      <div className="mt-1 max-w-[240px] rounded border border-blue-100 bg-blue-50 px-2 py-1 text-[11px] text-blue-800">
                        Agrupa {groupSummary.count} órdenes{groupLabels ? `: ${groupLabels}` : ""}
                      </div>
                    )}
                    {printUrl && (
                      <a
                        href={printUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 inline-flex items-center gap-1 text-xs text-blue-700 hover:underline"
                      >
                        <Printer className="h-3.5 w-3.5" aria-hidden="true" />
                        Imprimir remito
                      </a>
                    )}
                    {ingresoRemitoId && (
                      <button
                        type="button"
                        onClick={() => openExitRemito(order)}
                        disabled={saving}
                        className="mt-1 inline-flex items-center gap-1 text-xs text-blue-700 hover:underline disabled:opacity-50"
                      >
                        <Printer className="h-3.5 w-3.5" aria-hidden="true" />
                        Imprimir salida
                      </button>
                    )}
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
                );
              })
            )}
            {!loading && !orders.length && (
              <tr>
                <td colSpan={10} className="px-3 py-8 text-center text-gray-500">
                  Sin órdenes.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-gray-600">
        <div>
          {total > 0
            ? `Mostrando ${page * PAGE_SIZE + 1}-${page * PAGE_SIZE + orders.length} de ${total}`
            : "Sin resultados"}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setPage((current) => Math.max(0, current - 1))}
            disabled={loading || page === 0}
            className="h-9 rounded border px-3 text-sm hover:bg-gray-50 disabled:opacity-50"
          >
            Anterior
          </button>
          <span className="min-w-[90px] text-center">
            Página {page + 1} de {Math.max(1, Math.ceil(total / PAGE_SIZE))}
          </span>
          <button
            type="button"
            onClick={() => setPage((current) => current + 1)}
            disabled={loading || (page + 1) * PAGE_SIZE >= total}
            className="h-9 rounded border px-3 text-sm hover:bg-gray-50 disabled:opacity-50"
          >
            Siguiente
          </button>
        </div>
      </div>
      <DeliveryOrderRemitoModal
        open={remitoModalOpen}
        orders={selectedOrders}
        canAssignArticles={canAssignArticles}
        onClose={() => setRemitoModalOpen(false)}
        onGenerated={() => {
          setRemitoModalOpen(false);
          setSelected([]);
          load();
        }}
      />
      <RemitoHistoryModal
        open={historyModalOpen}
        loading={historyLoading}
        items={remitoHistory}
        onClose={() => setHistoryModalOpen(false)}
      />
      <NewDeliveryOrderModal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onCreated={() => {
          setError("");
          load();
        }}
      />
    </div>
  );
}

function RemitoHistoryModal({ open, loading, items, onClose }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-5xl overflow-hidden rounded bg-white shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Historial de remitos Bejerman</h2>
            <p className="text-sm text-gray-600">Últimos remitos emitidos y órdenes agrupadas.</p>
          </div>
          <button type="button" className="rounded p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900" onClick={onClose} aria-label="Cerrar">
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>

        <div className="max-h-[calc(90vh-73px)] overflow-auto">
          <table className="min-w-full text-left text-xs">
            <thead className="bg-gray-50 text-gray-500">
              <tr>
                <th className="px-4 py-2 font-medium">Remito</th>
                <th className="px-4 py-2 font-medium">Cliente</th>
                <th className="px-4 py-2 font-medium">Órdenes</th>
                <th className="px-4 py-2 font-medium">Fecha</th>
                <th className="px-4 py-2 text-right font-medium">PDF</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {loading && (
                <tr>
                  <td colSpan={5} className="px-4 py-10 text-center text-gray-500">
                    Cargando...
                  </td>
                </tr>
              )}
              {!loading &&
                items.map((group) => (
                  <tr key={group.id} className="align-top hover:bg-gray-50">
                    <td className="whitespace-nowrap px-4 py-3">
                      <div className="font-medium text-gray-900">{group.remitoNumber || group.id}</div>
                      <div className="text-[11px] text-gray-500">
                        {[group.comprobanteTipo, group.operationCode, group.depositCode].filter(Boolean).join(" / ") || "-"}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <div>{group.customerName || "-"}</div>
                      {group.customerCode && <div className="text-[11px] text-gray-500">{group.customerCode}</div>}
                    </td>
                    <td className="px-4 py-3">
                      <div>{group.orderCount || 0} órdenes</div>
                      <div className="mt-1 max-w-md text-[11px] text-gray-500">
                        {(group.orders || [])
                          .slice(0, 4)
                          .map((order) => order.sourceReference || order.orderNumber)
                          .filter(Boolean)
                          .join(", ") || "-"}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-gray-600">
                      {formatDateTime(group.generatedAt || group.createdAt)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-right">
                      {group.printUrl ? (
                        <a
                          href={group.printUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-blue-700 hover:underline"
                        >
                          <Printer className="h-3.5 w-3.5" aria-hidden="true" />
                          Imprimir
                        </a>
                      ) : (
                        <span className="text-gray-500">Pendiente</span>
                      )}
                    </td>
                  </tr>
                ))}
              {!loading && !items.length && (
                <tr>
                  <td colSpan={5} className="px-4 py-10 text-center text-gray-500">
                    Todavía no hay remitos Bejerman emitidos.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function NewDeliveryOrderModal({ open, onClose, onCreated }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-6xl overflow-hidden rounded bg-white shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Nueva orden de entrega</h2>
            <p className="text-sm text-gray-600">Clientes, artículos, partidas y detalle completo como en Portal.</p>
          </div>
          <button type="button" className="text-sm text-gray-500 hover:text-gray-900" onClick={onClose}>
            Cerrar
          </button>
        </div>

        <div className="max-h-[calc(90vh-73px)] overflow-auto p-5">
          <DeliveryOrderCreateForm
            compact
            submitLabel="Crear entrega"
            onCancel={onClose}
            onCreated={(created) => {
              onCreated(created);
              onClose();
            }}
          />
        </div>
      </div>
    </div>
  );
}
