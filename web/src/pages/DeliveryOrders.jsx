import { useEffect, useMemo, useRef, useState } from "react";
import {
  deliveryOrderInvoicePdfUrl,
  getBlob,
  getDeliveryOrder,
  getDeliveryOrders,
  getDeliveryOrderRemitoHistory,
  patchDeliveryOrderItemPartidas,
  patchDeliveryOrderRemitoLocation,
  postDeliveryOrderCancel,
  postDeliveryOrderDelivered,
  postDeliveryOrderDriveSync,
  postDeliveryOrderInvoiced,
  postDeliveryOrderPrepared,
} from "../lib/api";
import { openPrintablePdf, waitForPdfBlob } from "../lib/pdf";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import DeliveryOrderCreateForm from "../components/DeliveryOrderCreateForm.jsx";
import DeliveryOrderRemitoModal from "../components/DeliveryOrderRemitoModal.jsx";
import {
  deliveryOrderCompanyLabel,
  deliveryOrderCommercialLabel,
  deliveryOrderItemsSummary,
  deliveryOrderItemPriceCurrency,
  deliveryOrderPriceCurrency,
} from "../lib/delivery-orders";
import { CloudUpload, Pencil, Plus, Printer, X } from "lucide-react";
import {
  DesktopTableWrap,
  MobileDataCard,
  MobileDataField,
  MobileDataList,
  ResponsiveActionBar,
  ResponsiveModalOverlay,
  ResponsiveModalPanel,
  fullWidthButtonClass,
} from "../components/Responsive.jsx";
import { useLocation, useNavigate } from "react-router-dom";

const STATUS_LABELS = {
  pendiente_armado: "Pendiente de armado",
  armado_pendiente_entrega: "Listo para retiro",
  entregado_pendiente_facturacion: "Pendiente de facturación",
  entregado_no_facturable: "Entregado",
  facturado: "Facturado",
  cancelado: "Cancelado",
};

const STATUS_CHIP_LABELS = {
  ...STATUS_LABELS,
  entregado_pendiente_facturacion: "Entregado",
  entregado_no_facturable: "Entregado",
};

const STATUS_CHIP_CLASSES = {
  pendiente_armado: "border-amber-300 bg-amber-100 text-amber-900",
  armado_pendiente_entrega: "border-emerald-200 bg-emerald-50 text-emerald-800",
  entregado_pendiente_facturacion: "border-gray-200 bg-gray-100 text-gray-600",
  entregado_no_facturable: "border-sky-200 bg-sky-50 text-sky-800",
  facturado: "border-slate-200 bg-slate-50 text-slate-600",
  cancelado: "border-red-200 bg-red-50 text-red-700",
};

const PENDING_ARMADO_ROW_CLASS = "bg-amber-100/90";
const URGENT_PENDING_ARMADO_ROW_CLASS = "bg-orange-200/75";

const TYPE_LABELS = {
  sale: "Venta",
  service_release: "Servicio técnico",
  rental: "Alquiler",
  demo: "Demo",
};

const PAGE_SIZE = 25;
const NON_CANCELABLE_STATUSES = new Set(["entregado_pendiente_facturacion", "entregado_no_facturable", "facturado", "cancelado"]);
const EDITABLE_STATUSES = new Set(["pendiente_armado", "armado_pendiente_entrega"]);
const REMITO_LOCATIONS = new Set(["recepcion", "oficina"]);
const PREPARE_PARTIDAS_ERROR_CODES = new Set([
  "DELIVERY_ORDER_PARTIDAS_REQUIRED",
  "DELIVERY_ORDER_PARTIDAS_DUPLICATED",
  "DELIVERY_ORDER_PARTIDAS_QUANTITY_MISMATCH",
]);

function normalizeRemitoLocation(value) {
  const location = String(value || "").trim().toLowerCase();
  return REMITO_LOCATIONS.has(location) ? location : "";
}

function isCancelableOrder(order) {
  return !NON_CANCELABLE_STATUSES.has(order?.status);
}

function isEditableOrder(order) {
  return (
    EDITABLE_STATUSES.has(order?.status) &&
    !order?.remitoNumber &&
    !order?.bejermanRemitoGroupId
  );
}

function isAdminUser(user) {
  return String(user?.rol || "").trim().toLowerCase() === "admin";
}

