import { Link, useLocation } from "react-router-dom";

const TABS = [
  { key: "tecnicos", label: "Técnicos", to: "/metricas" },
  { key: "actividad", label: "Actividad técnicos", to: "/metricas/actividad-tecnicos" },
  { key: "clientes", label: "Clientes", to: "/metricas/clientes" },
  { key: "finanzas", label: "Finanzas", to: "/metricas/finanzas" },
];

export default function MetricasNav({ extraRight }) {
  const { pathname } = useLocation();
  const section = pathname.startsWith("/metricas/actividad-tecnicos")
    ? "actividad"
    : pathname.startsWith("/metricas/clientes")
    ? "clientes"
    : pathname.startsWith("/metricas/finanzas")
      ? "finanzas"
      : "tecnicos";

  return (
    <div className="border-b mb-4 flex items-center">
      <div className="flex gap-2">
        {TABS.map((tab) => {
          const base = "px-3 py-2 rounded-t border";
          const active = "bg-white border-gray-200 border-b-0";
          const inactive = "text-gray-600 hover:text-black border-transparent";
          const className = `${base} ${section === tab.key ? active : inactive}`;
          return (
            <Link key={tab.to} to={tab.to} className={className}>
              {tab.label}
            </Link>
          );
        })}
      </div>
      <div className="ml-auto">{extraRight}</div>
    </div>
  );
}

