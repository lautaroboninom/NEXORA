import {
  catalogEquipmentLabel,
  formatOS,
  hasMgCode,
  ingresoIdOf,
  isMgInactiveBySale,
  isMgOwned,
  norm,
  parseDateLocal,
  resolveFechaCreacion,
  resolveFechaIngreso,
  tipoEquipoOf,
} from "./ui-helpers.js";

export const WORK_QUEUE_EMPTY_FILTER = "__none__";

export function workQueueAgeDays(row) {
  const raw = resolveFechaIngreso(row) || resolveFechaCreacion(row);
  const date = parseDateLocal(raw);
  if (!date || Number.isNaN(date.getTime())) return null;
  const start = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.max(0, Math.floor((today - start) / 86400000));
}

export function workQueueTechnician(row) {
  return (
    row?.tecnico ||
    row?.tecnico_nombre ||
    row?.asignado_a_nombre ||
    row?.asignado_nombre ||
    ""
  );
}

export function workQueuePriority(row) {
  const days = workQueueAgeDays(row);
  const motivo = String(row?.motivo || "").toLowerCase();
  const estado = String(row?.estado || "").toLowerCase();
  const presupuesto = String(row?.presupuesto_estado || "").toLowerCase();
  if (row?.derivado_devuelto) return { label: "Devuelto", tone: "blue", rank: 0, value: "devuelto" };
  if (motivo === "urgente control") return { label: "Urgente", tone: "red", rank: 1, value: "urgente" };
  if (presupuesto === "presupuestado") return { label: "Presupuesto", tone: "amber", rank: 2, value: "presupuesto" };
  if (!row?.asignado_a && !workQueueTechnician(row) && estado !== "liberado") {
    return { label: "Sin técnico", tone: "amber", rank: 3, value: "sin_tecnico" };
  }
  if (days != null && days >= 16) return { label: "16+ días", tone: "red", rank: 4, value: "16_mas" };
  if (days != null && days >= 7) return { label: "En espera", tone: "amber", rank: 5, value: "espera" };
  return { label: "Normal", tone: "gray", rank: 9, value: "normal" };
}

export function workQueueNextAction(row) {
  const estado = String(row?.estado || "").toLowerCase();
  const presupuesto = String(row?.presupuesto_estado || "").toLowerCase();
  if (row?.next_action) return row.next_action;
  if (row?.derivado_devuelto) return "Revisar devolución";
  if (!row?.asignado_a && !workQueueTechnician(row) && estado !== "liberado") return "Asignar técnico";
  if (estado === "ingresado") return "Diagnosticar";
  if (presupuesto === "presupuestado") return "Seguimiento del presupuesto";
  if (presupuesto === "aprobado" || estado === "reparar") return "Reparar";
  if (["reparado", "controlado_sin_defecto", "no_se_repara"].includes(estado)) return "Liberar o entregar";
  if (estado === "liberado") return "Coordinar entrega";
  if (estado === "vendido_pendiente_entrega") return "Confirmar entrega de venta";
  return "Actualizar avance";
}

export function workQueueProperty(row) {
  if (hasMgCode(row) && isMgInactiveBySale(row)) return "Cliente (MG histórico)";
  if (isMgOwned(row)) return "MG BIO";
  return "Cliente";
}

export function workQueueIdentifier(row) {
  return (
    row?.numero_interno ||
    row?.equipo?.numero_interno ||
    row?.numero_serie ||
    row?.equipo?.numero_serie ||
    ""
  );
}

function matchesQueueSelect(value, selected) {
  if (!selected) return true;
  const raw = String(value ?? "").trim();
  if (selected === WORK_QUEUE_EMPTY_FILTER) return !raw;
  return norm(raw) === norm(selected);
}

function matchesQueueAge(row, selected) {
  if (!selected) return true;
  const days = workQueueAgeDays(row);
  if (days == null) return false;
  if (selected === "0_6") return days <= 6;
  if (selected === "7_15") return days >= 7 && days <= 15;
  if (selected === "16_30") return days >= 16 && days <= 30;
  if (selected === "31_mas") return days >= 31;
  return true;
}