function isJefeUser(user) {
  return String(user?.rol || "").trim().toLowerCase() === "jefe";
}

function isVentasUser(user) {
  return String(user?.rol || "").trim().toLowerCase() === "ventas";
}


function canEditCommercialFields(user) {
  return ["admin", "ventas", "jefe"].includes(String(user?.rol || "").trim().toLowerCase());
}

function canPrintInvoiceForOrder(user, order) {
  const role = String(user?.rol || "").trim().toLowerCase();
  return Boolean(String(order?.invoiceNumber || "").trim()) && ["admin", "cobranzas", "recepcion"].includes(role);
}

function invoiceDocumentLabel(order) {
  return String(order?.invoiceNumber || order?.orderNumber || "factura").trim() || "factura";
}

function apiErrorCode(err) {
  return err?.data?.code || err?.code || "";
}

function isUrgentOrder(order) {
  return String(order?.priority || "").trim().toLowerCase() === "urgente";
}

function pendingArmadoRowClass(order) {
  if (order?.status !== "pendiente_armado") {
    return "";
  }
  return isUrgentOrder(order) ? URGENT_PENDING_ARMADO_ROW_CLASS : PENDING_ARMADO_ROW_CLASS;
}

function UrgentChip() {
  return (
    <span className="inline-flex whitespace-nowrap rounded-full border border-red-300 bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
      Urgente
    </span>
  );
}

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

