import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import ErrorBoundary from "./components/ErrorBoundary";

import "./index.css";
import App from "./App";
import Login from "./pages/Login";
import Forbidden from "./pages/Forbidden.jsx";
import Tecnico from "./pages/Tecnico";
import Presupuestados from "./pages/Presupuestados.jsx";
import PendientesPresupuesto from "./pages/PendientesPresupuesto.jsx";
import AdminListos from "./pages/AdminListos";
import GeneralPorCliente from "./pages/GeneralPorCliente";
import Usuarios from "./pages/Usuarios";
import NuevoIngreso from "./pages/NuevoIngreso";
import CatalogoClientes from "./pages/CatalogoClientes";
import CatalogoMarcas from "./pages/CatalogoMarcas";
import CatalogoProveedores from "./pages/CatalogoProveedores";
import TiposEquipo from "./pages/TiposEquipo.jsx";
import Accesorios from "./pages/Accesorios.jsx";
import Repuestos from "./pages/Repuestos.jsx";
import ProtectedRoute from "./components/ProtectedRoute";
import PendientesGeneral from "./pages/PendientesGeneral.jsx";
import Aprobados from "./pages/Aprobados.jsx";
import Reparados from "./pages/Reparados.jsx";
import HistoricoIngresos from "./pages/HistoricoIngresos.jsx";
import ServiceSheet from "./pages/ServiceSheet";
import PendientesPorTecnico from "./pages/PendientesPorTecnico.jsx";
import DerivarIngreso from "./pages/DerivarIngreso.jsx";
import StockAlquiler from "./pages/StockAlquiler.jsx";
import WorkDashboard from "./pages/WorkDashboard.jsx";
import BuscarNS from "./pages/BuscarNS.jsx";
import BuscarAccesorio from "./pages/BuscarAccesorio.jsx";
import ForgotPassword from "./pages/ForgotPassword";
import ResetPassword from "./pages/ResetPassword";
import Depositos from "./pages/Depositos.jsx";
import Derivados from "./pages/Derivados.jsx";
import Metricas from "./pages/Metricas.jsx";
import MetricasActividadTecnicos from "./pages/MetricasActividadTecnicos.jsx";
import MetricasClientes from "./pages/MetricasClientes.jsx";
import MetricasFinanzas from "./pages/MetricasFinanzas.jsx";
import Garantias from "./pages/Garantias.jsx";
import Equipos from "./pages/Equipos.jsx";
import ProtocolosTest from "./pages/ProtocolosTest.jsx";
import BejermanSync from "./pages/BejermanSync.jsx";
import RecepcionDashboard from "./pages/RecepcionDashboard.jsx";
import DeliveryOrders from "./pages/DeliveryOrders.jsx";
import Billing from "./pages/Billing.jsx";
import { can, canAny, PERMISSION_CODES } from "./lib/permissions";

function NotFound() {
  return <div className="p-8 text-center text-gray-600">Página no encontrada</div>;
}

function IndexRoute() {
  const { user } = useAuth();
  const role = (user?.rol || "").toString().trim().toLowerCase();
  if (role === "recepcion" && can(user, PERMISSION_CODES.PAGE_RECEPCION)) {
    return <Navigate to="/recepcion" replace />;
  }
  if (role === "cobranzas" && can(user, PERMISSION_CODES.PAGE_BILLING)) {
    return <Navigate to="/cobranzas/facturacion" replace />;
  }
  if (role === "admin" && can(user, PERMISSION_CODES.PAGE_DELIVERY_ORDERS)) {
    return <Navigate to="/administracion/ordenes-entrega" replace />;
  }
  if (can(user, PERMISSION_CODES.PAGE_HOME_SEARCH)) return <WorkDashboard />;
  if (canAny(user, [PERMISSION_CODES.ACTION_INGRESO_CREATE, PERMISSION_CODES.PAGE_NEW_INGRESO])) {
    return <Navigate to="/ingresos/nuevo" replace />;
  }
  if (can(user, PERMISSION_CODES.PAGE_DELIVERY_ORDERS)) return <Navigate to="/administracion/ordenes-entrega" replace />;
  if (can(user, PERMISSION_CODES.PAGE_LIBERADOS)) return <Navigate to="/listos" replace />;
  return <Forbidden />;
}

