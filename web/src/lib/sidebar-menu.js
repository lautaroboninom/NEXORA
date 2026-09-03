// web/src/lib/sidebar-menu.js
//
// Recorte del menú lateral para el técnico.
//
// Es sólo la lista de atajos: esconder un ítem acá NO le saca ningún permiso.
// La página sigue siendo alcanzable por URL y el backend sigue siendo el que
// autoriza, así que un recorte de más se corrige sin tocar permisos ni arriesgar
// dejar a alguien afuera de una pantalla que sí necesita.

// Extensión explícita: sin ella `node --test` no resuelve el módulo.
import { isTecnico } from "./authz.js";
import { can, PERMISSION_CODES, hasCustomPermissions } from "./permissions.js";

// El técnico entra a NEXORA a reparar. Con los permisos por defecto de su rol el
// menú completo le mostraba 14 pantallas: las 9 que sobran son de supervisión
// (Pendientes por técnico), de liberación y administración (Listos para liberar,
// Liberados, Histórico ingresos, General por cliente) o de logística
// (Stock / Depósitos, Derivados, Equipos, Buscar accesorio). No las usa en el
// día a día y le tapaban sus tres colas de trabajo.
const TECH_SECTIONS = [
  {
    title: "Servicio técnico",
    // "Mis pendientes" va primero a propósito: es la única cola que es suya.
    items: [
      { to: "/tecnico", label: "Mis pendientes", permission: PERMISSION_CODES.PAGE_WORK_QUEUES },
      { to: "/pendientes", label: "Pendientes general", permission: PERMISSION_CODES.PAGE_WORK_QUEUES },
      {
        to: "/pendientes-presupuesto",
        label: "Pendientes de presupuesto",
        permission: PERMISSION_CODES.PAGE_BUDGET_QUEUES,
      },
      {
        to: "/presupuestados",
        label: "Presupuestados",
        permission: PERMISSION_CODES.PAGE_BUDGET_QUEUES,
      },
      {
        to: "/pendientes-reparacion",
        label: "Pendientes de reparación",
        permission: PERMISSION_CODES.PAGE_WORK_QUEUES,
      },
    ],
  },
  {
    title: "Taller",
    items: [
      { to: "/catalogo/repuestos", label: "Repuestos", permission: PERMISSION_CODES.PAGE_SPARE_PARTS },
      { to: "/buscar-ns", label: "Buscar NS", permission: PERMISSION_CODES.PAGE_HOME_SEARCH },
    ],
  },
];

// Sólo el rol `tecnico` puro y SIN permisos personalizados. Si al técnico se le
// editaron permisos para habilitarle pantallas adicionales (ej. presupuestos,
// recepción u otras áreas), debe ver el menú completo según los permisos
// configurados para ese usuario.
export const usesTechMenu = (user) => isTecnico(user) && !hasCustomPermissions(user);

// Las secciones ya cruzadas con los permisos del usuario: una sección que se
// queda sin ítems no se devuelve, para que no aparezca el título colgado.
export function techSidebarSections(user) {
  return TECH_SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter((item) => can(user, item.permission)),
  })).filter((section) => section.items.length > 0);
}
