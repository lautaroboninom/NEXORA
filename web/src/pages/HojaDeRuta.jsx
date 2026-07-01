import { useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  CalendarDays,
  CheckCircle2,
  ExternalLink,
  MapPinned,
  Navigation,
  PauseCircle,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Search,
  Trash2,
  X,
} from "lucide-react";
import {
  getDeliveryOrder,
  getRouteLocations,
  getRouteSheet,
  getRouteSuggestedDeliveryOrders,
  patchRouteLocation,
  patchRouteStop,
  postRouteLocation,
  postRouteStop,
  postRouteStopCancel,
  postRouteStopComplete,
  postRouteStopPostpone,
  postRouteStopsReorder,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { useLocation, useNavigate } from "react-router-dom";

const STATUS_LABELS = {
  pendiente: "Pendiente",
  completado: "Completado",
  pospuesto: "Pospuesto",
  cancelado: "Cancelado",
};

const STATUS_CLASSES = {
  pendiente: "border-amber-200 bg-amber-50 text-amber-800",
  completado: "border-emerald-200 bg-emerald-50 text-emerald-800",
  pospuesto: "border-sky-200 bg-sky-50 text-sky-800",
  cancelado: "border-gray-200 bg-gray-100 text-gray-600",
};

const DESTINATION_META = {
  customer: { label: "Cliente", cls: "border-indigo-200 bg-indigo-50 text-indigo-800" },
  customer_location: { label: "Cliente", cls: "border-indigo-200 bg-indigo-50 text-indigo-800" },
  route_location: { label: "Frecuente", cls: "border-sky-200 bg-sky-50 text-sky-800" },
  manual: { label: "Manual", cls: "border-gray-200 bg-gray-50 text-gray-700" },
};

const ADDRESS_BOOK_LIMIT = 300;

const todayIso = () => new Date().toISOString().slice(0, 10);

const addDaysIso = (value, days = 1) => {
  const parts = clean(value).split("-").map((part) => Number(part));
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return todayIso();
  const date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + days));
  return date.toISOString().slice(0, 10);
};

const emptyForm = (routeDate = todayIso()) => ({
  id: "",
  routeDate,
  requesterName: "",
  timeWindow: "",
  locationId: "",
  customerId: "",
  locationSourceType: "",
  placeName: "",
  address: "",
  task: "",
  deliveryOrderId: "",
});

const clean = (value) => String(value || "").trim();

const mapQuery = (stop) => clean([stop?.address, stop?.placeName].filter(Boolean).join(" "));

const destinationType = (item) => item?.sourceType || item?.locationSourceType || (item?.customerId || item?.locationCustomerId ? "customer_location" : "route_location");

const destinationMeta = (item) => DESTINATION_META[destinationType(item)] || DESTINATION_META.route_location;

const destinationKey = (item, index = 0) =>
  item?.id ? `location-${item.id}` : `${destinationType(item)}-${item?.customerId || "sin-cliente"}-${item?.name || item?.placeName || ""}-${item?.address || ""}-${index}`;

const googleMapsUrl = (stop) =>
  `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(mapQuery(stop))}`;

const wazeUrl = (stop) =>
  `https://waze.com/ul?q=${encodeURIComponent(mapQuery(stop))}&navigate=yes`;

const mapsEmbedUrl = (stop) =>
  `https://maps.google.com/maps?q=${encodeURIComponent(mapQuery(stop))}&output=embed`;

function IconButton({ title, children, className = "", ...props }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      className={`inline-flex h-9 w-9 items-center justify-center rounded border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 disabled:opacity-40 ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

function CompleteButton({ onClick, disabled }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="col-span-2 inline-flex h-10 w-full min-w-0 items-center justify-center gap-2 rounded border border-emerald-300 bg-emerald-50 px-3 text-sm font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-40 sm:col-span-1 sm:h-9 sm:w-auto sm:min-w-[7rem]"
    >
      <CheckCircle2 size={17} />
      Entregado
    </button>
  );
}

function StatusPill({ status }) {
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${STATUS_CLASSES[status] || STATUS_CLASSES.pendiente}`}>
      {STATUS_LABELS[status] || status || "Pendiente"}
    </span>
  );
}

