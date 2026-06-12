import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import Sidebar from "./components/Sidebar.jsx";
import Footer from "./components/Footer.jsx";
import NotificationBell from "./components/NotificationBell.jsx";
import useRouteUiState from "./hooks/useRouteUiState";
import { useAuth } from "./context/AuthContext";
import { can, canAny, PERMISSION_CODES } from "./lib/permissions";

export default function App() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const rol = user?.rol;

  const canSeeGeneralCliente = canAny(user, [
    PERMISSION_CODES.PAGE_GENERAL_CLIENTE,
    PERMISSION_CODES.PAGE_INGRESOS_HISTORY,
  ]);
  const canSeeHistorico = can(user, PERMISSION_CODES.PAGE_INGRESOS_HISTORY);
  const canSeeEquipos = can(user, PERMISSION_CODES.PAGE_DEVICES_PREVENTIVOS);
  const canSeeRecepcion = can(user, PERMISSION_CODES.PAGE_RECEPCION);
  const canSeeDeliveryOrders = can(user, PERMISSION_CODES.PAGE_DELIVERY_ORDERS);
  const canSeeBilling = can(user, PERMISSION_CODES.PAGE_BILLING);
  const canCreateIngreso = canAny(user, [
    PERMISSION_CODES.ACTION_INGRESO_CREATE,
    PERMISSION_CODES.PAGE_NEW_INGRESO,
  ]);

  useRouteUiState();

  useEffect(() => {
    setMobileMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!mobileMenuOpen) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [mobileMenuOpen]);

  useEffect(() => {
    if (!mobileMenuOpen) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        setMobileMenuOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [mobileMenuOpen]);

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b bg-white">
        <div className="max-w-7xl mx-auto px-4 h-12 flex items-center">
          <button
            type="button"
            aria-label="Abrir menú"
            aria-controls="app-sidebar"
            aria-expanded={mobileMenuOpen}
            onClick={() => setMobileMenuOpen((open) => !open)}
            className="mr-3 inline-flex h-8 w-8 items-center justify-center rounded border border-gray-200 text-gray-700 hover:bg-gray-50 md:hidden"
          >
            <span className="sr-only">Abrir menú</span>
            <span className="flex flex-col gap-1">
              <span className="block h-0.5 w-4 bg-current"></span>
              <span className="block h-0.5 w-4 bg-current"></span>
              <span className="block h-0.5 w-4 bg-current"></span>
            </span>
          </button>

          <Link to="/" className="flex h-8 items-center overflow-hidden" aria-label="NEXORA">
            <img
              src="/branding/logotipo-nexora.png"
              alt="NEXORA"
              className="h-12 w-auto object-contain"
            />
          </Link>

          <nav className="hidden md:flex items-center gap-6 ml-6">
            {canSeeRecepcion && (
              <Link to="/recepcion" className="hover:underline">
                Recepción
              </Link>
            )}
            {canSeeDeliveryOrders && (
              <Link to="/administracion/ordenes-entrega" className="hover:underline">
                Órdenes
              </Link>
            )}
            {canSeeBilling && (
              <Link to="/cobranzas/facturacion" className="hover:underline">
                Cobranzas
              </Link>
            )}
            {canSeeGeneralCliente && (
              <Link to="/clientes" className="hover:underline">
                General por cliente
              </Link>
            )}
            {canSeeHistorico && (
              <Link to="/ingresos/historico" className="hover:underline">
                Histórico ingresos
              </Link>
            )}
            {canSeeEquipos && (
              <Link to="/equipos" className="hover:underline">
                Equipos
              </Link>
            )}
          </nav>

          <div className="ml-auto flex items-center gap-2 md:gap-3">
            {canCreateIngreso && (
              <Link
                to="/ingresos/nuevo"
                className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 text-xs md:text-sm"
              >
                Nuevo ingreso
              </Link>
            )}
            {user && <NotificationBell />}
            {user && (
              <span className="hidden md:inline text-sm text-gray-500">
                {user?.nombre} {rol}
              </span>
            )}
            <button
              onClick={() => {
                logout();
                nav("/login");
              }}
              className="px-2.5 py-1.5 rounded border hover:bg-gray-50 text-xs md:text-sm md:px-3"
            >
              Salir
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1 flex">
        <Sidebar
          mobileOpen={mobileMenuOpen}
          onClose={() => setMobileMenuOpen(false)}
        />
        <div className="flex-1 p-3 md:p-6">
          <Outlet />
        </div>
      </main>

      <Footer />
    </div>
  );
}