function OrderStatusCell({ order }) {
  return (
    <div className="flex flex-col items-start gap-1">
      {isUrgentOrder(order) && <UrgentChip />}
      <StatusChip status={order.status} />
    </div>
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

function formatQuantity(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

function formatMoney(value, currency = "ARS") {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const normalizedCurrency = String(currency || "").toUpperCase() === "USD" ? "USD" : "ARS";
  return number.toLocaleString("es-AR", {
    style: "currency",
    currency: normalizedCurrency,
    maximumFractionDigits: 2,
  });
}

function formatPercent(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "";
  return number.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

export default function DeliveryOrders() {
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [filters, setFilters] = useState({ status: "", q: "", deliveryType: "" });
  const [searchText, setSearchText] = useState("");
  const [orders, setOrders] = useState([]);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [driveSyncing, setDriveSyncing] = useState(false);
  const [error, setError] = useState("");
  const [driveSyncMessage, setDriveSyncMessage] = useState("");
  const [manualInvoicePrint, setManualInvoicePrint] = useState(null);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState(null);
  const [detailOrder, setDetailOrder] = useState(null);
  const [preparingOrder, setPreparingOrder] = useState(null);
  const [partidasOrder, setPartidasOrder] = useState(null);
  const [remitoModalOpen, setRemitoModalOpen] = useState(false);
  const [historyModalOpen, setHistoryModalOpen] = useState(false);
  const [remitoHistory, setRemitoHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const remitoLocationIntentRef = useRef(new Set());
  const handledRouteOrderRef = useRef("");

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
      setError("La orden no tiene una OS vinculada para imprimir.");
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
      setError(err?.message || "No se pudo imprimir la OS.");
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

  useEffect(() => {
    if (!user) return;
    const params = new URLSearchParams(location.search);
    const orderId = String(params.get("orderId") || "").trim();
    if (!orderId || handledRouteOrderRef.current === orderId) return;
    handledRouteOrderRef.current = orderId;

    getDeliveryOrder(orderId)
      .then((order) => {
        if (!order?.id) throw new Error("No se encontró la orden de entrega.");
        setError("");
        if (canPrepare && order.status === "pendiente_armado") {
          setPreparingOrder(order);
        } else {
          setDetailOrder(order);
        }
      })
      .catch((err) => setError(err?.message || "No se pudo abrir la orden de entrega."))
      .finally(() => {
        params.delete("orderId");
        const nextSearch = params.toString();
        navigate(
          {
            pathname: location.pathname,
            search: nextSearch ? `?${nextSearch}` : "",
          },
          { replace: true }
        );
      });
  }, [canPrepare, location.pathname, location.search, navigate, user]);

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

  const handleDriveSync = async () => {
    setDriveSyncing(true);
    setError("");
    setDriveSyncMessage("");
    try {
      const data = await postDeliveryOrderDriveSync();
      const created = Number(data?.createdRows ?? data?.appendedRows ?? 0);
      const existing = Number(data?.alreadyInDrive ?? data?.skippedExisting ?? 0);
      const range = data?.range ? ` (${data.range})` : "";
      setDriveSyncMessage(`Drive sincronizado: ${created} filas agregadas, ${existing} ya existían.${range}`);
    } catch (err) {
      setError(err?.message || "No se pudo sincronizar con Drive.");
    } finally {
      setDriveSyncing(false);
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

  const handleCancelOrder = (order) => {
    if (!order?.id) return;
    const orderLabel = order.orderNumber ? ` ${order.orderNumber}` : "";
    if (!window.confirm(`¿Seguro que querés cancelar la orden de entrega${orderLabel}?`)) return;
    runAction(() => postDeliveryOrderCancel(order.id));
  };

  const handlePrepareOrder = async (order) => {
    if (!order?.id) return;
    setSaving(true);
    try {
      await postDeliveryOrderPrepared(order.id);
      setError("");
      load();
    } catch (err) {
      if (err?.status === 409 && PREPARE_PARTIDAS_ERROR_CODES.has(apiErrorCode(err))) {
        setPreparingOrder(order);
        setError("");
      } else {
        setError(err?.message || "No se pudo completar la acción.");
      }
    } finally {
      setSaving(false);
    }
  };

  const openInvoiceBlob = (blob, order) =>
    openPrintablePdf(blob, {
      title: `Factura ${invoiceDocumentLabel(order)}`,
      documentLabel: `Factura ${invoiceDocumentLabel(order)}`,
    });

  const handlePrintInvoice = async (order) => {
    if (!order?.id) return;
    setSaving(true);
    setManualInvoicePrint(null);
    try {
      const label = invoiceDocumentLabel(order);
      const blob = await waitForPdfBlob(deliveryOrderInvoicePdfUrl(order.id), {
        label: `factura ${label}`,
      });
      const result = openInvoiceBlob(blob, order);
      if (result.opened) {
        setError("");
      } else {
        setManualInvoicePrint({ blob, order });
        setError(`La factura ${label} está lista, pero el navegador bloqueó la ventana automática.`);
      }
    } catch (err) {
      setError(err?.message || "No se pudo imprimir la factura.");
    } finally {
      setSaving(false);
    }
  };

  const openManualInvoicePrint = () => {
    if (!manualInvoicePrint?.blob) return;
    const result = openInvoiceBlob(manualInvoicePrint.blob, manualInvoicePrint.order);
    if (result.opened) {
      setManualInvoicePrint(null);
      setError("");
      return;
    }
    setError("El navegador sigue bloqueando la ventana de impresión de la factura.");
  };

  const savePreparationPartidas = async (itemsPayload) => {
    if (!preparingOrder?.id) return null;
    let saved = preparingOrder;
    for (const item of itemsPayload || []) {
      saved = await patchDeliveryOrderItemPartidas(preparingOrder.id, item.itemId, item.partidas || []);
    }
    saved = await postDeliveryOrderPrepared(preparingOrder.id);
    return saved;
  };

  const saveDetailPartidas = async (itemsPayload) => {
    if (!partidasOrder?.id) return null;
    let saved = partidasOrder;
    for (const item of itemsPayload || []) {
      saved = await patchDeliveryOrderItemPartidas(partidasOrder.id, item.itemId, item.partidas || []);
    }
    return saved;
  };

  const openEditOrder = (order) => {
    setDetailOrder(null);
    setEditingOrder(order);
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

  const markRemitoLocationIntent = (orderId) => {
    if (orderId) remitoLocationIntentRef.current.add(orderId);
  };

  const handleRemitoLocationChange = (order) => (event) => {
    const orderId = order?.id;
    const nextLocation = normalizeRemitoLocation(event.target.value);
    const currentLocation = normalizeRemitoLocation(order?.remitoLocation);
    const hasOperatorIntent = remitoLocationIntentRef.current.has(orderId);
    remitoLocationIntentRef.current.delete(orderId);

    if (!hasOperatorIntent || !nextLocation || nextLocation === currentLocation) {
      event.target.value = currentLocation;
      return;
    }

    runAction(() =>
      patchDeliveryOrderRemitoLocation(orderId, { remitoLocation: nextLocation })
    );
  };

  const renderOrderNumber = (order) => {
    const ingresoId = exitRemitoIngresoId(order);
    return (
      <div>
        <OrderNumberCell value={order.orderNumber} />
        {ingresoId && (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              openExitRemito(order);
            }}
            disabled={saving}
            className="mt-1 inline-flex items-center gap-1 text-xs text-blue-700 hover:underline disabled:opacity-50"
          >
            <Printer className="h-3.5 w-3.5" aria-hidden="true" />
            Imprimir OS
          </button>
        )}
      </div>
    );
  };

  const renderOrderActions = (order) => (
    <div className="flex flex-wrap gap-1" onClick={(event) => event.stopPropagation()}>
      {canPrepare && order.status === "pendiente_armado" && (
        <button
          type="button"
          onClick={() => handlePrepareOrder(order)}
          className="rounded border px-2 py-1 text-xs hover:bg-gray-50"
        >
          Preparado
        </button>
      )}
      {canDeliver && !NON_CANCELABLE_STATUSES.has(order.status) && !order.remitoNumber && (
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
      {canPrintInvoiceForOrder(user, order) && (
        <button
          type="button"
          onClick={() => handlePrintInvoice(order)}
          disabled={saving}
          className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
        >
          <Printer className="h-3.5 w-3.5" aria-hidden="true" />
          Imprimir factura
        </button>
      )}
      {canCancel && isCancelableOrder(order) && (
        <button
          type="button"
          onClick={() => handleCancelOrder(order)}
          className="rounded border px-2 py-1 text-xs text-red-700 hover:bg-red-50"
        >
          Cancelar
        </button>
      )}
    </div>
  );

  const renderOrderRemito = (order) => {
    const printUrl = remitoPrintUrl(order);
    const groupSummary = remitoGroupSummary(order);
    const groupLabels = (groupSummary?.labels || []).slice(0, 4).join(", ");
    return (
      <div onClick={(event) => event.stopPropagation()}>
        <div>{order.remitoNumber || "-"}</div>
        {groupSummary?.count > 1 && (
          <div className="mt-1 rounded border border-blue-100 bg-blue-50 px-2 py-1 text-[11px] text-blue-800">
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
        {order.remitoNumber && canMoveRemito && (
          <select
            value={normalizeRemitoLocation(order.remitoLocation)}
            onPointerDown={() => markRemitoLocationIntent(order.id)}
            onKeyDown={() => markRemitoLocationIntent(order.id)}
            onChange={handleRemitoLocationChange(order)}
            className="mt-1 h-8 w-full rounded border px-2 text-xs"
          >
            <option value="" disabled>Ubicación</option>
            <option value="recepcion">Recepción</option>
            <option value="oficina">Oficina</option>
          </select>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Órdenes de entrega</h1>
          <p className="text-sm text-gray-600">Artículos, preparación, remitos y cierre administrativo.</p>
        </div>
        <ResponsiveActionBar>
          <button
            type="button"
            onClick={openHistoryModal}
            className={`inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50 ${fullWidthButtonClass}`}
          >
            <Printer className="h-4 w-4" aria-hidden="true" />
            Historial remitos
          </button>
          {(isJefeUser(user) || isVentasUser(user) || isAdminUser(user)) && (
            <button
              type="button"
              onClick={handleDriveSync}
              disabled={driveSyncing}
              className={`inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50 ${fullWidthButtonClass}`}
            >
              <CloudUpload className="h-4 w-4" aria-hidden="true" />
              {driveSyncing ? "Sincronizando..." : "Sincronizar con Drive"}
            </button>
          )}
          {canCreate && (
            <button
              type="button"
              onClick={() => setCreateModalOpen(true)}
              className={`inline-flex items-center gap-2 rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 ${fullWidthButtonClass}`}
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
              className={`rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50 ${fullWidthButtonClass}`}
            >
              Generar remito Bejerman
            </button>
          )}
        </ResponsiveActionBar>
      </div>

      <div className="grid gap-2 border p-3 sm:grid-cols-2 lg:grid-cols-[150px_150px_minmax(220px,1fr)_auto] lg:items-end">
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Estado</span>
          <select
            value={filters.status}
            onChange={updateFilter("status")}
            className="h-9 w-full rounded border px-2"
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
            className="h-9 w-full rounded border px-2"
          >
            <option value="">Todos</option>
            {Object.entries(TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm sm:col-span-2 lg:col-span-1">
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
        <button type="button" onClick={applySearch} className="h-9 rounded border px-3 text-sm hover:bg-gray-50 sm:col-span-2 lg:col-span-1">
          Aplicar
        </button>
      </div>

      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {driveSyncMessage && (
        <div className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {driveSyncMessage}
        </div>
      )}
      {manualInvoicePrint?.blob && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <button
            type="button"
            onClick={openManualInvoicePrint}
            className="inline-flex items-center gap-2 rounded border border-amber-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-amber-100"
          >
            <Printer className="h-4 w-4" aria-hidden="true" />
            Abrir e imprimir factura
          </button>
        </div>
      )}

      <MobileDataList>
        {loading && !orders.length && <MobileDataCard className="text-center text-gray-500">Cargando...</MobileDataCard>}
        {orders.map((order) => {
            const articles = deliveryOrderItemsSummary(order);
            return (
              <MobileDataCard
                key={order.id}
                className={`${pendingArmadoRowClass(order)} cursor-pointer`}
                onClick={() => setDetailOrder(order)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    {renderOrderNumber(order)}
                    <div className="mt-1 break-words text-sm text-gray-700">{order.customerName || "-"}</div>
                  </div>
                  <input
                    type="checkbox"
                    checked={selected.includes(order.id)}
                    onClick={(event) => event.stopPropagation()}
                    onChange={() => toggleSelected(order.id)}
                    disabled={order.remitoNumber || NON_CANCELABLE_STATUSES.has(order.status)}
                    className="h-5 w-5 shrink-0 cursor-pointer rounded border-gray-300 accent-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
                    aria-label={`Seleccionar ${order.orderNumber || "orden"}`}
                  />
                </div>
                <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                  <MobileDataField label="Tipo" value={TYPE_LABELS[order.deliveryType] || order.deliveryType} />
                  <MobileDataField label="Empresa" value={deliveryOrderCompanyLabel(order)} />
                  <MobileDataField label="Estado">
                    <OrderStatusCell order={order} />
                  </MobileDataField>
                  <MobileDataField label="Artículos" className="min-[420px]:col-span-2">
                    <div className="font-medium">{articles.primary}</div>
                    {articles.secondary && <div className="text-xs text-gray-500">{articles.secondary}</div>}
                  </MobileDataField>
                  <MobileDataField label="Comercial" value={deliveryOrderCommercialLabel(order) || "-"} />
                  <MobileDataField label="Factura" value={order.invoiceNumber || "-"} />
                  <MobileDataField label="Remito" className="min-[420px]:col-span-2">
                    {renderOrderRemito(order)}
                  </MobileDataField>
                </div>
                <div className="mt-3 border-t border-gray-100 pt-2">{renderOrderActions(order)}</div>
              </MobileDataCard>
            );
        })}
        {!loading && !orders.length && <MobileDataCard className="text-center text-gray-500">Sin órdenes.</MobileDataCard>}
      </MobileDataList>

      <DesktopTableWrap className="border">
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
            {loading && !orders.length ? (
              <tr>
                <td colSpan={10} className="px-3 py-8 text-center text-gray-500">
                  Cargando...
                </td>
              </tr>
            ) : (
              orders.map((order) => {
                return (
                <tr
                  key={order.id}
                  onClick={() => setDetailOrder(order)}
                  className={`cursor-pointer border-t align-top hover:bg-gray-50 ${pendingArmadoRowClass(order)}`}
                >
                  <td className="px-3 py-2" onClick={(event) => event.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selected.includes(order.id)}
                      onChange={() => toggleSelected(order.id)}
                      disabled={order.remitoNumber || NON_CANCELABLE_STATUSES.has(order.status)}
                      className="h-5 w-5 cursor-pointer rounded border-gray-300 align-middle accent-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
                    />
                  </td>
                  <td className="w-[116px] px-2 py-2">
                    {renderOrderNumber(order)}
                  </td>
                  <td className="px-2 py-2">{order.customerName || "-"}</td>
                  <td className="px-2 py-2">
                    <div>{TYPE_LABELS[order.deliveryType] || order.deliveryType}</div>
                    <div className="mt-0.5 text-xs text-gray-500">{deliveryOrderCompanyLabel(order)}</div>
                  </td>
                  <td className="px-2 py-2">
                    <OrderStatusCell order={order} />
                  </td>
                  <td className="px-2 py-2"><ArticlesCell order={order} /></td>
                  <td className="px-2 py-2 text-xs text-gray-600">{deliveryOrderCommercialLabel(order) || "-"}</td>
                  <td className="px-2 py-2">
                    {renderOrderRemito(order)}
                  </td>
                  <td className="px-2 py-2">{order.invoiceNumber || "-"}</td>
                  <td className="px-2 py-2">
                    {renderOrderActions(order)}
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
      </DesktopTableWrap>
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
        onGenerated={(_result, options = {}) => {
          if (options.keepOpen) return;
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
      <DeliveryOrderDetailModal
        open={Boolean(detailOrder)}
        order={detailOrder}
        canEdit={canEditCommercialFields(user) && isEditableOrder(detailOrder)}
        canEditPartidas={canAssignArticles && isEditableOrder(detailOrder)}
        canPrintInvoice={canPrintInvoiceForOrder(user, detailOrder)}
        onEdit={() => openEditOrder(detailOrder)}
        onEditPartidas={() => setPartidasOrder(detailOrder)}
        onPrintInvoice={() => handlePrintInvoice(detailOrder)}
        onClose={() => setDetailOrder(null)}
      />
      <NewDeliveryOrderModal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        formProps={{ canEditItemDiscounts: isVentasUser(user) || isJefeUser(user) }}
        onCreated={() => {
          setError("");
          load();
        }}
      />
      <NewDeliveryOrderModal
        open={Boolean(editingOrder)}
        order={editingOrder}
        onClose={() => setEditingOrder(null)}
        formProps={{ canEditItemDiscounts: isVentasUser(user) || isJefeUser(user) }}
        onCreated={() => {
          setError("");
          setEditingOrder(null);
          load();
        }}
      />
      <NewDeliveryOrderModal
        open={Boolean(preparingOrder)}
        order={preparingOrder}
        title="Completar partidas"
        description="Complete las partidas faltantes para marcar la orden como preparada. Los datos comerciales y los artículos no se editan en este paso."
        submitLabel="Guardar partidas y preparar"
        formProps={{
          readOnlyHeader: true,
          readOnlyItems: true,
          partidasRequired: true,
          onPartidasSubmit: savePreparationPartidas,
        }}
        onClose={() => setPreparingOrder(null)}
        onCreated={() => {
          setError("");
          setPreparingOrder(null);
          load();
        }}
      />
      <NewDeliveryOrderModal
        open={Boolean(partidasOrder)}
        order={partidasOrder}
        title="Cargar partidas"
        description="Registre las partidas que Recepción tomó del depósito. La orden no se marca como preparada en este paso."
        submitLabel="Guardar partidas"
        formProps={{
          readOnlyHeader: true,
          readOnlyItems: true,
          partidasOpenByDefault: true,
          partidasSubmitRequiresAll: false,
          onPartidasSubmit: saveDetailPartidas,
        }}
        onClose={() => setPartidasOrder(null)}
        onCreated={(saved) => {
          setError("");
          setPartidasOrder(null);
          if (saved?.id) setDetailOrder(saved);
          load();
        }}
      />
    </div>
  );
}

function DetailField({ label, value, children, className = "" }) {
  return (
    <div className={className}>
      <div className="text-[11px] font-semibold uppercase text-gray-500">{label}</div>
      <div className="mt-0.5 break-words text-sm text-gray-900">{children ?? value ?? "-"}</div>
    </div>
  );
}

function DeliveryOrderDetailModal({
  open,
  order,
  canEdit,
  canEditPartidas,
  canPrintInvoice,
  onEdit,
  onEditPartidas,
  onPrintInvoice,
  onClose,
}) {
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open || !order) return null;

  const items = Array.isArray(order.items) ? order.items : [];
  const currency = deliveryOrderPriceCurrency(order);
  const timeline = [
    ["Creada", order.createdAt],
    ["Preparada", order.preparedAt],
    ["Entregada", order.deliveredAt],
    ["Facturada", order.invoicedAt],
    ["Cancelada", order.cancelledAt],
  ].filter(([, value]) => value);

  return (
    <ResponsiveModalOverlay role="dialog" aria-modal="true" onClick={onClose}>
      <ResponsiveModalPanel
        className="max-w-6xl overflow-hidden"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3 sm:px-5 sm:py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">{order.orderNumber || "Orden de entrega"}</h2>
            <p className="text-sm text-gray-600">{order.customerName || "-"}</p>
          </div>
          <div className="flex items-center gap-2">
            {canPrintInvoice && (
              <button
                type="button"
                onClick={onPrintInvoice}
                className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50"
              >
                <Printer className="h-4 w-4" aria-hidden="true" />
                Imprimir factura
              </button>
            )}
            {canEdit && (
              <button
                type="button"
                onClick={onEdit}
                className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm hover:bg-gray-50"
              >
                <Pencil className="h-4 w-4" aria-hidden="true" />
                Editar orden
              </button>
            )}
            <button type="button" className="text-sm text-gray-500 hover:text-gray-900" onClick={onClose}>
              Cerrar
            </button>
          </div>
        </div>

        <div className="max-h-[calc(100dvh-5rem)] overflow-auto p-3 sm:p-5">
          <div className="grid grid-cols-1 gap-3 rounded border bg-gray-50 p-3 sm:grid-cols-2 lg:grid-cols-4">
            <DetailField label="Cliente" value={order.customerName || "-"} className="lg:col-span-2" />
            <DetailField label="Cliente Bejerman" value={order.bejermanCustomerCode || "-"} />
          <DetailField label="Estado">
              <OrderStatusCell order={order} />
          </DetailField>
            <DetailField label="Concepto" value={TYPE_LABELS[order.deliveryType] || order.deliveryType || "-"} />
            <DetailField label="Empresa" value={deliveryOrderCompanyLabel(order)} />
            <DetailField label="Vendedor" value={order.sellerName || order.sellerCode || "-"} />
            <DetailField label="Fecha" value={order.orderDate || "-"} />
            <DetailField label="Condición" value={order.commercialCondition || "-"} />
            <DetailField label="Moneda" value={currency === "USD" ? "U$S" : "$"} />
            <DetailField label="TC" value={order.commercialExchangeRate || "-"} />
            <DetailField label="Prioridad" value={order.priority === "urgente" ? "Urgente" : "Normal"} />
            <DetailField label="Remito" value={order.remitoNumber || "-"} />
            <DetailField label="Factura" value={order.invoiceNumber || "-"} />
          </div>

          <div className="mt-4">
            <h3 className="text-sm font-semibold text-gray-900">Artículos</h3>
            <div className="mt-2 overflow-hidden rounded border">
              <table className="min-w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-3 py-2 font-medium">Código</th>
                    <th className="px-3 py-2 font-medium">Detalle</th>
                    <th className="px-3 py-2 font-medium">Cantidad</th>
                    <th className="px-3 py-2 font-medium">Precio</th>
                    <th className="px-3 py-2 font-medium">Partidas</th>
                  </tr>
                </thead>
                <tbody className="divide-y bg-white">
                  {items.map((item) => {
                    const itemCurrency = deliveryOrderItemPriceCurrency(item, order);
                    const partidas = Array.isArray(item.partidas) && item.partidas.length
                      ? item.partidas
                      : item.partida
                        ? [{ partida: item.partida, assignedQuantity: item.quantity, partidaExpirationDate: item.partidaExpirationDate }]
                        : [];
                    return (
                      <tr key={item.id || `${item.articleCode}-${item.description}`} className="align-top">
                        <td className="px-3 py-2 font-mono text-xs text-sky-700">{item.articleCode || "-"}</td>
                        <td className="px-3 py-2">
                          <div className="font-medium text-gray-900">{item.articleName || item.description || "-"}</div>
                          {item.description && item.description !== item.articleName && (
                            <div className="mt-0.5 text-xs text-gray-500">{item.description}</div>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">{formatQuantity(item.quantity)}</td>
                        <td className="whitespace-nowrap px-3 py-2">
                          <div>{formatMoney(item.unitPrice, itemCurrency)}</div>
                          {Number(item.discountPercent || 0) > 0 && (
                            <div className="mt-0.5 text-xs text-emerald-700">
                              Desc. {formatPercent(item.discountPercent)}% - Neto {formatMoney(item.netSubtotal, itemCurrency)}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          {partidas.length ? (
                            <div className="space-y-1">
                              {partidas.map((partida, partidaIndex) => (
                                <div key={`${partida.partida}-${partidaIndex}`} className="text-xs text-gray-700">
                                  <span className="font-mono font-semibold">{partida.partida || "-"}</span>
                                  <span> · Cant. {formatQuantity(partida.assignedQuantity)}</span>
                                  {partida.partidaExpirationDate && <span> · Vto. {partida.partidaExpirationDate}</span>}
                                  {partida.stockDepositCode && <span> · Dep. {partida.stockDepositCode}</span>}
                                </div>
                              ))}
                              {canEditPartidas && (
                                <button
                                  type="button"
                                  onClick={onEditPartidas}
                                  className="mt-1 rounded border px-2 py-1 text-xs text-sky-800 hover:bg-sky-50"
                                >
                                  Editar partidas
                                </button>
                              )}
                            </div>
                          ) : (
                            <div className="space-y-1">
                              <span className="block text-xs text-amber-700">Sin partidas indicadas</span>
                              {canEditPartidas && (
                                <button
                                  type="button"
                                  onClick={onEditPartidas}
                                  className="rounded border px-2 py-1 text-xs text-sky-800 hover:bg-sky-50"
                                >
                                  Cargar partidas
                                </button>
                              )}
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                  {!items.length && (
                    <tr>
                      <td colSpan={5} className="px-3 py-8 text-center text-gray-500">
                        Sin artículos.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-2">
            <DetailField label="Detalle completo">
              <div className="whitespace-pre-wrap rounded border bg-white p-3 text-sm text-gray-800">{order.rawPedido || "-"}</div>
            </DetailField>
            <DetailField label="Eventos">
              <div className="rounded border bg-white p-3 text-sm text-gray-800">
                {timeline.length ? (
                  <div className="space-y-1">
                    {timeline.map(([label, value]) => (
                      <div key={label} className="flex justify-between gap-3">
                        <span className="text-gray-600">{label}</span>
                        <span className="text-right">{formatDateTime(value)}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  "-"
                )}
              </div>
            </DetailField>
          </div>
        </div>
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
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
    <ResponsiveModalOverlay role="dialog" aria-modal="true" onClick={onClose}>
      <ResponsiveModalPanel
        className="max-w-5xl overflow-hidden"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3 sm:px-5 sm:py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Historial de remitos Bejerman</h2>
            <p className="text-sm text-gray-600">Últimos remitos emitidos y órdenes agrupadas.</p>
          </div>
          <button type="button" className="rounded p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900" onClick={onClose} aria-label="Cerrar">
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>

        <div className="max-h-[calc(100dvh-5rem)] overflow-auto">
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
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
  );
}

function NewDeliveryOrderModal({
  open,
  order = null,
  onClose,
  onCreated,
  title = "",
  description = "",
  submitLabel = "",
  formProps = {},
}) {
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
    <ResponsiveModalOverlay role="dialog" aria-modal="true">
      <ResponsiveModalPanel
        className="max-w-6xl overflow-hidden"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3 sm:px-5 sm:py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">{title || (order?.id ? "Editar orden de entrega" : "Nueva orden de entrega")}</h2>
            <p className="text-sm text-gray-600">{description || "Clientes, artículos, partidas y detalle completo como en Portal."}</p>
          </div>
          <button type="button" className="text-sm text-gray-500 hover:text-gray-900" onClick={onClose}>
            Cerrar
          </button>
        </div>

        <div className="max-h-[calc(100dvh-5rem)] overflow-auto p-3 sm:p-5">
          <DeliveryOrderCreateForm
            compact
            initialOrder={order}
            submitLabel={submitLabel || (order?.id ? "Guardar cambios" : "Crear entrega")}
            onCancel={onClose}
            onCreated={(created) => {
              if (onCreated) onCreated(created);
              onClose();
            }}
            {...formProps}
          />
        </div>
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
  );
}
