import { openDetachedWindow, openPrintablePdf, reservePdfWindow, waitForPdfBlob } from "./pdf";

const normalizedRemitoNumber = (value) => String(value || "").trim();

export function risRemitosFrom(source = {}) {
  const ris = source?.ris || source || {};
  const candidates = [source?.remitos, ris?.remitos].find(Array.isArray) || [];
  return candidates.filter((item) => item && typeof item === "object");
}

export function risRemitoNumbersFrom(source = {}) {
  const ris = source?.ris || source || {};
  const explicit = [
    source?.remito_numbers,
    source?.remitoNumbers,
    ris?.remito_numbers,
    ris?.remitoNumbers,
  ].find(Array.isArray) || [];
  const fromRemitos = risRemitosFrom(source).flatMap((item) => [
    item?.remito_number,
    item?.remitoNumber,
  ]);
  const direct = [
    ris?.remito_number,
    ris?.remitoNumber,
    source?.remito_number,
    source?.remitoNumber,
    source?.remito_ingreso,
  ];
  return [...explicit, ...fromRemitos, ...direct].reduce((numbers, value) => {
    const normalized = normalizedRemitoNumber(value);
    if (normalized && !numbers.includes(normalized)) numbers.push(normalized);
    return numbers;
  }, []);
}

export function risRemitoFrom(source = {}) {
  return risRemitoNumbersFrom(source)[0] || "";
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
  const remitoCount = risRemitoNumbersFrom(source).length;
  if (remitoCount > 1) return `lote de ${remitoCount} remitos`;
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
  const status = String(ris?.status || source?.status || "").toLowerCase();
  return documentMode === "register" && (!status || status === "generated");
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

export function risPrintableOptions(source = {}, options = {}) {
  const remitoNumbers = risRemitoNumbersFrom(source);
  const documentLabel = remitoNumbers.length > 1
    ? `lote de ${remitoNumbers.length} remitos`
    : remitoNumbers[0]
      ? `remito ${remitoNumbers[0]}`
      : risDocumentLabelFrom(source);
  return {
    title: documentLabel,
    documentLabel,
    ...options,
  };
}

export function openRisPrintablePdf(blob, source = {}, options = {}) {
  return openPrintablePdf(blob, risPrintableOptions(source, options));
}

export function openReservedRisPrintablePdf(reservedWindow, blob, source = {}, options = {}) {
  if (!reservedWindow) return openRisPrintablePdf(blob, source, options);
  return reservedWindow.openPrintable(blob, risPrintableOptions(source, options));
}

export function reserveRisPrintWindow(options = {}) {
  return reservePdfWindow({
    title: "Remito - NEXORA",
    heading: "Preparando el remito",
    message: "NEXORA está preparando el remito",
    detail: "Esta pestaña abre la impresión cuando el PDF esté listo.",
    fallbackMessage:
      "La emisión sigue en NEXORA. Aguarde un momento más.",
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
    remitos: risRemitoNumbersFrom(source),
  };
}
