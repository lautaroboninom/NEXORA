import { NavLink } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { can, canAny, PERMISSION_CODES } from "../lib/permissions";
import { canActAsTech } from "../lib/authz";

const VARIANT_BORDER = {
  amber: "border-amber-500",
  green: "border-emerald-500",
  lime: "border-lime-500",
  blue: "border-blue-500",
  indigo: "border-indigo-500",
  cyan: "border-cyan-500",
  gray: "border-gray-400",
};

function variantOfPath(to) {
  const p = String(to || "");
  if (p === "/tecnico") return "amber";
  if (p === "/pendientes") return "gray";
  if (p === "/pendientes-por-tecnico") return "amber";
  if (p === "/pendientes-presupuesto") return "amber";
  if (p === "/presupuestados") return "lime";
  if (p === "/aprobados") return "lime";
  if (p === "/derivados") return "cyan";
  if (p === "/reparados") return "lime";
  if (p === "/listos") return "green";
  if (p === "/alquiler/stock") return "indigo";
  if (p === "/recepcion") return "blue";
  if (p === "/administracion/ordenes-entrega") return "indigo";
  if (p === "/cobranzas/facturacion") return "green";
  return null;
}

const LinkItem = ({ to, children, variant, onClick }) => (
  <NavLink
    to={to}
    onClick={onClick}
    className={({ isActive }) => {
      const base = "block rounded border-l-4 px-3 py-2 hover:bg-gray-50";
      const active = isActive ? " bg-gray-100 font-semibold" : "";
      const v = variant || variantOfPath(to);
      const border = v ? VARIANT_BORDER[v] || "border-gray-200" : "border-transparent";
      return `${base} ${border}${active}`;
    }}
  >
    {children}
  </NavLink>
);

const Section = ({ title, children }) => (
  <div>
    <div className="mb-1 px-1 text-xs uppercase text-gray-400">{title}</div>
    <div className="space-y-1">{children}</div>
  </div>
);

