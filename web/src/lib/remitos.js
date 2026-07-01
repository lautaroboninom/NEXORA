export const REGISTERED_REMITO_NO_PDF_REASON =
  "Remito registrado manualmente; no genera PDF de Bejerman.";

export const REGISTERED_NO_PDF_CODES = new Set([
  "BEJERMAN_REMITO_REGISTERED_NO_PDF",
  "RIS_REGISTERED_NO_PDF",
]);

export function registeredNoPdfMessage(label = "remito") {
  return `Este ${label} fue registrado manualmente en NEXORA, no emitido. Los remitos registrados no generan PDF de Bejerman para abrir o imprimir desde NEXORA.`;
}

export function isRegisteredNoPdfCode(code) {
  return REGISTERED_NO_PDF_CODES.has(String(code || ""));
}

export function trimRemitoValue(value) {
  return String(value ?? "").trim();
}

function valueOf(source, keys) {
  for (const key of keys) {
    const value = trimRemitoValue(source?.[key]);
    if (value) return value;
  }
  return "";
}

export function remitoDocumentLabel(source, fallback = "Sin número") {
  const direct = valueOf(source, [
    "remito_number",
    "remitoNumber",
    "manual_remito_number",
    "manualRemitoNumber",
    "documentNumber",
    "numero",
  ]);
  if (direct) return direct;
  const parts = [
    valueOf(source, ["comprobante_tipo", "comprobanteTipo", "type"]),
    valueOf(source, ["comprobante_letra", "comprobanteLetra", "letter"]),
    valueOf(source, ["comprobante_pto_venta", "comprobantePtoVenta", "pointOfSale"]),
    valueOf(source, ["comprobante_numero", "comprobanteNumero", "number"]),
  ].filter(Boolean);
  return parts.length ? parts.join(" ") : fallback;
}

export function remitoPrintableLabel(source, fallback = "remito") {
  const label = remitoDocumentLabel(source, "");
  return label && label !== "Sin número" ? label : fallback;
}

export function remitoDocumentNumber(source, fallback = "-") {
  return valueOf(source, ["documentNumber", "numero", "number", "comprobanteNumero"]) || fallback;
}

export function remitoPdfUnavailableReason(source) {
  const explicit = valueOf(source, ["pdf_unavailable_reason", "pdfUnavailableReason"]);
  if (explicit) return explicit;
  const mode = valueOf(source, ["document_mode", "documentMode"]).toLowerCase();
  if (["register", "registered", "registrar", "registrado"].includes(mode)) {
    return REGISTERED_REMITO_NO_PDF_REASON;
  }
  return "";
}
