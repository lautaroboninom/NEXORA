// web/src/lib/ui-helpers.js

export const ingresoIdOf = (row) => row?.ingreso_id ?? row?.id;

export const formatOS = (rowOrId, prefix = "") => {
  // Acepta objeto row o un id suelto
  if (rowOrId && typeof rowOrId === "object") {
    const id = ingresoIdOf(rowOrId) ?? 0;
    return rowOrId?.os ?? `${prefix}${String(id).padStart(5, "0")}`;
  }
  const id = Number(rowOrId ?? 0);
  return `${prefix}${String(id).padStart(5, "0")}`;
};

export const formatDateTime = (s, locale = "es-AR") =>
  s ? new Date(s).toLocaleDateString(locale, { dateStyle: "short" }) : "-";

export const resolveFechaIngreso = (row) => row?.fecha_ingreso ?? row?.fecha_creacion ?? null;
export const resolveFechaCreacion = (row) => row?.fecha_creacion ?? row?.fecha_ingreso ?? null;

// Parseador seguro para fechas "YYYY-MM-DD": tratarlas como hora local 00:00
export const parseDateLocal = (s) => {
  if (!s) return null;
  if (typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s)) {
    return new Date(`${s}T00:00:00`);
  }
  return new Date(s);
};

// Para inputs de fecha civil: no desplazar el día al convertir a UTC.
export const localDateInputValue = (date = new Date()) => {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
};

export const formatDateOnly = (s, locale = "es-AR") => {
  const d = parseDateLocal(s);
  return d ? d.toLocaleDateString(locale, { dateStyle: "short" }) : "-";
};

export const modeloSerieVarianteOf = (row, fallback = "-") => {
  if (!row) return fallback;
  const str = (v) => (typeof v === "string" ? v.trim() : "");
  const firstNonEmpty = (...vals) => vals.map(str).find((x) => !!x);

  // Preferir: Modelo + Variante
  const modelo = firstNonEmpty(
    row?.modelo,
    row?.equipo?.modelo,
    row?.modelo_nombre,
    row?.equipo?.modelo_nombre
  );
  const variante = firstNonEmpty(
    row?.equipo_variante,
    row?.equipo?.variante,
    row?.modelo_variante,
    row?.variante,
    row?.variante_nombre
  );
  if (modelo) {
    return [modelo, variante].filter(Boolean).join(" ").trim();
  }

  // Fallback histórico: serie/variante consolidado
  const serie = firstNonEmpty(
    row?.modelo_serie_variante,
    row?.modelo_serie,
    row?.serie_nombre
  );
  const alt = [serie, variante].filter(Boolean).join(" ").trim();
  if (alt) return alt;

  return fallback;
};

export const tipoEquipoOf = (row, fallback = "-") => {
  if (!row) return fallback;
  const candidates = [
    row?.tipo_equipo,
    row?.equipo?.tipo_equipo,
    row?.tipo_equipo_nombre,
    row?.equipo?.tipo_equipo_nombre,
    row?.tipo,
    row?.equipo?.tipo,
    row?.tipoEquipo,
    row?.equipo?.tipoEquipo,
    row?.modelo_tipo,
    row?.equipo?.modelo_tipo,
  ];
  for (const raw of candidates) {
    if (typeof raw === "string") {
      const value = raw.trim();
      if (value) return value;
    }
  }
  return fallback;
};

export const catalogEquipmentLabel = (row, fallback = "-") => {
  if (!row) return fallback;
  const tipo = tipoEquipoOf(row, "").toString().trim();
  const marca = (row?.marca || row?.equipo?.marca || "").toString().trim();
  const modelo = modeloSerieVarianteOf(row, "").toString().trim();
  const parts = [tipo, marca, modelo].filter((part) => part);
  return parts.length ? parts.join(" | ") : fallback;
};