export default function Sidebar({ mobileOpen = false, onClose }) {
  const { user } = useAuth();
  if (!user) return null;

  const techLike = canActAsTech(user);
  const canHome = can(user, PERMISSION_CODES.PAGE_HOME_SEARCH);
  const canHistory = can(user, PERMISSION_CODES.PAGE_INGRESOS_HISTORY);
  const canWorkQueues = can(user, PERMISSION_CODES.PAGE_WORK_QUEUES);
  const canBudgetQueues = can(user, PERMISSION_CODES.PAGE_BUDGET_QUEUES);
  const canLogistics = can(user, PERMISSION_CODES.PAGE_LOGISTICS);
  const canLiberados = can(user, PERMISSION_CODES.PAGE_LIBERADOS);
  const canDevices = can(user, PERMISSION_CODES.PAGE_DEVICES_PREVENTIVOS);
  const canMetrics = can(user, PERMISSION_CODES.PAGE_METRICS);
  const canUsers = can(user, PERMISSION_CODES.PAGE_USERS);
  const canCatalogs = can(user, PERMISSION_CODES.PAGE_CATALOGS);
  const canSpareParts = can(user, PERMISSION_CODES.PAGE_SPARE_PARTS);
  const canWarranty = can(user, PERMISSION_CODES.PAGE_WARRANTY);
  const canBejerman = can(user, PERMISSION_CODES.PAGE_BEJERMAN_SYNC);
  const canRecepcion = can(user, PERMISSION_CODES.PAGE_RECEPCION);
  const canDeliveryOrders = can(user, PERMISSION_CODES.PAGE_DELIVERY_ORDERS);
  const canBilling = can(user, PERMISSION_CODES.PAGE_BILLING);
  const canCreateIngreso = canAny(user, [
    PERMISSION_CODES.ACTION_INGRESO_CREATE,
    PERMISSION_CODES.PAGE_NEW_INGRESO,
  ]);
  const canManageTestProtocols = can(user, PERMISSION_CODES.ACTION_TESTS_PROTOCOL_MANAGE);

  const showRecepcion = canRecepcion || canCreateIngreso;
  const showServicioTecnico = canWorkQueues || canBudgetQueues || canLogistics || canLiberados;
  const showAdministracion = canHistory || canDevices || canDeliveryOrders || canCreateIngreso || canHome;
  const showCobranzas = canBilling;
  const showSistema =
    canMetrics || canUsers || canCatalogs || canSpareParts || canWarranty || canBejerman || canManageTestProtocols;

  const handleNavigate = () => {
    if (onClose) onClose();
  };
  const linkProps = onClose ? { onClick: handleNavigate } : {};

  return (
    <>
      <div
        className={`fixed inset-0 z-40 bg-black/40 md:hidden ${mobileOpen ? "block" : "hidden"}`}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        id="app-sidebar"
        className={`fixed inset-y-0 left-0 z-50 w-72 transform overflow-y-auto border-r bg-white text-sm shadow-lg transition-transform duration-200 ease-out md:static md:w-56 md:translate-x-0 md:shadow-none md:block ${
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-12 items-center justify-between border-b px-3 md:hidden">
          <span className="text-sm text-gray-600">Menú</span>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-8 w-8 items-center justify-center rounded border border-gray-200 text-gray-700 hover:bg-gray-50"
            aria-label="Cerrar menú"
          >
            X
          </button>
        </div>

        <div className="border-b px-3 py-2 text-xs text-gray-500 md:hidden">
          {user?.nombre} {user?.rol}
        </div>

        <div className="hidden p-3 text-xs text-gray-500 md:block">NEXORA</div>
        <div className="space-y-3 px-3 pb-3">
          {showRecepcion && (
            <Section title="Recepción">
              {canRecepcion && (
                <LinkItem to="/recepcion" {...linkProps}>
                  Panel de recepción
                </LinkItem>
              )}
              {canDeliveryOrders && (
                <LinkItem to="/administracion/ordenes-entrega" {...linkProps}>
                  Órdenes de entrega
                </LinkItem>
              )}
            </Section>
          )}

          {showServicioTecnico && (
            <Section title="Servicio técnico">
              {canWorkQueues && (
                <LinkItem to="/pendientes" {...linkProps}>
                  Pendientes general
                </LinkItem>
              )}
              {canWorkQueues && techLike && (
                <LinkItem to="/tecnico" {...linkProps}>
                  Mis pendientes
                </LinkItem>
              )}
              {canWorkQueues && (
                <LinkItem to="/pendientes-por-tecnico" {...linkProps}>
                  Pendientes por técnico
                </LinkItem>
              )}
              {canBudgetQueues && (
                <>
                  <LinkItem to="/pendientes-presupuesto" {...linkProps}>
                    Pendientes de presupuesto
                  </LinkItem>
                  <LinkItem to="/presupuestados" {...linkProps}>
                    Presupuestados
                  </LinkItem>
                </>
              )}
              {canWorkQueues && (
                <>
                  <LinkItem to="/aprobados" {...linkProps}>
                    Aprobados
                  </LinkItem>
                  <LinkItem to="/reparados" {...linkProps}>
                    Reparados
                  </LinkItem>
                </>
              )}
              {canLiberados && (
                <LinkItem to="/listos" {...linkProps}>
                  Liberados
                </LinkItem>
              )}
              {canLogistics && (
                <>
                  <LinkItem to="/derivados" {...linkProps}>
                    Derivados
                  </LinkItem>
                  <LinkItem to="/alquiler/stock" {...linkProps}>
                    Stock de alquiler
                  </LinkItem>
                  <LinkItem to="/depositos" {...linkProps}>
                    Depósitos/Bajas
                  </LinkItem>
                </>
              )}
            </Section>
          )}

          {showAdministracion && (
            <Section title="Administración">
              {canDeliveryOrders && (
                <LinkItem to="/administracion/ordenes-entrega" {...linkProps}>
                  Órdenes de entrega
                </LinkItem>
              )}
              {canHistory && (
                <LinkItem to="/clientes" {...linkProps}>
                  General por cliente
                </LinkItem>
              )}
              {canHistory && (
                <LinkItem to="/ingresos/historico" {...linkProps}>
                  Histórico ingresos
                </LinkItem>
              )}
              {canDevices && (
                <LinkItem to="/equipos" {...linkProps}>
                  Equipos
                </LinkItem>
              )}
              {canHome && (
                <>
                  <LinkItem to="/buscar-ns" {...linkProps}>
                    Buscar NS
                  </LinkItem>
                  <LinkItem to="/buscar-accesorio" {...linkProps}>
                    Buscar accesorio
                  </LinkItem>
                </>
              )}
            </Section>
          )}

          {showCobranzas && (
            <Section title="Cobranzas">
              <LinkItem to="/cobranzas/facturacion" {...linkProps}>
                Facturación
              </LinkItem>
              {canDeliveryOrders && (
                <LinkItem to="/administracion/ordenes-entrega" {...linkProps}>
                  Remitos pendientes
                </LinkItem>
              )}
            </Section>
          )}

          {showSistema && (
            <Section title="Sistema">
              {canMetrics && (
                <LinkItem to="/metricas" {...linkProps}>
                  Métricas
                </LinkItem>
              )}
              {canUsers && (
                <LinkItem to="/usuarios" {...linkProps}>
                  Usuarios
                </LinkItem>
              )}
              {canCatalogs && (
                <>
                  <LinkItem to="/catalogo/clientes" {...linkProps}>
                    Clientes
                  </LinkItem>
                  <LinkItem to="/catalogo/tipos-equipo" {...linkProps}>
                    Tipos de equipo
                  </LinkItem>
                  <LinkItem to="/catalogo/accesorios" {...linkProps}>
                    Accesorios
                  </LinkItem>
                  <LinkItem to="/catalogo/marcas" {...linkProps}>
                    Marcas y modelos
                  </LinkItem>
                  <LinkItem to="/catalogo/proveedores" {...linkProps}>
                    Proveedores externos
                  </LinkItem>
                </>
              )}
              {canSpareParts && (
                <LinkItem to="/catalogo/repuestos" {...linkProps}>
                  Repuestos
                </LinkItem>
              )}
              {canWarranty && (
                <LinkItem to="/garantias" {...linkProps}>
                  Garantías
                </LinkItem>
              )}
              {canBejerman && (
                <LinkItem to="/bejerman" {...linkProps}>
                  Bejerman
                </LinkItem>
              )}
              {canManageTestProtocols && (
                <LinkItem to="/sistema/protocolos-test" {...linkProps}>
                  Protocolos de test
                </LinkItem>
              )}
            </Section>
          )}
        </div>
      </aside>
    </>
  );
}
