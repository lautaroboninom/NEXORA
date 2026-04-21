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

// Parseador seguro para fechas "YYYY-MM-DD": trtalas como hora local 00:00
export const parseDateLocal = (s) => {
  if (!s) return null;
  if (typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s)) {
    return new Date(`${s}T00:00:00`);
  }
  return new Date(s);
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

  // Fallback histrico: serie/variante consolidado
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

// Devuelve la etiqueta de serie priorizando el nmero interno (MG) si existe.
// Reglas:
//  - Si hay MG (numero_interno) -> mostrar MG
//  - Si no hay MG pero hay N/S (numero_serie) -> mostrar N/S
//  - Si no hay ninguno -> fallback ("-")
export const nsPreferInternoOf = (row, fallback = "-") => {
  if (!row) return fallback;
  const str = (v) => (v == null ? "" : String(v).trim());
  const interno =
    str(row?.numero_interno) ||
    str(row?.equipo?.numero_interno);
  if (interno) return interno;
  const serie = str(row?.numero_serie) || str(row?.equipo?.numero_serie);
  return serie || fallback;
};
export const norm = (v) => {
  const s = (v ?? "").toString().toLowerCase().trim();
  try {
    // Remover acentos/diacrticos para comparaciones robustas
    return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  } catch {
    return s;
  }
};

export const isMotivoCotizacionEquipo = (motivo) => {
  const key = norm(motivo).replace(/\s+/g, " ");
  return key === "cotizacion de equipo";
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