export const SALE_TICKET_STATES = new Set(["vendido_pendiente_entrega", "vendido_entregado"]);
// Número interno de MG BIO: pegado o separado, con o sin ceros a la izquierda y hasta 6 dígitos
// ("MG1678", "MG 001678"). Espejo de `service/mg_codes.py`: si cambia uno, cambia el otro.
export const MG_CODE_RE = /^(MG|NM|NV|CE)\s*0*\d{1,6}$/i;
const cleanText = (v) => (v == null ? "" : String(v).trim());

export const locationNameKey = (value) =>
  cleanText(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("es-AR")
    .replace(/\s+/g, " ");

const SEPID_KEYS = new Set([
  "sepid",
  "deposito sepid",
  "deposito sepid s.a.",
  "deposito sepid sa",
  "sepid s.a.",
  "sepid sa",
  "deposito sepid (val)",
  "deposito sepid val",
  "sepid (val)",
  "sepid val",
  "deposito central",
  "deposito central sepid",
  "valdenegro",
  "deposito valdenegro",
  "valdenegro 4578",
]);

export const isSepidLocationName = (value) => {
  const key = locationNameKey(value);
  if (!key) return false;
  return (
    SEPID_KEYS.has(key) ||
    key === "sepid" ||
    key.startsWith("sepid") ||
    key.includes("sepid") ||
    key.includes("valdenegro")
  );
};

export const canonicalLocationName = (value) => {
  const key = locationNameKey(value);
  if (!key) return cleanText(value);
  if (isSepidLocationName(value)) return "Sepid";
  if (key === "sarmiento" || key.includes("sarmiento")) return "Sarmiento";
  if (key === "taller" || key.includes("taller")) return "Taller";
  if (key.includes("estanter") && key.includes("alquiler")) return "Estantería de Alquiler";
  if (key === "-") return "-";
  return cleanText(value);
};

export const bejermanDepositForLocation = (value) => {
  const canonical = canonicalLocationName(value);
  if (canonical === "Taller") return "STR";
  if (canonical === "Sarmiento") return "SAR";
  if (canonical === "Estantería de Alquiler") return "STL";
  if (canonical === "Sepid") return "VAL";
  return "";
};

export const locationDisplayName = (value) => {
  const canonical = canonicalLocationName(value);
  if (canonical === "Sarmiento") return "Sarmiento (SAR)";
  if (canonical === "Sepid") return "Sepid (VAL)";
  return canonical || cleanText(value);
};

export const isSaleTicketState = (value) =>
  SALE_TICKET_STATES.has(String(value ?? "").trim().toLowerCase());

export const isMgInactiveBySale = (row) => {
  if (!row) return false;
  const str = (v) => (v == null ? "" : String(v).trim());
  const mgEstado = str(row?.mg_estado || row?.equipo?.mg_estado).toLowerCase();
  const estado = str(row?.estado || row?.equipo?.estado).toLowerCase();
  return (
    Boolean(row?.mg_inactivo_venta || row?.equipo?.mg_inactivo_venta || row?.vendido || row?.equipo?.vendido)
    || mgEstado === "inactivo_venta"
    || isSaleTicketState(estado)
  );
};

export const hasMgCode = (row) => {
  if (!row) return false;
  if (row?.tiene_codigo_mg || row?.equipo?.tiene_codigo_mg) return true;
  return [
    row?.numero_interno,
    row?.equipo?.numero_interno,
    row?.numero_serie,
    row?.equipo?.numero_serie,
  ].some((value) => MG_CODE_RE.test(cleanText(value)));
};

/** ¿Ese nombre de ubicación es la estantería de alquiler?
 *
 * Mismo criterio que `is_rental_shelf_location_name` en el backend: el nombre convivió
 * escrito de varias maneras -con y sin acento, con y sin "de"- y los ids de ubicación
 * son datos de instancia.
 */
export const isRentalShelfLocationName = (value) => {
  const text = locationNameKey(value);
  return text.includes("estanter") && text.includes("alquiler");
};

export const isMgOwned = (row) => {
  if (!row || isMgInactiveBySale(row)) return false;
  if (row?.es_propietario_mg === true || row?.equipo?.es_propietario_mg === true) return true;
  if (row?.es_cliente_mg_owner === true || row?.equipo?.es_cliente_mg_owner === true) return true;
  return hasMgCode(row);
};

// El enum de motivos convivió con y sin tilde (hay un comando de corrección de etiquetas
// para eso), así que se aceptan las dos escrituras en vez de depender de cuál quedó.
const DEMO_RETURN_MOTIVOS = new Set(["devolución demo", "devolucion demo"]);

/** ¿El equipo entró por una devolución de demo? */
export const isDemoReturn = (row) => {
  if (!row) return false;
  if (row?.es_devolucion_demo === true || row?.equipo?.es_devolucion_demo === true) return true;
  return [row?.motivo, row?.equipo?.motivo, row?.motivo_ingreso].some((value) =>
    DEMO_RETURN_MOTIVOS.has(cleanText(value).toLowerCase())
  );
};

export const propiedadEquipoLabelOf = (row) => {
  if (!row) return "-";
  if (hasMgCode(row) && isMgInactiveBySale(row)) return "Cliente (MG histórico)";
  if (isMgOwned(row)) return "MG BIO";
  // Un equipo que vuelve de una demo es nuestro, aunque el ingreso siga mostrando al
  // cliente que lo devolvió: por eso su stock vuelve a VAL y no al depósito del cliente.
  // Si ya se vendió, la propiedad pasó al comprador y vuelve a leerse como "Cliente".
  if (isDemoReturn(row) && !isMgInactiveBySale(row)) return "MG BIO";
  return "Cliente";
};

// `includeHistoricalMg` es para las vistas de detalle de una OS/equipo, donde el
// MG viejo sigue siendo trazabilidad útil. Los listados usan el default.
export const deviceIdentifierPartsOf = (row, fallback = "-", { includeHistoricalMg = false } = {}) => {
  if (!row) {
    return {
      primary: fallback,
      secondary: "",
      identifiers: [],
      sold: false,
      clientOwned: false,
      numeroSerie: "",
      numeroInterno: "",
      numeroAlternativo: "",
    };
  }
  const str = (v) => (v == null ? "" : String(v).trim());
  const sold = isMgInactiveBySale(row);
  const mgOwned = isMgOwned(row);
  const clientOwned = !mgOwned;
  const interno =
    str(row?.numero_interno) ||
    str(row?.equipo?.numero_interno);
  const serie = str(row?.numero_serie) || str(row?.equipo?.numero_serie);
  const alternativo =
    str(row?.mg_venta_numero_alternativo) ||
    str(row?.equipo?.mg_venta_numero_alternativo) ||
    str(row?.numero_alternativo) ||
    str(row?.equipo?.numero_alternativo);
  const historicalMg = sold && Boolean(interno);
  // Un MG histórico ya no identifica al equipo: se vendió y el número interno
  // dejó de ser suyo. En los listados sólo confunde al lado del N/S del cliente,
  // así que se esconde mientras quede algún otro identificador para mostrar.
  const hideHistoricalMg =
    historicalMg && !includeHistoricalMg && Boolean(serie || alternativo);
  const primary = (sold || clientOwned)
    ? (serie || alternativo || interno || fallback)
    : (interno || serie || fallback);
  const isMgOwner = mgOwned && !sold;
  const ownerLabel = isMgOwner ? "propio" : "cliente";
  const internalOwnerLabel = sold ? "MG histórico" : ownerLabel;
  const identifiers = [
    serie ? { label: `N/S (${ownerLabel})`, value: serie, kind: "serie" } : null,
    interno && !hideHistoricalMg
      ? { label: `N° interno (${internalOwnerLabel})`, value: interno, kind: "interno" }
      : null,
    alternativo ? { label: "N° alternativo (cliente)", value: alternativo, kind: "alternativo" } : null,
  ].filter(Boolean);
  return {
    primary,
    secondary: historicalMg && !hideHistoricalMg ? `MG histórico: ${interno}` : "",
    identifiers,
    sold,
    clientOwned,
    numeroSerie: serie,
    numeroInterno: interno,
    numeroAlternativo: alternativo,
  };
};

// Devuelve la etiqueta principal para tablas/listados.
// MG propio activo: prioriza número interno. Cliente o MG histórico vendido: prioriza N/S.
export const nsPreferInternoOf = (row, fallback = "-") => {
  return deviceIdentifierPartsOf(row, fallback).primary;
};

// "Particular" y "MG BIO" son clientes contenedores: cuando el equipo tiene dueño
// propio cargado aparte, el nombre que le interesa al operador es el del dueño.
const isOwnerDisplayCustomer = (value) => {
  const normalized = String(value || "").trim().toLowerCase();
  if (!normalized) return false;
  if (normalized === "particular") return true;
  return normalized.includes("mg") && normalized.includes("bio");
};

// `razon_social` cubre el detalle de ingreso; las filas de equipo traen
// last_customer_nombre / customer_nombre y algunos listados, `cliente`.
const deviceCustomerName = (row) =>
  String(
    row?.last_customer_nombre ||
      row?.customer_nombre ||
      row?.razon_social ||
      row?.cliente ||
      row?.cliente_nombre ||
      ""
  ).trim();

/**
 * Nombre a mostrar en la columna Cliente de los listados de ingresos.
 *
 * Devuelve cadena vacía si no hay nada, para que los filtros puedan ofrecer
 * "(sin dato)". Para mostrarlo directo usar `deviceCustomerTitle`.
 */
export const ingresoCustomerName = (row) => {
  const customer = deviceCustomerName(row);
  const owner = String(row?.propietario_nombre || "").trim();
  if (owner && isOwnerDisplayCustomer(customer)) return owner;
  return customer;
};

/**
 * Nombre del cliente contenedor cuando quedó tapado por el del dueño.
 *
 * Devuelve "" si el título ya es el cliente. En los listados sólo interesa este
 * caso: `deviceCustomerSubtitle` además avisa "Dueño: X" sobre clientes reales,
 * y eso, fila por fila, es ruido.
 */
export const ingresoCustomerBaseName = (row) => {
  const customer = deviceCustomerName(row);
  const owner = String(row?.propietario_nombre || "").trim();
  return owner && isOwnerDisplayCustomer(customer) ? customer : "";
};

export const deviceCustomerTitle = (row) =>
  ingresoCustomerName(row) || String(row?.propietario_nombre || "").trim() || "-";

export const deviceCustomerSubtitle = (row) => {
  const customer = deviceCustomerName(row);
  const owner = String(row?.propietario_nombre || "").trim();
  if (owner && customer && isOwnerDisplayCustomer(customer)) {
    return `Cliente base: ${customer}`;
  }
  if (owner && customer && owner.toLowerCase() !== customer.toLowerCase()) {
    return `Dueño: ${owner}`;
  }
  return "";
};

export const deviceOwnerMeta = (row) => {
  const parts = [row?.propietario_contacto, row?.propietario_doc]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  return parts.join(" | ");
};

export const deviceModelTitle = (row) => {
  const modelo = String(row?.modelo || "").trim();
  const variante = String(row?.variante || "").trim();
  return [modelo, variante].filter(Boolean).join(" ").trim() || "-";
};

export const deviceBarcodeValue = (row) =>
  String(row?.numero_serie || row?.numero_interno || "").trim();
export const norm = (v) => {
  const s = (v ?? "").toString().toLowerCase().trim();
  try {
    // Remover acentos/diacríticos para comparaciones robustas
    return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  } catch {
    return s;
  }
};

export const isMotivoCotizacionEquipo = (motivo) => {
  const key = norm(motivo).replace(/\s+/g, " ");
  return key === "cotizacion de equipo";
};

export const isMotivoRevisionTecnica = (motivo) => {
  const key = norm(motivo).replace(/\s+/g, " ");
  return key === "revision tecnica";
};

export const formatMoney = (amount, currency = "ARS", locale = "es-AR") => {
  if (amount == null || isNaN(Number(amount))) return "-";
  try {
    return new Intl.NumberFormat(locale, { style: "currency", currency }).format(Number(amount));
  } catch {
    return new Intl.NumberFormat(locale).format(Number(amount));
  }
};

export const toNum = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