function StopCard({
  stop,
  selected,
  canManage,
  canComplete,
  canPostpone,
  onSelect,
  onEdit,
  onCancel,
  onComplete,
  onPostpone,
  onMove,
  isFirst,
  isLast,
  saving,
}) {
  const query = mapQuery(stop);
  const order = stop.deliveryOrder || null;
  const closed = ["completado", "cancelado"].includes(stop.status);
  const source = stop.locationSourceType ? destinationMeta(stop) : null;
  return (
    <article
      className={`rounded-md border bg-white p-3 shadow-sm transition ${selected ? "border-blue-500 ring-2 ring-blue-100" : "border-gray-200"}`}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <button type="button" onClick={onSelect} className="min-w-0 w-full text-left sm:flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-words font-medium text-gray-900">{stop.placeName || order?.customerName || "Sin lugar"}</span>
            <StatusPill status={stop.status} />
            {source && <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${source.cls}`}>{source.label}</span>}
          </div>
          <div className="mt-1 whitespace-pre-wrap break-words text-sm text-gray-700">{stop.task || "-"}</div>
          <div className="mt-1 text-xs text-gray-500">
            {stop.timeWindow || "-"} {stop.requesterName ? `· ${stop.requesterName}` : ""}
          </div>
          {order && (
            <div className="mt-2 inline-flex flex-wrap gap-1 rounded border border-indigo-100 bg-indigo-50 px-2 py-1 text-xs text-indigo-800">
              <span>{order.sourceReference || order.orderNumber}</span>
              <span>{order.remitoNumber || "Sin remito"}</span>
            </div>
          )}
          {stop.address && <div className="mt-2 whitespace-pre-wrap break-words text-sm text-gray-600">{stop.address}</div>}
          {stop.postponeNote && <div className="mt-2 text-xs text-sky-700">Pospuesto: {stop.postponeNote}</div>}
        </button>
        <div className="grid grid-cols-4 gap-1 sm:flex sm:shrink-0 sm:flex-wrap sm:justify-end">
          {canManage && (
            <>
              <IconButton title="Subir" onClick={() => onMove(stop, -1)} disabled={saving || isFirst}>
                <ArrowUp size={16} />
              </IconButton>
              <IconButton title="Bajar" onClick={() => onMove(stop, 1)} disabled={saving || isLast}>
                <ArrowDown size={16} />
              </IconButton>
              <IconButton title="Editar" onClick={() => onEdit(stop)} disabled={saving || closed}>
                <Pencil size={16} />
              </IconButton>
              <IconButton title="Cancelar" onClick={() => onCancel(stop)} disabled={saving || stop.status === "completado" || stop.status === "cancelado"}>
                <Trash2 size={16} />
              </IconButton>
            </>
          )}
          {canComplete && (
            <CompleteButton onClick={() => onComplete(stop)} disabled={saving || stop.status === "completado" || stop.status === "cancelado"} />
          )}
          {canPostpone && (
            <IconButton title="Posponer" onClick={() => onPostpone(stop)} disabled={saving || stop.status === "completado" || stop.status === "cancelado"} className="text-sky-700">
              <PauseCircle size={17} />
            </IconButton>
          )}
          {query && (
            <>
              <a
                href={googleMapsUrl(stop)}
                target="_blank"
                rel="noreferrer"
                title="Google Maps"
                aria-label="Google Maps"
                className="inline-flex h-9 w-9 items-center justify-center rounded border border-gray-300 bg-white text-gray-700 hover:bg-gray-50"
              >
                <MapPinned size={16} />
              </a>
              <a
                href={wazeUrl(stop)}
                target="_blank"
                rel="noreferrer"
                title="Waze"
                aria-label="Waze"
                className="inline-flex h-9 w-9 items-center justify-center rounded border border-gray-300 bg-white text-gray-700 hover:bg-gray-50"
              >
                <Navigation size={16} />
              </a>
            </>
          )}
        </div>
      </div>
    </article>
  );
}

function PostponeModal({ form, saving, onChange, onClose, onSubmit }) {
  const stop = form.stop;
  if (!form.open || !stop) return null;
  const currentDate = clean(stop.routeDate);
  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-black/30 px-3 py-4 sm:items-center">
      <form onSubmit={onSubmit} className="w-full max-w-sm rounded-md border bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
          <div className="min-w-0">
            <h2 className="font-semibold text-gray-900">Posponer parada</h2>
            <div className="mt-1 truncate text-sm text-gray-500">{stop.placeName || stop.task || "Sin lugar"}</div>
          </div>
          <button type="button" onClick={onClose} className="rounded p-1 text-gray-500 hover:bg-gray-100" aria-label="Cerrar">
            <X size={18} />
          </button>
        </div>
        <div className="space-y-3 px-4 py-4">
          <label className="block text-sm font-medium text-gray-700">
            Nueva fecha
            <span className="mt-1 flex items-center gap-2 rounded border bg-white px-3 py-2">
              <CalendarDays size={16} className="text-gray-400" />
              <input
                type="date"
                value={form.routeDate}
                min={currentDate ? addDaysIso(currentDate, 1) : undefined}
                onChange={(event) => onChange({ routeDate: event.target.value })}
                className="w-full bg-transparent outline-none"
                required
              />
            </span>
          </label>
          <label className="block text-sm font-medium text-gray-700">
            Motivo
            <textarea
              value={form.note}
              onChange={(event) => onChange({ note: event.target.value })}
              rows={3}
              className="mt-1 w-full resize-none rounded border bg-white px-3 py-2 text-sm outline-none focus:border-blue-400"
              placeholder="Opcional"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 border-t px-4 py-3">
          <button type="button" onClick={onClose} className="rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50">
            Cancelar
          </button>
          <button type="submit" disabled={saving} className="rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50">
            Posponer
          </button>
        </div>
      </form>
    </div>
  );
}

function RouteLocationsModal({
  open,
  query,
  onQueryChange,
  form,
  onFormChange,
  items,
  saving,
  onSubmit,
  onClose,
  onReset,
  onEditItem,
  onUseItem,
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-black/30 px-3 py-4 sm:items-center">
      <section className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-md border bg-white shadow-xl">
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
          <div>
            <h2 className="font-semibold text-gray-900">Direcciones frecuentes</h2>
            <div className="mt-0.5 text-sm text-gray-500">{items.length} direcciones guardadas para Hoja de ruta</div>
          </div>
          <button type="button" onClick={onClose} className="rounded p-1 text-gray-500 hover:bg-gray-100" aria-label="Cerrar">
            <X size={18} />
          </button>
        </div>

        <div className="space-y-3 border-b bg-gray-50 p-4">
          <div className="flex items-center rounded border bg-white px-3 py-2">
            <Search size={16} className="mr-2 text-gray-400" />
            <input
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
              placeholder="Buscar lugar o dirección"
              className="w-full bg-transparent text-sm outline-none"
            />
          </div>
          <form onSubmit={onSubmit} className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1.3fr_auto] sm:items-end">
            <input
              value={form.name}
              onChange={(event) => onFormChange((prev) => ({ ...prev, name: event.target.value }))}
              placeholder="Lugar"
              className="rounded border bg-white px-3 py-2 text-sm"
            />
            <input
              value={form.address}
              onChange={(event) => onFormChange((prev) => ({ ...prev, address: event.target.value }))}
              placeholder="Dirección"
              className="rounded border bg-white px-3 py-2 text-sm"
            />
            <div className="flex justify-end gap-2">
              {form.id && (
                <button type="button" onClick={onReset} className="rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50">
                  Cancelar
                </button>
              )}
              <button type="submit" disabled={saving} className="inline-flex items-center gap-2 rounded bg-gray-900 px-3 py-2 text-sm text-white hover:bg-black disabled:opacity-50">
                <Save size={15} />
                {form.id ? "Guardar" : "Agregar"}
              </button>
            </div>
          </form>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {items.length === 0 ? (
            <div className="py-8 text-center text-sm text-gray-500">Sin direcciones frecuentes.</div>
          ) : (
            items.map((item) => (
              <div key={item.id} className="border-t py-3 first:border-t-0">
                <div className="flex items-start justify-between gap-3">
                  <button type="button" onClick={() => onUseItem(item)} className="min-w-0 flex-1 text-left">
                    <span className="block truncate text-sm font-medium text-gray-900">{item.name}</span>
                    <span className="block break-words text-xs text-gray-500">{item.address || "-"}</span>
                  </button>
                  <button type="button" onClick={() => onEditItem(item)} className="rounded border bg-white px-2 py-1 text-xs hover:bg-gray-50">
                    Editar
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}

export default function HojaDeRuta() {
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [routeDate, setRouteDate] = useState(() => new URLSearchParams(window.location.search).get("date") || todayIso());
  const [stops, setStops] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [suggestedOrders, setSuggestedOrders] = useState([]);
  const [locations, setLocations] = useState([]);
  const [locationQuery, setLocationQuery] = useState("");
  const [addressBookQuery, setAddressBookQuery] = useState("");
  const [addressBookItems, setAddressBookItems] = useState([]);
  const [addressBookForm, setAddressBookForm] = useState({ id: "", name: "", address: "", notes: "" });
  const [addressBookSaving, setAddressBookSaving] = useState(false);
  const [addressBookOpen, setAddressBookOpen] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState(emptyForm());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [postponeForm, setPostponeForm] = useState({ open: false, stop: null, routeDate: "", note: "" });

  const canManage = can(user, PERMISSION_CODES.ACTION_ROUTE_SHEET_MANAGE);
  const canComplete = can(user, PERMISSION_CODES.ACTION_ROUTE_SHEET_COMPLETE);
  const canPostpone = can(user, PERMISSION_CODES.ACTION_ROUTE_SHEET_POSTPONE);

  const selectedStop = useMemo(
    () => stops.find((stop) => stop.id === selectedId) || stops[0] || null,
    [selectedId, stops]
  );

  const load = async (date = routeDate) => {
    setLoading(true);
    try {
      const data = await getRouteSheet({ date });
      const items = Array.isArray(data?.items) ? data.items : [];
      setStops(items);
      setSelectedId((prev) => (items.some((item) => item.id === prev) ? prev : items[0]?.id || ""));
      setError("");
    } catch (err) {
      setError(err?.message || "No se pudo cargar la Hoja de ruta.");
    } finally {
      setLoading(false);
    }
  };

  const loadSuggestedOrders = async (date = routeDate) => {
    if (!canManage) return;
    try {
      const data = await getRouteSuggestedDeliveryOrders({ date, limit: 80 });
      setSuggestedOrders(Array.isArray(data?.items) ? data.items : []);
    } catch {
      setSuggestedOrders([]);
    }
  };

  useEffect(() => {
    load();
  }, [routeDate]);

  useEffect(() => {
    loadSuggestedOrders();
  }, [routeDate, canManage]);

  useEffect(() => {
    if (!canManage) return;
    const params = new URLSearchParams(location.search);
    const dateParam = clean(params.get("date"));
    const orderId = clean(params.get("orderId"));
    const routeLocationId = clean(params.get("locationId"));
    const customerId = clean(params.get("customerId"));
    const placeName = clean(params.get("placeName"));
    const address = clean(params.get("address"));
    if (dateParam && dateParam !== routeDate) {
      setRouteDate(dateParam);
    }
    if (!orderId && (routeLocationId || customerId || placeName || address)) {
      setForm((prev) => ({
        ...emptyForm(dateParam || routeDate),
        locationId: routeLocationId,
        customerId,
        locationSourceType: routeLocationId ? "route_location" : customerId ? "customer_location" : "manual",
        placeName,
        address,
        task: "",
      }));
      setLocationQuery(placeName);
      setFormOpen(true);
      setError("");
      for (const key of ["locationId", "customerId", "placeName", "address"]) params.delete(key);
      const next = params.toString();
      navigate({ pathname: location.pathname, search: next ? `?${next}` : "" }, { replace: true });
      return;
    }
    if (!orderId) return;
    getDeliveryOrder(orderId)
      .then((order) => {
        if (order?.status === "pendiente_stock") {
          throw new Error("La orden está pendiente de stock. Pasala a pendiente de armado antes de agregarla a Hoja de ruta.");
        }
        setForm((prev) => ({
          ...emptyForm(dateParam || routeDate),
          deliveryOrderId: order.id,
          placeName: order.customerName || "",
          task: `Entregar ${order.sourceReference || order.orderNumber}${order.rawPedido ? ` - ${order.rawPedido}` : ""}`,
          requesterName: order.sellerName || "",
        }));
        setLocationQuery(order.customerName || "");
        setFormOpen(true);
        setError("");
      })
      .catch((err) => setError(err?.message || "No se pudo cargar la orden de entrega."))
      .finally(() => {
        params.delete("orderId");
        const next = params.toString();
        navigate({ pathname: location.pathname, search: next ? `?${next}` : "" }, { replace: true });
      });
  }, [location.pathname, location.search, navigate, canManage]);

  useEffect(() => {
    if (!canManage || clean(locationQuery).length < 2) {
      setLocations([]);
      return;
    }
    let active = true;
    getRouteLocations({ q: locationQuery, limit: 12 })
      .then((data) => {
        if (active) setLocations(Array.isArray(data?.items) ? data.items : []);
      })
      .catch(() => {
        if (active) setLocations([]);
      });
    return () => {
      active = false;
    };
  }, [locationQuery, canManage]);

  useEffect(() => {
    if (!canManage || !addressBookOpen) return;
    let active = true;
    const timer = window.setTimeout(() => {
      getRouteLocations({ q: addressBookQuery, limit: ADDRESS_BOOK_LIMIT })
        .then((data) => {
          if (!active) return;
          const items = Array.isArray(data?.items) ? data.items : [];
          setAddressBookItems(items.filter((item) => item.id));
        })
        .catch(() => {
          if (active) setAddressBookItems([]);
        });
    }, 200);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [addressBookQuery, canManage, addressBookOpen]);

  const updateForm = (field) => (event) => {
    const value = event?.target ? event.target.value : event;
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  const openCreate = () => {
    setForm(emptyForm(routeDate));
    setLocationQuery("");
    setFormOpen(true);
  };

  const openEdit = (stop) => {
    setForm({
      id: stop.id,
      routeDate: stop.routeDate || routeDate,
      requesterName: stop.requesterName || "",
      timeWindow: stop.timeWindow || "",
      locationId: stop.locationId || "",
      customerId: stop.locationCustomerId || "",
      locationSourceType: stop.locationSourceType || "",
      placeName: stop.placeName || "",
      address: stop.address || "",
      task: stop.task || "",
      deliveryOrderId: stop.deliveryOrderId || "",
    });
    setLocationQuery(stop.placeName || "");
    setFormOpen(true);
  };

  const chooseLocation = (location) => {
    setForm((prev) => ({
      ...prev,
      locationId: location.id || "",
      customerId: location.customerId || "",
      locationSourceType: location.sourceType || "",
      placeName: location.name || "",
      address: location.address || "",
    }));
    setLocationQuery(location.name || "");
    setLocations([]);
  };

  const chooseOrder = (order) => {
    setForm((prev) => ({
      ...prev,
      routeDate: prev.id ? prev.routeDate : routeDate,
      deliveryOrderId: order.id,
      customerId: order.customerId || prev.customerId || "",
      placeName: prev.placeName || order.customerName || "",
      task: prev.task || `Entregar ${order.sourceReference || order.orderNumber}${order.rawPedido ? ` - ${order.rawPedido}` : ""}`,
    }));
    setFormOpen(true);
  };

  const saveForm = async (event) => {
    event.preventDefault();
    setSaving(true);
    try {
      const payload = {
        routeDate: form.routeDate || routeDate,
        requesterName: form.requesterName,
        timeWindow: form.timeWindow,
        locationId: form.locationId || null,
        customerId: form.customerId || null,
        placeName: form.placeName,
        address: form.address,
        task: form.task,
        deliveryOrderId: form.deliveryOrderId || null,
      };
      if (form.id) await patchRouteStop(form.id, payload);
      else await postRouteStop(payload);
      const savedDate = payload.routeDate || routeDate;
      if (savedDate !== routeDate) setRouteDate(savedDate);
      setFormOpen(false);
      setError("");
      await load(savedDate);
      await loadSuggestedOrders(savedDate);
    } catch (err) {
      setError(err?.message || "No se pudo guardar la parada.");
    } finally {
      setSaving(false);
    }
  };

  const completeStop = async (stop) => {
    const payload = {};
    if (stop.deliveryOrderId && !clean(stop.deliveryOrder?.remitoNumber)) {
      const remitoNumber = window.prompt("Número de remito", "");
      if (!clean(remitoNumber)) return;
      payload.remitoNumber = remitoNumber;
    }
    setSaving(true);
    try {
      await postRouteStopComplete(stop.id, payload);
      setError("");
      await load();
      await loadSuggestedOrders();
    } catch (err) {
      setError(err?.message || "No se pudo completar la parada.");
    } finally {
      setSaving(false);
    }
  };

  const openPostpone = (stop) => {
    const currentDate = clean(stop.routeDate) || routeDate;
    setPostponeForm({
      open: true,
      stop,
      routeDate: addDaysIso(currentDate, 1),
      note: stop.postponeNote || "",
    });
    setError("");
  };

  const closePostpone = () => {
    if (saving) return;
    setPostponeForm({ open: false, stop: null, routeDate: "", note: "" });
  };

  const updatePostponeForm = (values) => {
    setPostponeForm((prev) => ({ ...prev, ...values }));
  };

  const submitPostpone = async (event) => {
    event.preventDefault();
    const stop = postponeForm.stop;
    if (!stop) return;
    const targetDate = clean(postponeForm.routeDate);
    const currentDate = clean(stop.routeDate) || routeDate;
    if (!targetDate) {
      setError("Elegí la fecha a la que querés mover la parada.");
      return;
    }
    if (targetDate <= currentDate) {
      setError("La nueva fecha debe ser posterior a la fecha actual.");
      return;
    }
    setSaving(true);
    try {
      await postRouteStopPostpone(stop.id, { routeDate: targetDate, note: postponeForm.note });
      setPostponeForm({ open: false, stop: null, routeDate: "", note: "" });
      setError("");
      await load(routeDate);
      await loadSuggestedOrders(routeDate);
    } catch (err) {
      setError(err?.message || "No se pudo posponer la parada.");
    } finally {
      setSaving(false);
    }
  };

  const cancelStop = async (stop) => {
    if (!window.confirm("¿Seguro que querés cancelar esta parada?")) return;
    setSaving(true);
    try {
      await postRouteStopCancel(stop.id);
      setError("");
      await load();
      await loadSuggestedOrders();
    } catch (err) {
      setError(err?.message || "No se pudo cancelar la parada.");
    } finally {
      setSaving(false);
    }
  };

  const moveStop = async (stop, delta) => {
    const index = stops.findIndex((item) => item.id === stop.id);
    const nextIndex = index + delta;
    if (index < 0 || nextIndex < 0 || nextIndex >= stops.length) return;
    const nextStops = [...stops];
    const [item] = nextStops.splice(index, 1);
    nextStops.splice(nextIndex, 0, item);
    setStops(nextStops);
    setSaving(true);
    try {
      await postRouteStopsReorder({ routeDate, ids: nextStops.map((row) => row.id) });
      setError("");
      await load();
    } catch (err) {
      setError(err?.message || "No se pudo reordenar la Hoja de ruta.");
      await load();
    } finally {
      setSaving(false);
    }
  };

  const editAddressBookItem = (item) => {
    setAddressBookForm({
      id: item.id || "",
      name: item.name || "",
      address: item.address || "",
      notes: item.notes || "",
    });
  };

  const resetAddressBookForm = () => {
    setAddressBookForm({ id: "", name: "", address: "", notes: "" });
  };

  const saveAddressBookItem = async (event) => {
    event.preventDefault();
    if (!clean(addressBookForm.name)) {
      setError("Ingresá el nombre del lugar.");
      return;
    }
    setAddressBookSaving(true);
    try {
      const payload = {
        name: addressBookForm.name,
        address: addressBookForm.address,
        notes: addressBookForm.notes,
      };
      if (addressBookForm.id) await patchRouteLocation(addressBookForm.id, payload);
      else await postRouteLocation(payload);
      resetAddressBookForm();
      setError("");
      const data = await getRouteLocations({ q: addressBookQuery, limit: ADDRESS_BOOK_LIMIT });
      setAddressBookItems((Array.isArray(data?.items) ? data.items : []).filter((item) => item.id));
    } catch (err) {
      setError(err?.message || "No se pudo guardar la dirección frecuente.");
    } finally {
      setAddressBookSaving(false);
    }
  };

  const useAddressBookItem = (item) => {
    chooseLocation(item);
    setFormOpen(true);
    setAddressBookOpen(false);
  };

  const selectedQuery = mapQuery(selectedStop);

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-3 py-4 md:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold text-gray-900">Hoja de ruta</h1>
            {canManage && (
              <button
                type="button"
                onClick={() => setAddressBookOpen(true)}
                className="inline-flex h-8 items-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm text-gray-700 hover:bg-gray-50"
              >
                <MapPinned size={15} />
                Direcciones
              </button>
            )}
          </div>
          <div className="mt-1 text-sm text-gray-500">{stops.length} paradas</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="inline-flex items-center gap-2 rounded border bg-white px-3 py-2 text-sm text-gray-700">
            <CalendarDays size={16} />
            <input
              type="date"
              value={routeDate}
              onChange={(event) => setRouteDate(event.target.value || todayIso())}
              className="bg-transparent outline-none"
            />
          </label>
          <IconButton title="Actualizar" onClick={load} disabled={loading || saving}>
            <RefreshCw size={16} />
          </IconButton>
          {canManage && (
            <button
              type="button"
              onClick={openCreate}
              className="inline-flex items-center gap-2 rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700"
            >
              <Plus size={16} />
              Nueva parada
            </button>
          )}
        </div>
      </div>

      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.05fr)_minmax(360px,0.95fr)]">
        <section className="space-y-3">
          {canManage && formOpen && (
            <form onSubmit={saveForm} className="rounded-md border border-blue-200 bg-blue-50 p-3">
              <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                <input type="date" value={form.routeDate} onChange={updateForm("routeDate")} aria-label="Fecha de agenda" title="Fecha de agenda" className="rounded border bg-white px-3 py-2" />
                <input value={form.requesterName} onChange={updateForm("requesterName")} placeholder="Solicitante" className="rounded border bg-white px-3 py-2" />
                <input value={form.timeWindow} onChange={updateForm("timeWindow")} placeholder="Horario" className="rounded border bg-white px-3 py-2" />
              </div>
              <div className="relative mt-2">
                <div className="flex items-center rounded border bg-white px-3 py-2">
                  <Search size={16} className="mr-2 text-gray-400" />
                  <input
                    value={locationQuery}
                    onChange={(event) => {
                      setLocationQuery(event.target.value);
                      setForm((prev) => ({ ...prev, placeName: event.target.value, locationId: "", customerId: "", locationSourceType: "manual" }));
                    }}
                    placeholder="Buscar cliente, dirección frecuente o lugar libre"
                    className="w-full bg-transparent outline-none"
                  />
                </div>
                {locations.length > 0 && (
                  <div className="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded border bg-white shadow-lg">
                    {locations.map((location, index) => {
                      const meta = destinationMeta(location);
                      return (
                      <button
                        key={destinationKey(location, index)}
                        type="button"
                        onClick={() => chooseLocation(location)}
                        className="block w-full border-b px-3 py-2 text-left text-sm hover:bg-gray-50"
                      >
                        <span className="flex items-center justify-between gap-2">
                          <span className="min-w-0 truncate font-medium text-gray-900">{location.name}</span>
                          <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] ${meta.cls}`}>{meta.label}</span>
                        </span>
                        <span className="block text-xs text-gray-500">{location.address || "-"}</span>
                      </button>
                      );
                    })}
                  </div>
                )}
              </div>
              <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-[1fr_1.4fr]">
                <input value={form.address} onChange={updateForm("address")} placeholder="Dirección" className="rounded border bg-white px-3 py-2" />
                <input value={form.task} onChange={updateForm("task")} placeholder="Tarea" className="rounded border bg-white px-3 py-2" />
              </div>
              {form.deliveryOrderId && (
                <div className="mt-2 flex items-center justify-between rounded border border-indigo-200 bg-white px-3 py-2 text-sm text-indigo-800">
                  <span>OE vinculada</span>
                  <button type="button" onClick={() => setForm((prev) => ({ ...prev, deliveryOrderId: "" }))} className="text-indigo-700 hover:underline">
                    Quitar
                  </button>
                </div>
              )}
              <div className="mt-3 flex justify-end gap-2">
                <button type="button" onClick={() => setFormOpen(false)} className="inline-flex items-center gap-2 rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50">
                  <X size={16} />
                  Cerrar
                </button>
                <button type="submit" disabled={saving} className="inline-flex items-center gap-2 rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50">
                  <Save size={16} />
                  Guardar
                </button>
              </div>
            </form>
          )}

          {loading ? (
            <div className="rounded border bg-white p-6 text-center text-sm text-gray-500">Cargando...</div>
          ) : stops.length === 0 ? (
            <div className="rounded border bg-white p-6 text-center text-sm text-gray-500">Sin paradas para la fecha seleccionada.</div>
          ) : (
            stops.map((stop, index) => (
              <StopCard
                key={stop.id}
                stop={stop}
                selected={selectedStop?.id === stop.id}
                canManage={canManage}
                canComplete={canComplete}
                canPostpone={canPostpone}
                onSelect={() => setSelectedId(stop.id)}
                onEdit={openEdit}
                onCancel={cancelStop}
                onComplete={completeStop}
                onPostpone={openPostpone}
                onMove={moveStop}
                isFirst={index === 0}
                isLast={index === stops.length - 1}
                saving={saving}
              />
            ))
          )}
        </section>

        <aside className="space-y-4">
          <section className="overflow-hidden rounded-md border bg-white">
            <div className="border-b px-3 py-2">
              <div className="font-medium text-gray-900">{selectedStop?.placeName || "Mapa"}</div>
              <div className="mt-0.5 text-sm text-gray-500">{selectedStop?.address || selectedStop?.task || "-"}</div>
            </div>
            <div className="aspect-[4/3] bg-gray-100">
              {selectedQuery ? (
                <iframe
                  title="Mapa de Hoja de ruta"
                  src={mapsEmbedUrl(selectedStop)}
                  className="h-full w-full border-0"
                  loading="lazy"
                  referrerPolicy="no-referrer-when-downgrade"
                />
              ) : (
                <div className="grid h-full place-items-center text-sm text-gray-500">Sin dirección</div>
              )}
            </div>
            {selectedQuery && (
              <div className="flex flex-wrap gap-2 border-t p-3">
                <a href={googleMapsUrl(selectedStop)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50">
                  <ExternalLink size={16} />
                  Google Maps
                </a>
                <a href={wazeUrl(selectedStop)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50">
                  <Navigation size={16} />
                  Waze
                </a>
              </div>
            )}
          </section>

          {false && canManage && (
            <section className="rounded-md border bg-white">
              <div className="border-b px-3 py-2 font-medium text-gray-900">Direcciones frecuentes</div>
              <div className="space-y-3 p-3">
                <div className="flex items-center rounded border bg-white px-3 py-2">
                  <Search size={16} className="mr-2 text-gray-400" />
                  <input
                    value={addressBookQuery}
                    onChange={(event) => setAddressBookQuery(event.target.value)}
                    placeholder="Buscar lugar o dirección"
                    className="w-full bg-transparent text-sm outline-none"
                  />
                </div>
                <form onSubmit={saveAddressBookItem} className="grid grid-cols-1 gap-2">
                  <input
                    value={addressBookForm.name}
                    onChange={(event) => setAddressBookForm((prev) => ({ ...prev, name: event.target.value }))}
                    placeholder="Lugar"
                    className="rounded border bg-white px-3 py-2 text-sm"
                  />
                  <input
                    value={addressBookForm.address}
                    onChange={(event) => setAddressBookForm((prev) => ({ ...prev, address: event.target.value }))}
                    placeholder="Dirección"
                    className="rounded border bg-white px-3 py-2 text-sm"
                  />
                  <div className="flex justify-end gap-2">
                    {addressBookForm.id && (
                      <button type="button" onClick={resetAddressBookForm} className="rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50">
                        Cancelar
                      </button>
                    )}
                    <button type="submit" disabled={addressBookSaving} className="inline-flex items-center gap-2 rounded bg-gray-900 px-3 py-2 text-sm text-white hover:bg-black disabled:opacity-50">
                      <Save size={15} />
                      {addressBookForm.id ? "Guardar" : "Agregar"}
                    </button>
                  </div>
                </form>
                <div className="max-h-64 overflow-y-auto">
                  {addressBookItems.length === 0 ? (
                    <div className="py-4 text-center text-sm text-gray-500">Sin direcciones frecuentes.</div>
                  ) : (
                    addressBookItems.map((item) => (
                      <div key={item.id} className="border-t py-2 first:border-t-0">
                        <div className="flex items-start justify-between gap-2">
                          <button type="button" onClick={() => useAddressBookItem(item)} className="min-w-0 flex-1 text-left">
                            <span className="block truncate text-sm font-medium text-gray-900">{item.name}</span>
                            <span className="block line-clamp-2 text-xs text-gray-500">{item.address || "-"}</span>
                          </button>
                          <button type="button" onClick={() => editAddressBookItem(item)} className="rounded border bg-white px-2 py-1 text-xs hover:bg-gray-50">
                            Editar
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </section>
          )}

          {canManage && (
            <section className="rounded-md border bg-white">
              <div className="border-b px-3 py-2 font-medium text-gray-900">Órdenes para agendar</div>
              <div className="max-h-[420px] overflow-y-auto p-2">
                {suggestedOrders.length === 0 ? (
                  <div className="px-2 py-4 text-center text-sm text-gray-500">Sin órdenes sugeridas.</div>
                ) : (
                  suggestedOrders.map((order) => (
                    <div key={order.id} className="mb-2 rounded border border-gray-200 p-2">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="font-medium text-gray-900">{order.sourceReference || order.orderNumber}</div>
                          <div className="truncate text-sm text-gray-600">{order.customerName}</div>
                          <div className="mt-1 line-clamp-2 text-xs text-gray-500">{order.rawPedido || "-"}</div>
                        </div>
                        <button type="button" onClick={() => chooseOrder(order)} className="shrink-0 rounded border bg-white px-2 py-1 text-sm hover:bg-gray-50">
                          Agregar
                        </button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </section>
          )}
        </aside>
      </div>

      <RouteLocationsModal
        open={addressBookOpen}
        query={addressBookQuery}
        onQueryChange={setAddressBookQuery}
        form={addressBookForm}
        onFormChange={setAddressBookForm}
        items={addressBookItems}
        saving={addressBookSaving}
        onSubmit={saveAddressBookItem}
        onClose={() => setAddressBookOpen(false)}
        onReset={resetAddressBookForm}
        onEditItem={editAddressBookItem}
        onUseItem={useAddressBookItem}
      />

      <PostponeModal
        form={postponeForm}
        saving={saving}
        onChange={updatePostponeForm}
        onClose={closePostpone}
        onSubmit={submitPostpone}
      />
    </div>
  );
}