export function matchesWorkQueueFilters(row, filters = {}) {
  const {
    q,
    prioridad,
    os,
    antiguedad,
    cliente,
    propiedad,
    tipo,
    marca,
    modelo,
    variante,
    identificacion,
    tecnico,
    motivo,
    estado,
    presupuesto,
    accion,
  } = filters;
  const needle = norm(q);
  if (needle) {
    const fields = [
      formatOS(row),
      row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
      row?.marca ?? row?.equipo?.marca,
      catalogEquipmentLabel(row),
      tipoEquipoOf(row),
      workQueueProperty(row),
      workQueueTechnician(row),
      row?.motivo,
      row?.estado,
      row?.presupuesto_estado,
      row?.numero_serie,
      row?.numero_interno,
      workQueueNextAction(row),
    ];
    if (!fields.some((field) => norm(field).includes(needle))) return false;
  }
  if (prioridad && workQueuePriority(row).value !== prioridad) return false;
  if (os && !norm(formatOS(row)).includes(norm(os))) return false;
  if (!matchesQueueAge(row, antiguedad)) return false;
  if (cliente && !norm(row?.razon_social ?? row?.cliente ?? row?.cliente_nombre).includes(norm(cliente))) return false;
  if (!matchesQueueSelect(workQueueProperty(row), propiedad)) return false;
  if (!matchesQueueSelect(tipoEquipoOf(row), tipo)) return false;
  if (!matchesQueueSelect(row?.marca ?? row?.equipo?.marca, marca)) return false;
  if (!matchesQueueSelect(row?.modelo ?? row?.equipo?.modelo, modelo)) return false;
  if (!matchesQueueSelect(row?.equipo_variante ?? row?.variante ?? row?.equipo?.variante, variante)) return false;
  if (
    identificacion &&
    ![workQueueIdentifier(row), row?.numero_serie, row?.numero_interno]
      .some((value) => norm(value).includes(norm(identificacion)))
  ) return false;
  if (!matchesQueueSelect(workQueueTechnician(row), tecnico)) return false;
  if (!matchesQueueSelect(row?.motivo, motivo)) return false;
  if (!matchesQueueSelect(row?.estado, estado)) return false;
  if (!matchesQueueSelect(row?.presupuesto_estado, presupuesto)) return false;
  if (!matchesQueueSelect(workQueueNextAction(row), accion)) return false;
  return true;
}

export const WORK_QUEUE_SORT_OPTIONS = [
  { value: "prioridad", label: "Prioridad" },
  { value: "os", label: "OS" },
  { value: "antiguedad", label: "Antigüedad" },
  { value: "cliente", label: "Cliente" },
  { value: "propiedad", label: "Propiedad" },
  { value: "tipo", label: "Tipo" },
  { value: "marca", label: "Marca" },
  { value: "modelo", label: "Modelo" },
  { value: "variante", label: "Variante" },
  { value: "identificacion", label: "N/S o MG" },
  { value: "tecnico", label: "Técnico" },
  { value: "motivo", label: "Motivo" },
  { value: "estado", label: "Estado" },
  { value: "presupuesto", label: "Presupuesto" },
  { value: "accion", label: "Próxima acción" },
];

export const WORK_QUEUE_SORT_COLUMNS = {
  prioridad: { accessor: (row) => workQueuePriority(row).rank, type: "number" },
  os: { accessor: (row) => ingresoIdOf(row), type: "number" },
  antiguedad: { accessor: workQueueAgeDays, type: "number" },
  cliente: { accessor: (row) => row?.razon_social ?? row?.cliente ?? row?.cliente_nombre },
  propiedad: { accessor: workQueueProperty },
  tipo: { accessor: tipoEquipoOf },
  marca: { accessor: (row) => row?.marca ?? row?.equipo?.marca },
  modelo: { accessor: (row) => row?.modelo ?? row?.equipo?.modelo },
  variante: {
    accessor: (row) =>
      row?.equipo_variante ?? row?.variante ?? row?.equipo?.variante ?? row?.modelo_variante,
  },
  identificacion: { accessor: workQueueIdentifier },
  tecnico: { accessor: workQueueTechnician },
  motivo: { accessor: (row) => row?.motivo },
  estado: { accessor: (row) => row?.estado },
  presupuesto: { accessor: (row) => row?.presupuesto_estado },
  accion: { accessor: workQueueNextAction },
};
