import { openDetachedWindow, openPrintablePdf, reservePdfWindow, waitForPdfBlob } from "./pdf";

export function risRemitoFrom(source = {}) {
  const ris = source?.ris || source || {};
  return (
    ris?.remito_number ||
    ris?.remitoNumber ||
    source?.remito_number ||
    source?.remitoNumber ||
    source?.remito_ingreso ||
    ""
  ).toString().trim();
}

export function risDocumentTypeFrom(source = {}) {
  const ris = source?.ris || source || {};
  const profile =
    ris?.document_profile ||
    ris?.documentProfile ||
    source?.document_profile ||
    source?.documentProfile ||
    source?.preview?.document_profile ||
    source?.preview?.documentProfile ||
    {};
  return String(profile?.type || ris?.comprobante_tipo || source?.comprobante_tipo || "").trim().toUpperCase();
}

export function risDocumentLabelFrom(source = {}) {
  const documentType = risDocumentTypeFrom(source);
  return documentType ? `remito ${documentType}` : "remito";
}

const documentReasonKey = (value) =>
  String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");

export function documentNameFromRis(source = {}, fallback = "RIS") {
  const documentType = risDocumentTypeFrom(source);
  if (documentType) return documentType;
  const ris = source?.ris || source || {};
  const previewItem = Array.isArray(ris?.preview?.items) ? ris.preview.items[0] : null;
  const reason = documentReasonKey(
    ris?.motivo ||
      source?.motivo ||
      ris?.repairReason ||
      source?.repairReason ||
      previewItem?.repairReason,
  );
  if (reason === "baja alquiler" || reason === "reparacion alquiler") return "RDA";
  if (reason === "devolucion demo") return "RDN";
  return String(fallback || "RIS").trim() || "RIS";
}

export function isRisRegistered(source = {}) {
  const ris = source?.ris || source || {};
  const documentMode = String(ris?.document_mode || ris?.documentMode || source?.document_mode || source?.documentMode || "").toLowerCase();
  return documentMode === "register";
}

export function isRisGenerated(source = {}) {
  const ris = source?.ris || source || {};
  if (isRisRegistered(source)) return false;
  return Boolean(risRemitoFrom(source) && String(ris?.status || "").toLowerCase() === "generated");
}

export function risPrintUrlFor(ingresoId, source = {}) {
  const ris = source?.ris || {};
  const direct = source?.print_url || source?.printUrl || ris?.print_url || ris?.printUrl || "";
  if (direct) return String(direct);
  if (!ingresoId) return "";
  return `/api/ingresos/${encodeURIComponent(ingresoId)}/ris/print/`;
}

export function risPdfUrlFor(ingresoId, source = {}) {
  const ris = source?.ris || {};
  const direct = source?.pdf_url || source?.pdfUrl || ris?.pdf_url || ris?.pdfUrl || "";
  if (direct) return String(direct);
  if (!ingresoId) return "";
  return `/api/ingresos/${encodeURIComponent(ingresoId)}/ris/pdf/`;
}

export function waitForRisPdfBlob(ingresoId, source = {}, options = {}) {
  return waitForPdfBlob(risPdfUrlFor(ingresoId, source), {
    label: risDocumentLabelFrom(source),
    ...options,
  });
}

export function openRisPrintablePdf(blob, source = {}, options = {}) {
  const remito = risRemitoFrom(source);
  const documentLabel = remito ? `remito ${remito}` : risDocumentLabelFrom(source);
  return openPrintablePdf(blob, {
    title: documentLabel,
    documentLabel,
    ...options,
  });
}

export function reserveRisPrintWindow(options = {}) {
  return reservePdfWindow({
    title: "REMITO",
    message: "Preparando remito...",
    fallbackMessage:
      "La emisión sigue en NEXORA. Si el remito ya se imprimió, puede cerrar esta pestaña y reimprimir desde la hoja de servicio.",
    ...options,
  });
}

export function openRisPrintUrl(printUrl, reservedWindow = null) {
  const href = String(printUrl || "").trim();
  if (!href) return false;
  if (reservedWindow) return reservedWindow.openUrl(href);
  return openDetachedWindow(href);
}

export function openGeneratedRisPrint(ingresoId, source = {}) {
  const printUrl = risPrintUrlFor(ingresoId, source);
  return {
    opened: openRisPrintUrl(printUrl),
    printUrl,
    remito: risRemitoFrom(source),
  };
}
