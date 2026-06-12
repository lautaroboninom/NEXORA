import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getGlobalSearch, getWorkResumen } from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import BusquedaNSCard from "../components/BusquedaNSCard.jsx";
import BusquedaAccRefCard from "../components/BusquedaAccRefCard.jsx";
import QrScanCard from "../components/QrScanCard.jsx";
import DeliveryOrderCreateForm from "../components/DeliveryOrderCreateForm.jsx";
import WorkQueueTable from "../components/WorkQueueTable.jsx";
import {
  catalogEquipmentLabel,
  deviceIdentifierPartsOf,
  formatDateOnly,
  formatOS,
  ingresoIdOf,
} from "../lib/ui-helpers";
import {
  deliveryOrderCommercialLabel,
  deliveryOrderItemsSummary,
  deliveryOrderSourceLabel,
} from "../lib/delivery-orders";

const severityClasses = {
  critical: "border-red-200 bg-red-50 text-red-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  info: "border-blue-200 bg-blue-50 text-blue-800",
};

const STATUS_LABELS = {
  pendiente_armado: "Pendiente de armado",
  armado_pendiente_entrega: "Listo para entrega",
  entregado_pendiente_facturacion: "Pendiente de facturación",
  facturado: "Facturado",
  cancelado: "Cancelado",
};

const TYPE_LABELS = {
  sale: "Venta",
  service_release: "Servicio técnico",
  rental: "Alquiler",
};

function KpiCard({ item }) {
  const tone = severityClasses[item?.severity] || severityClasses.info;
  return (
    <div className={`rounded border p-3 ${tone}`}>
      <div className="text-xs font-medium uppercase tracking-wide opacity-80">{item?.label}</div>
      <div className="text-2xl font-semibold leading-tight">{item?.value ?? 0}</div>
    </div>
  );
}

function KpiGrid({ items, loading }) {
  const list = Array.isArray(items) ? items : [];
  return (
    <section className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
      {list.map((item) => (
        <KpiCard key={item.key} item={item} />
      ))}
      {loading && !list.length && <div className="text-sm text-gray-500">Cargando indicadores...</div>}
    </section>
  );
}

