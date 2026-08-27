import {
  catalogEquipmentLabel,
  ingresoIdOf,
  resolveFechaCreacion,
  resolveFechaIngreso,
  tipoEquipoOf,
} from "./ui-helpers.js";
import {
  workQueueIdentifier,
  workQueueProperty,
  workQueueTechnician,
} from "./work-queue.js";
import { resolutionLabel } from "./constants.js";

export const INGRESO_LIST_SORT_COLUMNS = {
  os: { accessor: ingresoIdOf, type: "number" },
  cliente: { accessor: (row) => row?.razon_social ?? row?.cliente ?? row?.cliente_nombre },
  equipo: { accessor: catalogEquipmentLabel },
  propiedad: { accessor: workQueueProperty },
  tipo: { accessor: tipoEquipoOf },
  marca: { accessor: (row) => row?.marca ?? row?.equipo?.marca },
  modelo: { accessor: (row) => row?.modelo ?? row?.equipo?.modelo },
  variante: {
    accessor: (row) =>
      row?.equipo_variante ?? row?.variante ?? row?.equipo?.variante ?? row?.modelo_variante,
  },
  identificacion: { accessor: workQueueIdentifier },
  numero_interno: { accessor: (row) => row?.numero_interno ?? row?.equipo?.numero_interno },
  numero_serie: { accessor: (row) => row?.numero_serie ?? row?.equipo?.numero_serie },
  tecnico: { accessor: workQueueTechnician },
  proveedor: { accessor: (row) => row?.proveedor ?? row?.proveedor_nombre },
  accesorio: { accessor: (row) => row?.accesorio_nombre },
  referencia: { accessor: (row) => row?.referencia },
  motivo: { accessor: (row) => row?.motivo },
  estado: { accessor: (row) => row?.estado },
  resolucion: { accessor: (row) => resolutionLabel(row?.resolucion ?? "") },
  presupuesto: { accessor: (row) => row?.presupuesto_estado },
  monto: {
    accessor: (row) => row?.presupuesto_monto ?? row?.presupuesto_total,
    type: "number",
  },
  fecha_ingreso: { accessor: resolveFechaIngreso, type: "date" },
  fecha_creacion: { accessor: resolveFechaCreacion, type: "date" },
  fecha_servicio: { accessor: (row) => row?.fecha_servicio, type: "date" },
  fecha_emision: {
    accessor: (row) => row?.presupuesto_fecha_emision ?? row?.fecha_emision,
    type: "date",
  },
  fecha_envio: {
    accessor: (row) => row?.presupuesto_fecha_envio ?? row?.fecha_envio,
    type: "date",
  },
  fecha_presupuestado: {
    accessor: (row) => row?.presupuesto_fecha_emision ?? row?.presupuesto_fecha_envio,
    type: "date",
  },
  fecha_aprobado: {
    accessor: (row) =>
      row?.fecha_aprobado ??
      row?.presupuesto_fecha_aprobacion ??
      row?.fecha_reparado ??
      row?.fecha_reparacion,
    type: "date",
  },
  fecha_reparado: {
    accessor: (row) =>
      row?.fecha_reparado ??
      row?.fecha_reparacion ??
      row?.reparado_fecha ??
      row?.estado_fecha,
    type: "date",
  },
  fecha_liberacion: { accessor: (row) => row?.fecha_liberacion ?? row?.fecha_listo, type: "date" },
  fecha_listo: {
    accessor: (row) =>
      row?.fecha_listo ??
      row?.fecha_entrega ??
      row?.fecha_reparado ??
      row?.fecha_estado ??
      row?.estado_fecha,
    type: "date",
  },
  fecha_entrega: { accessor: (row) => row?.fecha_entrega, type: "date" },
  fecha_derivacion: { accessor: (row) => row?.fecha_deriv, type: "date" },
  ubicacion: { accessor: (row) => row?.ubicacion_nombre ?? row?.ubicacion },
};

export const INGRESO_SORT_LABELS = {
  os: "OS",
  cliente: "Cliente",
  equipo: "Equipo",
  propiedad: "Propiedad",
  tipo: "Tipo",
  marca: "Marca",
  modelo: "Modelo",
  variante: "Variante",
  identificacion: "N/S o MG",
  numero_interno: "N.º interno",
  numero_serie: "N.º serie",
  tecnico: "Técnico",
  proveedor: "Proveedor",
  accesorio: "Accesorio",
  referencia: "Referencia",
  motivo: "Motivo",
  estado: "Estado",
  resolucion: "Resolución",
  presupuesto: "Presupuesto",
  monto: "Monto",
  fecha_ingreso: "Fecha de ingreso",
  fecha_creacion: "Fecha de creación",
  fecha_servicio: "Fecha de servicio",
  fecha_emision: "Fecha de emisión",
  fecha_envio: "Fecha de envío",
  fecha_presupuestado: "Fecha presupuestado",
  fecha_aprobado: "Fecha de aprobación",
  fecha_reparado: "Fecha de reparación",
  fecha_liberacion: "Fecha de liberación",
  fecha_listo: "Fecha listo",
  fecha_entrega: "Fecha de entrega",
  fecha_derivacion: "Fecha de derivación",
  ubicacion: "Ubicación",
};

export function ingresoSortOptions(fields) {
  return (fields || []).map((field) => ({
    value: field,
    label: INGRESO_SORT_LABELS[field] || field,
  }));
}
