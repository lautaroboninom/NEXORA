import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getGlobalSearch, getWorkResumen } from "../lib/api";
import BusquedaNSCard from "../components/BusquedaNSCard.jsx";
import BusquedaAccRefCard from "../components/BusquedaAccRefCard.jsx";
import QrScanCard from "../components/QrScanCard.jsx";
import WorkQueueTable from "../components/WorkQueueTable.jsx";
import {
  catalogEquipmentLabel,
  formatDateOnly,
  formatOS,
  ingresoIdOf,
  nsPreferInternoOf,
} from "../lib/ui-helpers";

const severityClasses = {
  critical: "border-red-200 bg-red-50 text-red-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  info: "border-blue-200 bg-blue-50 text-blue-800",
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
    <section className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3">
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
    <div className="border-b last:border-b-0 py-3">
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
      <div className="mt-2 h-2 rounded bg-gray-100 overflow-hidden">
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
  const subtitle =
    groupKey === "clientes"
      ? [item?.cod_empresa, item?.telefono, item?.email].filter(Boolean).join(" · ")
      : [item?.cliente, catalogEquipmentLabel(item), nsPreferInternoOf(item)].filter(Boolean).join(" · ");

  return (
    <button
      type="button"
      className="w-full text-left px-3 py-2 hover:bg-gray-50 border-b last:border-b-0"
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
}) {
  return (
    <div className="flex flex-col lg:flex-row lg:items-end gap-3 justify-between">
      <div>
        <h1 className="text-2xl font-semibold text-gray-900">{title}</h1>
        {subtitle && <div className="text-sm text-gray-500">{subtitle}</div>}
      </div>
      <div className="flex flex-col sm:flex-row gap-2">
        {showPeriod && (
          <select
            className="border rounded p-2"
            value={periodo}
            onChange={(e) => onPeriodoChange(e.target.value)}
            aria-label="Período de objetivos"
          >
            <option value="hoy">Hoy</option>
            <option value="semana">Semana</option>
          </select>
        )}
        <button type="button" className="btn" onClick={onReload}>
          Recargar
        </button>
      </div>
    </div>
  );
}

function SearchSection({ search, setSearch, searchData, searching, onOpen }) {
  return (
    <section className="rounded border bg-white p-4">
      <div className="flex flex-col md:flex-row md:items-center gap-3 justify-between mb-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Búsqueda global</h2>
          <div className="text-sm text-gray-500">OS, N/S, MG, cliente o equipo.</div>
        </div>
        <input
          className="border rounded p-2 w-full md:max-w-lg"
          placeholder="Buscar OS, serie, MG, cliente o equipo"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Búsqueda global"
        />
      </div>
      {search.trim().length >= 2 && (
        <div className="border rounded overflow-hidden">
          {searching ? (
            <div className="p-3 text-sm text-gray-500">Buscando...</div>
          ) : (searchData?.groups || []).some((group) => group.items?.length) ? (
            <div className="grid grid-cols-1 lg:grid-cols-3 divide-y lg:divide-y-0 lg:divide-x">
              {(searchData?.groups || []).map((group) => (
                <div key={group.key}>
                  <div className="px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-500 bg-gray-50">
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
      <div className="flex items-center justify-between gap-3 mb-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
          {subtitle && <div className="text-sm text-gray-500">{subtitle}</div>}
        </div>
        {action && (
          <button type="button" className="btn" onClick={() => navigate(action.href)}>
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
      <h2 className="text-lg font-semibold text-gray-900 mb-3">{title}</h2>
      {list.length ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {list.map((alert) => (
            <button
              key={alert.key}
              type="button"
              onClick={() => alert.href && onNavigate(alert.href)}
              className={`text-left rounded border p-3 hover:shadow-sm ${severityClasses[alert.severity] || severityClasses.info}`}
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
      <h2 className="text-lg font-semibold text-gray-900 mb-2">{title}</h2>
      {list.length ? (
        list.map((item) => <ObjectiveRow key={item.id} item={item} />)
      ) : (
        <div className="text-sm text-gray-500">No hay objetivos activos para este período.</div>
      )}
    </section>
  );
}

function QuickAccessSection() {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-gray-900">Accesos rápidos</h2>
      <BusquedaNSCard />
      <BusquedaAccRefCard />
      <QrScanCard />
    </section>
  );
}

function UpdatedAt({ value }) {
  if (!value) return null;
  return <div className="text-xs text-gray-500">Actualizado: {formatDateOnly(value)}</div>;
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
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_360px] gap-5">
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
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_360px] gap-5">
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
          emptyText="No tenés prioridades críticas por ahora."
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

function RecepcionDashboard({ summary, loading, priorities, onOpen }) {
  return (
    <div className="space-y-5">
      <KpiGrid items={summary?.kpis} loading={loading && !summary} />
      <PrioritiesSection
        title="Liberados en espera"
        subtitle="Equipos listos para coordinar entrega."
        rows={priorities}
        loading={loading && !summary}
        emptyText="No hay equipos liberados en espera."
        onOpen={onOpen}
        action={{ label: "Ver listos", href: "/listos" }}
      />
      <UpdatedAt value={summary?.generated_at} />
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
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_360px] gap-5">
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

export default function WorkDashboard() {
  const [periodo, setPeriodo] = useState("hoy");
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [search, setSearch] = useState("");
  const [searchData, setSearchData] = useState(null);
  const [searching, setSearching] = useState(false);
  const navigate = useNavigate();

  async function load() {
    try {
      setErr("");
      setLoading(true);
      setSummary(await getWorkResumen({ periodo }));
    } catch (e) {
      setErr(e?.message || "No se pudo cargar el centro de trabajo");
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
    };
    if (variant === "tecnico") return <TecnicoDashboard {...props} />;
    if (variant === "recepcion") return <RecepcionDashboard {...props} />;
    if (variant === "admin") return <AdminDashboard {...props} />;
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
      subtitle: "Equipos liberados pendientes de entrega.",
      showPeriod: false,
    },
    admin: {
      title: "Administración operativa",
      subtitle: "Seguimiento de logística, derivaciones y preventivos.",
      showPeriod: false,
    },
  };
  const header = headerByVariant[variant] || headerByVariant.jefe;

  return (
    <div className="p-4 md:p-6 space-y-5">
      <DashboardHeader
        title={header.title}
        subtitle={header.subtitle}
        periodo={periodo}
        onPeriodoChange={setPeriodo}
        onReload={load}
        showPeriod={header.showPeriod}
      />

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded">
          {err}
        </div>
      )}

      {renderDashboard()}
    </div>
  );
}