const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  { path: "/recuperar", element: <ForgotPassword /> },
  { path: "/403", element: <Forbidden /> },
  { path: "/restablecer", element: <ResetPassword /> },
  {
    path: "/",
    element: <App />,
    children: [
      {
        index: true,
        element: (
          <ProtectedRoute
            permissions={[
              PERMISSION_CODES.PAGE_HOME_SEARCH,
              PERMISSION_CODES.PAGE_RECEPCION,
              PERMISSION_CODES.PAGE_DELIVERY_ORDERS,
              PERMISSION_CODES.PAGE_BILLING,
              PERMISSION_CODES.ACTION_INGRESO_CREATE,
              PERMISSION_CODES.PAGE_NEW_INGRESO,
              PERMISSION_CODES.PAGE_LIBERADOS,
            ]}
          >
            <IndexRoute />
          </ProtectedRoute>
        ),
      },
      {
        path: "recepcion",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_RECEPCION}>
            <RecepcionDashboard />
          </ProtectedRoute>
        ),
      },
      {
        path: "administracion/ordenes-entrega",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_DELIVERY_ORDERS}>
            <DeliveryOrders />
          </ProtectedRoute>
        ),
      },
      {
        path: "ordenes-entrega",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_DELIVERY_ORDERS}>
            <DeliveryOrders />
          </ProtectedRoute>
        ),
      },
      {
        path: "cobranzas/facturacion",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_BILLING}>
            <Billing />
          </ProtectedRoute>
        ),
      },
      {
        path: "buscar-ns",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_HOME_SEARCH}>
            <BuscarNS />
          </ProtectedRoute>
        ),
      },
      {
        path: "buscar-accesorio",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_HOME_SEARCH}>
            <BuscarAccesorio />
          </ProtectedRoute>
        ),
      },
      {
        path: "tecnico",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_WORK_QUEUES}>
            <Tecnico />
          </ProtectedRoute>
        ),
      },
      {
        path: "pendientes",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_WORK_QUEUES}>
            <PendientesGeneral />
          </ProtectedRoute>
        ),
      },
      {
        path: "pendientes-por-tecnico",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_WORK_QUEUES}>
            <PendientesPorTecnico />
          </ProtectedRoute>
        ),
      },
      {
        path: "pendientes-presupuesto",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_BUDGET_QUEUES}>
            <PendientesPresupuesto />
          </ProtectedRoute>
        ),
      },
      {
        path: "presupuestados",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_BUDGET_QUEUES}>
            <Presupuestados />
          </ProtectedRoute>
        ),
      },
      {
        path: "aprobados",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_WORK_QUEUES}>
            <Aprobados />
          </ProtectedRoute>
        ),
      },
      {
        path: "reparados",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_WORK_QUEUES}>
            <Reparados />
          </ProtectedRoute>
        ),
      },
      {
        path: "derivados",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_LOGISTICS}>
            <Derivados />
          </ProtectedRoute>
        ),
      },
      {
        path: "listos",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_LIBERADOS}>
            <AdminListos />
          </ProtectedRoute>
        ),
      },
      {
        path: "alquiler/stock",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_LOGISTICS}>
            <StockAlquiler />
          </ProtectedRoute>
        ),
      },
      {
        path: "depositos",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_LOGISTICS}>
            <Depositos />
          </ProtectedRoute>
        ),
      },
      {
        path: "clientes",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_INGRESOS_HISTORY}>
            <GeneralPorCliente />
          </ProtectedRoute>
        ),
      },
      {
        path: "ingresos/historico",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_INGRESOS_HISTORY}>
            <HistoricoIngresos />
          </ProtectedRoute>
        ),
      },
      {
        path: "ingresos",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_INGRESOS_HISTORY}>
            <HistoricoIngresos />
          </ProtectedRoute>
        ),
      },
      {
        path: "equipos",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_DEVICES_PREVENTIVOS}>
            <Equipos />
          </ProtectedRoute>
        ),
      },
      {
        path: "ingresos/nuevo",
        element: (
          <ProtectedRoute
            permissions={[
              PERMISSION_CODES.ACTION_INGRESO_CREATE,
              PERMISSION_CODES.PAGE_NEW_INGRESO,
            ]}
          >
            <NuevoIngreso />
          </ProtectedRoute>
        ),
      },
      {
        path: "metricas",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_METRICS}>
            <Metricas />
          </ProtectedRoute>
        ),
      },
      {
        path: "metricas/clientes",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_METRICS}>
            <MetricasClientes />
          </ProtectedRoute>
        ),
      },
      {
        path: "metricas/actividad-tecnicos",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_METRICS}>
            <MetricasActividadTecnicos />
          </ProtectedRoute>
        ),
      },
      {
        path: "metricas/finanzas",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_METRICS}>
            <MetricasFinanzas />
          </ProtectedRoute>
        ),
      },
      {
        path: "metricas/config",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_METRICS}>
            <Metricas />
          </ProtectedRoute>
        ),
      },
      {
        path: "bejerman",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_BEJERMAN_SYNC}>
            <BejermanSync />
          </ProtectedRoute>
        ),
      },
      {
        path: "garantias",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_WARRANTY}>
            <Garantias />
          </ProtectedRoute>
        ),
      },
      {
        path: "usuarios",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_USERS}>
            <Usuarios />
          </ProtectedRoute>
        ),
      },
      {
        path: "catalogo/clientes",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_CATALOGS}>
            <CatalogoClientes />
          </ProtectedRoute>
        ),
      },
      {
        path: "catalogo/marcas",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_CATALOGS}>
            <CatalogoMarcas />
          </ProtectedRoute>
        ),
      },
      {
        path: "catalogo/tipos-equipo",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_CATALOGS}>
            <TiposEquipo />
          </ProtectedRoute>
        ),
      },
      {
        path: "catalogo/accesorios",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_CATALOGS}>
            <Accesorios />
          </ProtectedRoute>
        ),
      },
      {
        path: "catalogo/repuestos",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_SPARE_PARTS}>
            <Repuestos />
          </ProtectedRoute>
        ),
      },
      {
        path: "catalogo/proveedores",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.PAGE_CATALOGS}>
            <CatalogoProveedores />
          </ProtectedRoute>
        ),
      },
      {
        path: "sistema/protocolos-test",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.ACTION_TESTS_PROTOCOL_MANAGE}>
            <ProtocolosTest />
          </ProtectedRoute>
        ),
      },
      {
        path: "ingresos/:id",
        element: (
          <ProtectedRoute
            permissions={[
              PERMISSION_CODES.PAGE_INGRESOS_HISTORY,
              PERMISSION_CODES.PAGE_WORK_QUEUES,
              PERMISSION_CODES.PAGE_BUDGET_QUEUES,
              PERMISSION_CODES.PAGE_LOGISTICS,
              PERMISSION_CODES.PAGE_LIBERADOS,
              PERMISSION_CODES.PAGE_SERVICE_SHEET_PRINCIPAL,
              PERMISSION_CODES.ACTION_PRESUPUESTO_MANAGE,
              PERMISSION_CODES.ACTION_INGRESO_EDIT_BASICS,
              PERMISSION_CODES.ACTION_INGRESO_EDIT_DIAGNOSIS,
              PERMISSION_CODES.ACTION_INGRESO_EDIT_LOCATION,
              PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY,
              PERMISSION_CODES.ACTION_INGRESO_MANAGE_DERIVATIONS,
              PERMISSION_CODES.ACTION_INGRESO_REPAIR_TRANSITIONS,
              PERMISSION_CODES.ACTION_INGRESO_BAJA_ALTA,
            ]}
          >
            <ServiceSheet />
          </ProtectedRoute>
        ),
      },
      {
        path: "ingresos/:id/derivar",
        element: (
          <ProtectedRoute permissions={PERMISSION_CODES.ACTION_INGRESO_MANAGE_DERIVATIONS}>
            <DerivarIngreso />
          </ProtectedRoute>
        ),
      },
    ],
  },
  { path: "*", element: <NotFound /> },
]);

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ErrorBoundary>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  try {
    navigator.serviceWorker.getRegistrations?.().then((regs) => {
      regs?.forEach((r) => r.unregister().catch(() => {}));
    });
    if (window.caches && caches.keys) {
      caches.keys().then((keys) => keys.forEach((k) => caches.delete(k)));
    }
  } catch (_) {}
}

if (import.meta.env.PROD && import.meta.env.VITE_SW === "1" && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

