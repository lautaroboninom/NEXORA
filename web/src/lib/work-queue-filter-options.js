import { norm, tipoEquipoOf } from "./ui-helpers.js";
import {
  WORK_QUEUE_EMPTY_FILTER,
  workQueueNextAction,
  workQueueTechnician,
} from "./work-queue.js";

export const WORK_QUEUE_PRIORITY_OPTIONS = [
  { value: "devuelto", label: "Devuelto" },
  { value: "urgente", label: "Urgente" },
  { value: "presupuesto", label: "Presupuesto" },
  { value: "sin_tecnico", label: "Sin técnico" },
  { value: "16_mas", label: "16+ días" },
  { value: "espera", label: "En espera" },
  { value: "normal", label: "Normal" },
];

export const WORK_QUEUE_AGE_OPTIONS = [
  { value: "0_6", label: "0 a 6 días" },
  { value: "7_15", label: "7 a 15 días" },
  { value: "16_30", label: "16 a 30 días" },
  { value: "31_mas", label: "31 días o más" },
];

export const WORK_QUEUE_PROPERTY_OPTIONS = [
  { value: "MG BIO", label: "MG BIO" },
  { value: "Cliente", label: "Cliente" },
  { value: "Cliente (MG histórico)", label: "Cliente (MG histórico)" },
];

export function uniqueOptions(rows, accessor, emptyLabel = "") {
  const values = new Map();
  for (const row of rows || []) {
    const raw = accessor(row);
    const value = String(raw ?? "").trim();
    if (!value) continue;
    const key = norm(value);
    if (!values.has(key)) values.set(key, value);
  }
  const options = Array.from(values.values())
    .sort((a, b) => a.localeCompare(b, "es-AR", { numeric: true, sensitivity: "base" }))
    .map((value) => ({ value, label: value }));
  if (emptyLabel) options.unshift({ value: WORK_QUEUE_EMPTY_FILTER, label: emptyLabel });
  return options;
}

/**
 * Opciones de los filtros de la cola de trabajo.
 *
 * Prioridad, antigüedad y propiedad son listas cerradas; el resto sale de las
 * filas que hay cargadas, así el combo no ofrece valores que no filtran nada.
 */
export function workQueueFilterOptions(rows) {
  return {
    prioridad: WORK_QUEUE_PRIORITY_OPTIONS,
    antiguedad: WORK_QUEUE_AGE_OPTIONS,
    propiedad: WORK_QUEUE_PROPERTY_OPTIONS,
    tipo: uniqueOptions(rows, tipoEquipoOf),
    marca: uniqueOptions(rows, (row) => row?.marca ?? row?.equipo?.marca),
    modelo: uniqueOptions(rows, (row) => row?.modelo ?? row?.equipo?.modelo),
    variante: uniqueOptions(
      rows,
      (row) => row?.equipo_variante ?? row?.variante ?? row?.equipo?.variante
    ),
    tecnico: uniqueOptions(rows, workQueueTechnician, "Sin técnico"),
    motivo: uniqueOptions(rows, (row) => row?.motivo),
    estado: uniqueOptions(rows, (row) => row?.estado),
    presupuesto: uniqueOptions(rows, (row) => row?.presupuesto_estado, "Sin presupuesto"),
    accion: uniqueOptions(rows, workQueueNextAction),
  };
}