function ObjectiveRow({ item }) {
  const pct = Math.max(0, Math.min(100, Number(item?.percent || 0)));
  return (
    <div className="border-b py-3 last:border-b-0">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-gray-900">{item?.label}</div>
          <div className="text-xs text-gray-500">
            {item?.scope_type === "technician" ? item?.technician_name || "Técnico" : "Equipo completo"}
          </div>
        </div>
        <div className="text-sm font-semibold text-gray-900">
          {item?.progress ?? 0} / {item?.target_value ?? 0}
        </div>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded bg-gray-100">
        <div
          className={`h-full ${item?.status === "cumplido" ? "bg-emerald-600" : "bg-amber-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function SearchResult({ item, groupKey, onOpen }) {
  const title =
    groupKey === "clientes"
      ? item?.razon_social
      : groupKey === "equipos"
        ? catalogEquipmentLabel(item)
        : `OS ${formatOS(item)}`;
  const identifier = deviceIdentifierPartsOf(item, "");
  const identifierText = identifier.secondary
    ? `${identifier.primary} (${identifier.secondary})`
    : identifier.primary;
  const subtitle =
    groupKey === "clientes"
      ? [item?.cod_empresa, item?.telefono, item?.email].filter(Boolean).join(" - ")
      : [item?.cliente, catalogEquipmentLabel(item), identifierText].filter(Boolean).join(" - ");

  return (
    <button
      type="button"
      className="w-full border-b px-3 py-2 text-left last:border-b-0 hover:bg-gray-50"
      onClick={() => onOpen(item)}
    >
      <div className="text-sm font-medium text-gray-900">{title || "-"}</div>
      <div className="text-xs text-gray-500">{subtitle || "-"}</div>
    </button>
  );
}

function DashboardHeader({
  title,
  subtitle,
  periodo,
  onPeriodoChange,
  onReload,
  showPeriod = true,
  canCreateOrder = false,
  onCreateOrder,
}) {
  return (
    <div className="flex flex-col justify-between gap-3 lg:flex-row lg:items-end">
      <div>
        <h1 className="text-2xl font-semibold text-gray-900">{title}</h1>
        {subtitle && <div className="text-sm text-gray-500">{subtitle}</div>}
      </div>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        {canCreateOrder && (
          <button type="button" className="btn sm:mr-2" onClick={onCreateOrder}>
            Nueva entrega
          </button>
        )}
        {showPeriod && (
          <select
            className="rounded border p-2"
            value={periodo}
            onChange={(event) => onPeriodoChange(event.target.value)}
            aria-label="Período de objetivos"
          >
            <option value="hoy">Hoy</option>
            <option value="semana">Semana</option>
          </select>
        )}
        <button type="button" className="rounded border px-3 py-2 text-sm hover:bg-gray-50" onClick={onReload}>
          Recargar
        </button>
      </div>
    </div>
  );
}

function SearchSection({ search, setSearch, searchData, searching, onOpen }) {
  return (
    <section className="rounded border bg-white p-4">
      <div className="mb-3 flex flex-col justify-between gap-3 md:flex-row md:items-center">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Búsqueda global</h2>
          <div className="text-sm text-gray-500">OS, N/S, MG, cliente o equipo.</div>
        </div>
        <input
          className="w-full rounded border p-2 md:max-w-lg"
          placeholder="Buscar OS, serie, MG, cliente o equipo"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          aria-label="Búsqueda global"
        />
      </div>
      {search.trim().length >= 2 && (
        <div className="overflow-hidden rounded border">
          {searching ? (
            <div className="p-3 text-sm text-gray-500">Buscando...</div>
          ) : (searchData?.groups || []).some((group) => group.items?.length) ? (
            <div className="grid grid-cols-1 divide-y lg:grid-cols-3 lg:divide-x lg:divide-y-0">
              {(searchData?.groups || []).map((group) => (
                <div key={group.key}>
                  <div className="bg-gray-50 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                    {group.label}
                  </div>
                  {(group.items || []).length ? (
                    group.items.map((item) => (
                      <SearchResult
                        key={`${group.key}-${item.id || item.href}`}
                        item={item}
                        groupKey={group.key}
                        onOpen={onOpen}
                      />
                    ))
                  ) : (
                    <div className="p-3 text-sm text-gray-500">Sin resultados.</div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="p-3 text-sm text-gray-500">Sin resultados.</div>
          )}
        </div>
      )}
    </section>
  );
}

function PrioritiesSection({
  title,
  subtitle,
  rows,
  loading,
  emptyText,
  onOpen,
  action,
  showTechnician = true,
}) {
  const navigate = useNavigate();
  return (
    <section className="rounded border bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
          {subtitle && <div className="text-sm text-gray-500">{subtitle}</div>}
        </div>
        {action && (
          <button type="button" className="rounded border px-3 py-2 text-sm hover:bg-gray-50" onClick={() => navigate(action.href)}>
            {action.label}
          </button>
        )}
      </div>
      <WorkQueueTable
        rows={rows}
        loading={loading}
        emptyText={emptyText}
        onOpen={onOpen}
        showTechnician={showTechnician}
      />
    </section>
  );
}

function AlertsSection({ alerts, onNavigate, title = "Alertas operativas" }) {
  const list = Array.isArray(alerts) ? alerts : [];
  return (
    <section className="rounded border bg-white p-4">
      <h2 className="mb-3 text-lg font-semibold text-gray-900">{title}</h2>
      {list.length ? (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {list.map((alert) => (
            <button
              key={alert.key}
              type="button"
              onClick={() => alert.href && onNavigate(alert.href)}
              className={`rounded border p-3 text-left hover:shadow-sm ${severityClasses[alert.severity] || severityClasses.info}`}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="font-semibold">{alert.title}</div>
                <div className="text-lg font-semibold">{alert.count}</div>
              </div>
              <div className="text-sm opacity-80">{alert.description}</div>
            </button>
          ))}
        </div>
      ) : (
        <div className="text-sm text-gray-500">Sin alertas activas.</div>
      )}
    </section>
  );
}

function ObjectivesSection({ items, title = "Objetivos" }) {
  const list = Array.isArray(items) ? items : [];
  return (
    <section className="rounded border bg-white p-4">
      <h2 className="mb-2 text-lg font-semibold text-gray-900">{title}</h2>
      {list.length ? (
        list.map((item) => <ObjectiveRow key={item.id} item={item} />)
      ) : (
        <div className="text-sm text-gray-500">No hay objetivos activos para este período.</div>
      )}
    </section>
  );
}

function QuickAccessSection({ showScanner = true }) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-gray-900">Accesos rápidos</h2>
      <BusquedaNSCard />
      <BusquedaAccRefCard />
      {showScanner && <QrScanCard />}
    </section>
  );
}

function UpdatedAt({ value }) {
  if (!value) return null;
  return <div className="text-xs text-gray-500">Actualizado: {formatDateOnly(value)}</div>;
}

function DeliveryOrderArticlesSummary({ order }) {
  const summary = deliveryOrderItemsSummary(order, 1);
  return (
    <div>
      <div className="font-medium text-gray-900">{summary.primary}</div>
      {summary.secondary && <div className="text-xs text-gray-500">{summary.secondary}</div>}
    </div>
  );
}

function DeliveryOrdersSection({
  summary,
  loading,
  title,
  subtitle,
  emptyText = "Sin pedidos para mostrar.",
  actionLabel = "Ver órdenes",
  actionHref = "/administracion/ordenes-entrega",
}) {
  const navigate = useNavigate();
  const rows = Array.isArray(summary?.delivery_orders?.items) ? summary.delivery_orders.items : [];
  return (
    <section className="rounded border bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
          {subtitle && <div className="text-sm text-gray-500">{subtitle}</div>}
        </div>
        <button type="button" className="rounded border px-3 py-2 text-sm hover:bg-gray-50" onClick={() => navigate(actionHref)}>
          {actionLabel}
        </button>
      </div>

      {loading && !summary ? (
        <div className="py-6 text-sm text-gray-500">Cargando pedidos...</div>
      ) : rows.length ? (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
              <tr>
                <th className="px-2 py-2">Pedido</th>
                <th className="px-2 py-2">Cliente</th>
                <th className="px-2 py-2">Estado</th>
                <th className="px-2 py-2">Artículos</th>
                <th className="px-2 py-2">Comercial</th>
                <th className="px-2 py-2">Remito</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((order) => (
                <tr key={order.id || order.orderNumber} className="border-t align-top">
                  <td className="px-2 py-2 font-medium">{order.orderNumber || "-"}</td>
                  <td className="px-2 py-2">{order.customerName || "-"}</td>
                  <td className="px-2 py-2">{STATUS_LABELS[order.status] || order.status || "-"}</td>
                  <td className="px-2 py-2"><DeliveryOrderArticlesSummary order={order} /></td>
                  <td className="px-2 py-2 text-xs text-gray-600">
                    {deliveryOrderCommercialLabel(order) || deliveryOrderSourceLabel(order) || "-"}
                  </td>
                  <td className="px-2 py-2">
                    <div>{order.remitoNumber || "-"}</div>
                    {order.remitoLocation && <div className="text-xs text-gray-500">{order.remitoLocation}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="py-6 text-sm text-gray-500">{emptyText}</div>
      )}
    </section>
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
            <div className="text-lg font-semibold">Nueva orden de entrega</div>
            <div className="text-sm text-gray-600">Clientes, artículos, partidas y detalle completo como en Portal.</div>
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

function JefeDashboard(props) {
  const {
    summary,
    loading,
    priorities,
    search,
    setSearch,
    searchData,
    searching,
    onOpen,
    onNavigate,
  } = props;
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <main className="space-y-5">
        <SearchSection
          search={search}
          setSearch={setSearch}
          searchData={searchData}
          searching={searching}
          onOpen={onOpen}
        />
        <KpiGrid items={summary?.kpis} loading={loading && !summary} />
        <PrioritiesSection
          title="Prioridades"
          subtitle="Trabajos ordenados por urgencia y antigüedad."
          rows={priorities}
          loading={loading && !summary}
          emptyText="No hay prioridades críticas por ahora."
          onOpen={onOpen}
          action={{ label: "Ver pendientes", href: "/pendientes" }}
        />
        <AlertsSection alerts={summary?.alerts} onNavigate={onNavigate} />
      </main>

      <aside className="space-y-5">
        <ObjectivesSection items={summary?.objetivos} />
        <DeliveryOrdersSection
          summary={summary}
          loading={loading}
          title="Pedidos activos"
          subtitle="Órdenes de entrega abiertas."
          emptyText="No hay pedidos activos."
        />
        <QuickAccessSection />
        <UpdatedAt value={summary?.generated_at} />
      </aside>
    </div>
  );
}

function TecnicoDashboard(props) {
  const {
    summary,
    loading,
    priorities,
    search,
    setSearch,
    searchData,
    searching,
    onOpen,
    onNavigate,
  } = props;
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <main className="space-y-5">
        <SearchSection
          search={search}
          setSearch={setSearch}
          searchData={searchData}
          searching={searching}
          onOpen={onOpen}
        />
        <KpiGrid items={summary?.kpis} loading={loading && !summary} />
        <PrioritiesSection
          title="Mi trabajo"
          subtitle="Trabajos asignados ordenados por urgencia y antigüedad."
          rows={priorities}
          loading={loading && !summary}
          emptyText="No tiene prioridades críticas por ahora."
          onOpen={onOpen}
          action={{ label: "Ver mis pendientes", href: "/pendientes" }}
          showTechnician={false}
        />
        <AlertsSection alerts={summary?.alerts} onNavigate={onNavigate} title="Mis alertas" />
      </main>

      <aside className="space-y-5">
        <ObjectivesSection items={summary?.objetivos} title="Mis objetivos" />
        <QuickAccessSection />
        <UpdatedAt value={summary?.generated_at} />
      </aside>
    </div>
  );
}

function RecepcionDashboard({ summary, loading, onReload }) {
  return (
    <div className="space-y-5">
      <KpiGrid items={summary?.kpis} loading={loading && !summary} />
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        <main className="space-y-5">
          <QrScanCard
            receptionMode
            title="Lector de recepción"
            subtitle="Escanee equipo u OS para ingreso RIS, etiqueta o egreso RSS."
            onDelivered={onReload}
          />
          <DeliveryOrdersSection
            summary={summary}
            loading={loading}
            title="Pedidos y remitos"
            subtitle="Pedidos activos, entregas y remitos para ubicar."
            emptyText="No hay pedidos activos."
          />
        </main>
        <aside className="space-y-5">
          <QuickAccessSection showScanner={false} />
          <UpdatedAt value={summary?.generated_at} />
        </aside>
      </div>
    </div>
  );
}

function AdminDashboard(props) {
  const {
    summary,
    loading,
    priorities,
    search,
    setSearch,
    searchData,
    searching,
    onOpen,
    onNavigate,
  } = props;
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <main className="space-y-5">
        <SearchSection
          search={search}
          setSearch={setSearch}
          searchData={searchData}
          searching={searching}
          onOpen={onOpen}
        />
        <KpiGrid items={summary?.kpis} loading={loading && !summary} />
        <DeliveryOrdersSection
          summary={summary}
          loading={loading}
          title="Pedidos para preparar"
          subtitle="Órdenes de entrega que requieren armado o remito."
          emptyText="No hay pedidos pendientes de preparación."
        />
        <PrioritiesSection
          title="Logística operativa"
          subtitle="Liberados y derivaciones que requieren seguimiento."
          rows={priorities}
          loading={loading && !summary}
          emptyText="No hay pendientes logísticos críticos por ahora."
          onOpen={onOpen}
        />
        <AlertsSection alerts={summary?.alerts} onNavigate={onNavigate} title="Alertas administrativas" />
      </main>

      <aside className="space-y-5">
        <QuickAccessSection />
        <UpdatedAt value={summary?.generated_at} />
      </aside>
    </div>
  );
}

function CobranzasDashboard({ summary, loading }) {
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <main className="space-y-5">
        <KpiGrid items={summary?.kpis} loading={loading && !summary} />
        <DeliveryOrdersSection
          summary={summary}
          loading={loading}
          title="Remitos pendientes de facturación"
          subtitle="Pedidos entregados que todavía necesitan factura."
          emptyText="No hay remitos pendientes de facturación."
          actionLabel="Ir a cobranzas"
          actionHref="/cobranzas/facturacion"
        />
      </main>
      <aside className="space-y-5">
        <section className="rounded border bg-white p-4">
          <h2 className="mb-2 text-lg font-semibold text-gray-900">Acceso directo</h2>
          <Link to="/cobranzas/facturacion" className="inline-flex rounded border px-3 py-2 text-sm hover:bg-gray-50">
            Facturación
          </Link>
        </section>
        <UpdatedAt value={summary?.generated_at} />
      </aside>
    </div>
  );
}

export default function WorkDashboard() {
  const [periodo, setPeriodo] = useState("hoy");
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [search, setSearch] = useState("");
  const [searchData, setSearchData] = useState(null);
  const [searching, setSearching] = useState(false);
  const [orderModalOpen, setOrderModalOpen] = useState(false);
  const [createdOrder, setCreatedOrder] = useState(null);
  const navigate = useNavigate();
  const { user } = useAuth();
  const canCreateOrder = can(user, PERMISSION_CODES.ACTION_DELIVERY_ORDER_CREATE);

  async function load() {
    try {
      setErr("");
      setLoading(true);
      setSummary(await getWorkResumen({ periodo }));
    } catch (error) {
      setErr(error?.message || "No se pudo cargar el centro de trabajo");
      setSummary(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [periodo]);

  useEffect(() => {
    const term = search.trim();
    if (term.length < 2) {
      setSearchData(null);
      setSearching(false);
      return undefined;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        setSearching(true);
        const data = await getGlobalSearch(term);
        if (!cancelled) setSearchData(data);
      } catch {
        if (!cancelled) setSearchData({ q: term, groups: [], total: 0 });
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [search]);

  const variant = summary?.scope?.dashboard_variant || "jefe";
  const priorities = useMemo(
    () => (summary?.prioridades || []).filter((item) => ingresoIdOf(item)).slice(0, 10),
    [summary],
  );

  const openItem = (item) => {
    const href = item?.href;
    if (href) {
      navigate(href);
      return;
    }
    const id = ingresoIdOf(item);
    if (id) navigate(`/ingresos/${id}`);
  };

  const handleOrderCreated = (order) => {
    setCreatedOrder(order || {});
    setOrderModalOpen(false);
    load();
  };

  const renderDashboard = () => {
    const props = {
      summary,
      loading,
      priorities,
      search,
      setSearch,
      searchData,
      searching,
      onOpen: openItem,
      onNavigate: navigate,
      onReload: load,
    };
    if (variant === "tecnico") return <TecnicoDashboard {...props} />;
    if (variant === "recepcion") return <RecepcionDashboard {...props} />;
    if (variant === "admin") return <AdminDashboard {...props} />;
    if (variant === "cobranzas") return <CobranzasDashboard {...props} />;
    return <JefeDashboard {...props} />;
  };

  const headerByVariant = {
    jefe: {
      title: "Centro de trabajo",
      subtitle: "Prioridades, avisos, objetivos y búsqueda rápida del servicio técnico.",
      showPeriod: true,
    },
    tecnico: {
      title: "Mi trabajo",
      subtitle: "Prioridades, alertas y objetivos asignados a tu usuario.",
      showPeriod: true,
    },
    recepcion: {
      title: "Recepción",
      subtitle: "Ingresos, pedidos y remitos activos.",
      showPeriod: false,
    },
    admin: {
      title: "Administración operativa",
      subtitle: "Seguimiento de logística, pedidos, derivaciones y preventivos.",
      showPeriod: false,
    },
    cobranzas: {
      title: "Cobranzas",
      subtitle: "Remitos pendientes, clientes a facturar y consulta de facturación.",
      showPeriod: false,
    },
  };
  const header = headerByVariant[variant] || headerByVariant.jefe;

  return (
    <div className="space-y-5 p-4 md:p-6">
      <DashboardHeader
        title={header.title}
        subtitle={header.subtitle}
        periodo={periodo}
        onPeriodoChange={setPeriodo}
        onReload={load}
        showPeriod={header.showPeriod}
        canCreateOrder={canCreateOrder}
        onCreateOrder={() => setOrderModalOpen(true)}
      />

      {createdOrder && (
        <div className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          Entrega {createdOrder.orderNumber || "creada"} creada.{" "}
          <Link to="/administracion/ordenes-entrega" className="font-medium underline">
            Ver órdenes de entrega
          </Link>
        </div>
      )}

      {err && (
        <div className="rounded border border-red-300 bg-red-100 p-2 text-red-700">
          {err}
        </div>
      )}

      {renderDashboard()}

      <NewDeliveryOrderModal
        open={orderModalOpen}
        onClose={() => setOrderModalOpen(false)}
        onCreated={handleOrderCreated}
      />
    </div>
  );
}
