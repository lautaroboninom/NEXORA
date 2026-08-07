import {
  deliveryOrderCommercialLabel,
  deliveryOrderItemsSummary,
} from "./delivery-orders";

export const DELIVERY_ORDER_SORT_COLUMNS = {
  orden: { accessor: (row) => row?.orderNumber ?? row?.order_number },
  cliente: { accessor: (row) => row?.customerName ?? row?.customer_name },
  tipo: { accessor: (row) => row?.deliveryType ?? row?.delivery_type },
  estado: { accessor: (row) => row?.status },
  articulos: { accessor: (row) => deliveryOrderItemsSummary(row).primary },
  comercial: { accessor: deliveryOrderCommercialLabel },
  remito: { accessor: (row) => row?.remitoNumber ?? row?.remito_number },
  factura: { accessor: (row) => row?.invoiceNumber ?? row?.invoice_number },
  fecha: {
    accessor: (row) =>
      row?.createdAt ??
      row?.created_at ??
      row?.orderDate ??
      row?.order_date,
    type: "date",
  },
};

export const DELIVERY_ORDER_SORT_OPTIONS = [
  { value: "orden", label: "Orden" },
  { value: "cliente", label: "Cliente" },
  { value: "tipo", label: "Tipo" },
  { value: "estado", label: "Estado" },
  { value: "articulos", label: "Artículos" },
  { value: "comercial", label: "Comercial" },
  { value: "remito", label: "Remito" },
  { value: "factura", label: "Factura" },
  { value: "fecha", label: "Fecha" },
];

