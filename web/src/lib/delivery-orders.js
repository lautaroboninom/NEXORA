const clean = (value) => String(value ?? "").trim();

function parseNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number.parseFloat(String(value).replace(",", "."));
  return Number.isFinite(parsed) ? parsed : null;
}

export function deliveryOrderCompanyKey(order) {
  const explicit = clean(order?.companyKey).toUpperCase();
  if (explicit === "SEPID" || explicit === "MGBIO") return explicit;
  const marker = clean(order?.operationCompanyLabel || order?.sourceCompanyId).replace(/[-_\s]/g, "").toUpperCase();
  if (["MG", "MGB", "MGBI", "MGBIO", "MGBIOSA", "PORMG"].includes(marker) || marker.includes("MGBIO")) return "MGBIO";
  return "SEPID";
}

export function deliveryOrderCompanyLabel(order) {
  return deliveryOrderCompanyKey(order) === "MGBIO" ? "MG BIO" : "SEPID";
}

export function formatOrderQuantity(value) {
  const parsed = parseNumber(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return "1";
  if (Number.isInteger(parsed)) return String(parsed);
  return parsed.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

export function deliveryOrderPriceCurrency(order) {
  return clean(order?.priceCurrency).toUpperCase() === "USD" ? "USD" : "ARS";
}

export function deliveryOrderItemPriceCurrency(item, orderOrCurrency = "ARS") {
  const fallback = typeof orderOrCurrency === "string" ? orderOrCurrency : deliveryOrderPriceCurrency(orderOrCurrency);
  return clean(item?.priceCurrency).toUpperCase() === "USD" ? "USD" : fallback === "USD" ? "USD" : "ARS";
}

// Espejo de service/delivery_orders.remito_profile_for_type: cada tipo de entrega sale de un
// depósito distinto. Las reparaciones salen de STC (ST liberados) y no del depósito central:
// asumir VAL hace que la serie del equipo reparado no aparezca nunca en el stock consultado.
const DEPOSIT_BY_DELIVERY_TYPE = {
  sale: "VAL",
  demo: "VAL",
  rental: "STL",
  service_release: "STC",
};

/** Acepta el tipo de entrega o la orden entera, para no repetir el `order?.deliveryType`. */
export function deliveryOrderDefaultDeposit(deliveryTypeOrOrder) {
  const deliveryType =
    typeof deliveryTypeOrOrder === "string" ? deliveryTypeOrOrder : deliveryTypeOrOrder?.deliveryType;
  return DEPOSIT_BY_DELIVERY_TYPE[clean(deliveryType).toLowerCase()] || "VAL";
}

/** En una reparación la partida es la serie del equipo del cliente: no es intercambiable por
 * otra partida del mismo artículo, así que el stock del depósito no se ofrece como sugerencia. */
export function deliveryOrderPartidaIsFixedEquipment(deliveryTypeOrOrder) {
  const deliveryType =
    typeof deliveryTypeOrOrder === "string" ? deliveryTypeOrOrder : deliveryTypeOrOrder?.deliveryType;
  return clean(deliveryType).toLowerCase() === "service_release";
}

export function deliveryOrderItemRequiresPartida(item) {
  const value = item?.articleRequiresPartida ?? item?.requiresPartida;
  if (value === true || value === false) return value;
  const text = clean(value).toLowerCase();
  if (["1", "s", "si", "sí", "true", "yes", "y", "on"].includes(text)) return true;
  if (["0", "n", "no", "false", "off"].includes(text)) return false;
  return null;
}

export function deliveryOrderItemCanOmitPartida(order, item) {
  const deliveryType = clean(order?.deliveryType).toLowerCase() || "sale";
  return ["sale", "demo"].includes(deliveryType) && deliveryOrderItemRequiresPartida(item) === false;
}

function itemPartidaRows(item) {
  if (Array.isArray(item?.partidas) && item.partidas.length) return item.partidas;
  return clean(item?.partida) ? [item] : [];
}

export function deliveryOrderUsedPartidaKeys(items, itemIndex, partidaIndex, fallbackDeposit = "") {
  const itemList = Array.isArray(items) ? items : [];
  const targetItem = itemList[itemIndex] || {};
  const targetArticle = clean(targetItem.articleCode).toLowerCase();
  if (!targetArticle) return new Set();

  const targetRows = itemPartidaRows(targetItem);
  const targetRow = targetRows[partidaIndex] || {};
  const targetDeposit = clean(targetRow.stockDepositCode || targetItem.stockDepositCode || fallbackDeposit).toUpperCase();
  const used = new Set();

  itemList.forEach((item, candidateItemIndex) => {
    // Sólo se consideran renglones anteriores: son los que el operador ya cargó arriba.
    if (candidateItemIndex > itemIndex) return;
    if (clean(item?.articleCode).toLowerCase() !== targetArticle) return;

    itemPartidaRows(item).forEach((row, candidatePartidaIndex) => {
      if (candidateItemIndex === itemIndex && candidatePartidaIndex === partidaIndex) return;
      const partidaKey = clean(row?.partida).toLowerCase();
      if (!partidaKey) return;
      const rowDeposit = clean(row?.stockDepositCode || item?.stockDepositCode || fallbackDeposit).toUpperCase();
      if (targetDeposit && rowDeposit && rowDeposit !== targetDeposit) return;
      used.add(partidaKey);
    });
  });

  return used;
}

/** Por qué un NS con stock en STL no se puede alquilar, o "" si se puede.
 * El stock viene de Bejerman y el equipo de NEXORA, y no siempre coinciden: un remito manual que
 * no descontó STL deja stock de un equipo alquilado. El backend marca esos NS con
 * `rentalAvailable: false`; lo que no trae la marca (otro depósito, o si esa consulta falló) se
 * sigue ofreciendo y el guardado vuelve a validar. */
export function deliveryOrderRentalLotBlockReason(lot) {
  if (lot?.rentalAvailable !== false) return "";
  return clean(lot?.rentalBlockReason) || `El NS ${clean(lot?.partida)} no está disponible para alquilar.`;
}

export function deliveryOrderSplitRentalLots(lots) {
  const usable = [];
  const blocked = [];
  (Array.isArray(lots) ? lots : []).forEach((lot) => {
    (deliveryOrderRentalLotBlockReason(lot) ? blocked : usable).push(lot);
  });
  return { usable, blocked };
}

/** Cuántas partidas se piden al consultar el stock de un artículo en un depósito. Pedir 100
 * dejó afuera partidas reales de SAR (OE-00478: 186 Auto CPAP y 224 humidificadores) y el
 * recuadro las rechazaba como "no figura en el depósito". Espejo del tope del backend. */
export const DELIVERY_ORDER_STOCK_LOTS_LIMIT = 2000;

/** El lote de la partida escrita o escaneada, y si el listado prueba que falta.
 * Un listado recortado (`truncated`) no prueba nada: `missing` queda en false y la partida
 * la valida el guardado, que consulta el depósito completo. */
export function deliveryOrderFindStockLot(lots, partida, { truncated = false } = {}) {
  const key = clean(partida).toLowerCase();
  const lot = key
    ? (Array.isArray(lots) ? lots : []).find((candidate) => clean(candidate?.partida).toLowerCase() === key) || null
    : null;
  return { lot, missing: !lot && !truncated };
}

export function formatOrderMoney(value, currency = "ARS") {
  const parsed = parseNumber(value);
  if (parsed === null) return "-";
  const normalizedCurrency = String(currency || "").toUpperCase() === "USD" ? "USD" : "ARS";
  return parsed.toLocaleString("es-AR", {
    style: "currency",
    currency: normalizedCurrency,
    maximumFractionDigits: 2,
  });
}

export function deliveryOrderItemLabel(item) {
  const code = clean(item?.articleCode);
  const name = clean(item?.articleName) || clean(item?.description) || clean(item?.sourceText);
  const label = code && name && code !== name ? `${code} - ${name}` : code || name || "Artículo sin identificar";
  return `${formatOrderQuantity(item?.quantity)} x ${label}`;
}

export function formatServiceOrderReference(value) {
  const text = clean(value);
  if (!text) return "";
  const match = text.match(/OS[\s-]*(\d+)/i) || text.match(/^(\d+)$/);
  if (!match) return text.toUpperCase().startsWith("OS-") ? text.toUpperCase() : text;
  const number = Number.parseInt(match[1], 10);
  return Number.isFinite(number) ? `OS-${String(number).padStart(5, "0")}` : `OS-${match[1]}`;
}

export function serviceOrderReferenceIngresoId(value) {
  const reference = formatServiceOrderReference(value);
  const match = reference.match(/^OS-(\d+)$/i);
  return match ? String(Number.parseInt(match[1], 10)) : "";
}

function appendUniqueServiceReference(references, value) {
  const reference = formatServiceOrderReference(value);
  if (reference && !references.includes(reference)) references.push(reference);
}

function appendUniqueServiceIngresoId(ids, value) {
  const text = clean(value);
  if (!text) return;
  const parsed = serviceOrderReferenceIngresoId(text) || (/^\d+$/.test(text) ? String(Number.parseInt(text, 10)) : "");
  if (parsed && !ids.includes(parsed)) ids.push(parsed);
}

export function deliveryOrderServiceReleaseReferences(order) {
  if (clean(order?.deliveryType).toLowerCase() !== "service_release") return [];
  const references = [];
  const explicit = Array.isArray(order?.serviceReleaseReferences) ? order.serviceReleaseReferences : [];
  explicit.forEach((reference) => appendUniqueServiceReference(references, reference));
  appendUniqueServiceReference(references, order?.ingresoId);
  if (clean(order?.sourceSystem).toLowerCase() === "nexora") {
    appendUniqueServiceReference(references, order?.sourceExternalId);
  }
  const items = Array.isArray(order?.items) ? order.items : [];
  items.forEach((item) => {
    appendUniqueServiceReference(references, item?.ingresoId);
    const partidas = Array.isArray(item?.partidas) ? item.partidas : [];
    partidas.forEach((partida) => appendUniqueServiceReference(references, partida?.ingresoId));
  });
  return references;
}

export function deliveryOrderServiceReleaseIngresoIds(order) {
  if (clean(order?.deliveryType).toLowerCase() !== "service_release") return [];
  const ids = [];
  const explicit = Array.isArray(order?.serviceReleaseReferences) ? order.serviceReleaseReferences : [];
  explicit.forEach((reference) => appendUniqueServiceIngresoId(ids, reference));
  appendUniqueServiceIngresoId(ids, order?.ingresoId);
  if (clean(order?.sourceSystem).toLowerCase() === "nexora") {
    appendUniqueServiceIngresoId(ids, order?.sourceExternalId);
  }
  const items = Array.isArray(order?.items) ? order.items : [];
  items.forEach((item) => {
    appendUniqueServiceIngresoId(ids, item?.ingresoId);
    const partidas = Array.isArray(item?.partidas) ? item.partidas : [];
    partidas.forEach((partida) => appendUniqueServiceIngresoId(ids, partida?.ingresoId));
  });
  return ids;
}

export function deliveryOrderItemEffectivePartida(item, order) {
  const explicit = clean(item?.partida);
  if (explicit) return explicit;

  const partidas = Array.isArray(item?.partidas) ? item.partidas : [];
  if (partidas.some((partida) => clean(partida?.partida))) return "";

  const quantity = parseNumber(item?.quantity);
  if (Number.isFinite(quantity) && quantity > 1) return "";

  if (order?.deliveryType === "sale") return "";

  return clean(order?.equipmentSerial);
}

export function deliveryOrderItemPartidaLabel(item, order, options = {}) {
  const includePartidaLabel = options?.includePartidaLabel !== false;
  const partidas = Array.isArray(item?.partidas) ? item.partidas : [];
  const partidaText =
    deliveryOrderItemEffectivePartida(item, order) ||
    partidas
      .map((partida) => clean(partida?.partida))
      .filter(Boolean)
      .join(", ");
  const deposit = clean(item?.stockDepositCode) || clean(partidas.find((partida) => clean(partida?.stockDepositCode))?.stockDepositCode);
  return [partidaText ? `${includePartidaLabel ? "Partida " : ""}${partidaText}` : "", deposit ? `Depósito ${deposit}` : ""].filter(Boolean).join(" - ");
}

export function deliveryOrderItemUnitPrice(item) {
  return parseNumber(item?.unitPrice);
}

export function deliveryOrderItemDiscountPercent(item) {
  const parsed = parseNumber(item?.discountPercent);
  if (parsed === null) return 0;
  return Math.min(100, Math.max(0, parsed));
}

export function deliveryOrderItemAmounts(item) {
  const quantity = parseNumber(item?.quantity) ?? 0;
  const unitPrice = deliveryOrderItemUnitPrice(item);
  if (unitPrice === null) return null;
  const grossSubtotal = quantity * unitPrice;
  const discountAmount = grossSubtotal * deliveryOrderItemDiscountPercent(item) / 100;
  const netSubtotal = grossSubtotal - discountAmount;
  return { grossSubtotal, discountAmount, netSubtotal };
}

export function deliveryOrderItemSubtotal(item) {
  return deliveryOrderItemAmounts(item)?.netSubtotal ?? null;
}

export function deliveryOrderItemsTotals(order) {
  const items = Array.isArray(order?.items) ? order.items : [];
  const defaultCurrency = deliveryOrderPriceCurrency(order);
  const totalsByCurrency = {};
  let grossTotal = 0;
  let discountTotal = 0;
  let total = 0;
  let pricedItems = 0;
  let missingPriceItems = 0;
  for (const item of items) {
    const currency = deliveryOrderItemPriceCurrency(item, defaultCurrency);
    const bucket = totalsByCurrency[currency] || {
      currency,
      grossTotal: 0,
      discountTotal: 0,
      total: 0,
      pricedItems: 0,
      missingPriceItems: 0,
    };
    totalsByCurrency[currency] = bucket;
    const amounts = deliveryOrderItemAmounts(item);
    if (amounts === null) {
      missingPriceItems += 1;
      bucket.missingPriceItems += 1;
      continue;
    }
    grossTotal += amounts.grossSubtotal;
    discountTotal += amounts.discountAmount;
    total += amounts.netSubtotal;
    pricedItems += 1;
    bucket.grossTotal += amounts.grossSubtotal;
    bucket.discountTotal += amounts.discountAmount;
    bucket.total += amounts.netSubtotal;
    bucket.pricedItems += 1;
  }
  let currencies = ["ARS", "USD"].filter((currency) => totalsByCurrency[currency]);
  if (!currencies.length) {
    currencies = [defaultCurrency];
    totalsByCurrency[defaultCurrency] = totalsByCurrency[defaultCurrency] || {
      currency: defaultCurrency,
      grossTotal: 0,
      discountTotal: 0,
      total: 0,
      pricedItems: 0,
      missingPriceItems: 0,
    };
  }
  const mixedCurrency = currencies.length > 1;
  return {
    itemCount: items.length,
    pricedItems,
    missingPriceItems,
    hasMissingPrices: missingPriceItems > 0,
    currency: mixedCurrency ? "MIXED" : currencies[0],
    currencies,
    mixedCurrency,
    totalsByCurrency,
    grossTotal,
    discountTotal,
    total,
  };
}

export function formatOrderTotalsAmount(totals, field = "total") {
  if (!totals?.mixedCurrency) return formatOrderMoney(totals?.[field], totals?.currency);
  return (totals.currencies || [])
    .map((currency) => formatOrderMoney(totals.totalsByCurrency?.[currency]?.[field], currency))
    .filter((label) => label && label !== "-")
    .join(" / ") || "-";
}

export function deliveryOrderItemsSummary(order, maxItems = 2) {
  const items = Array.isArray(order?.items) ? order.items : [];
  if (!items.length) {
    return {
      primary: deliveryOrderEquipmentContext(order) || "-",
      secondary: "Sin renglones cargados",
    };
  }
  const serviceReleaseReferences = deliveryOrderServiceReleaseReferences(order);
  const serviceReleaseCount = Math.max(
    items.length,
    serviceReleaseReferences.length,
    Number.parseInt(clean(order?.serviceReleaseCount), 10) || 0
  );
  if (order?.deliveryType === "service_release" && serviceReleaseCount > 1) {
    const shownReferences = serviceReleaseReferences.slice(0, 3);
    const remainingReferences = serviceReleaseReferences.length - shownReferences.length;
    const referencesLabel = shownReferences.length
      ? `${shownReferences.join(", ")}${remainingReferences > 0 ? ` +${remainingReferences} OS` : ""}`
      : "";
    const shownEquipment = items
      .slice(0, maxItems)
      .map((item) => clean(item?.articleName) || clean(item?.description) || clean(item?.sourceText) || clean(item?.articleCode))
      .filter(Boolean);
    const remainingItems = items.length - shownEquipment.length;
    const equipmentLabel = shownEquipment.length
      ? `${shownEquipment.join(" | ")}${remainingItems > 0 ? ` +${remainingItems}` : ""}`
      : "";
    return {
      primary: `${serviceReleaseCount} equipos`,
      secondary: [referencesLabel, equipmentLabel].filter(Boolean).join(" - "),
    };
  }
  const shown = items.slice(0, maxItems).map(deliveryOrderItemLabel);
  const remaining = items.length - shown.length;
  return {
    primary: `${shown.join(" | ")}${remaining > 0 ? ` +${remaining}` : ""}`,
    secondary: [deliveryOrderItemPartidaLabel(items[0], order), deliveryOrderEquipmentContext(order)].filter(Boolean).join(" - "),
  };
}

export function deliveryOrderEquipmentContext(order) {
  const model = clean(order?.equipmentModel);
  const identifiers = [clean(order?.equipmentSerial), clean(order?.equipmentInternalNumber)].filter(Boolean).join(" / ");
  if (!model && !identifiers) return "";
  const prefix = order?.deliveryType === "service_release" ? "OS/equipo" : "Referencia";
  return [prefix, model, identifiers].filter(Boolean).join(" - ");
}

export function deliveryOrderSourceLabel(order) {
  const sheet = clean(order?.sourceSheet);
  const row = order?.sourceRow === 0 || order?.sourceRow ? `fila ${order.sourceRow}` : "";
  const sourceLocation = [sheet, row].filter(Boolean).join(" ");
  return [sourceLocation, clean(order?.sourceReference), clean(order?.sourceSystem)].filter(Boolean).join(" - ");
}

// Espejo de service/bejerman_sdk.py: OCCASIONAL_CUSTOMER_NAMES / is_occasional_customer_name.
const OCCASIONAL_CUSTOMER_NAMES = new Set(["particular", "particulares", "consumidorfinal", "clientevario"]);

function normalizedCustomerName(value) {
  // NFD separa los acentos en marcas combinantes y el filtro alfanumérico las descarta,
  // igual que el lado Python: "Pärticular" y "Particular" normalizan al mismo texto.
  return String(value ?? "")
    .normalize("NFD")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

export function isOccasionalCustomerName(value) {
  return OCCASIONAL_CUSTOMER_NAMES.has(normalizedCustomerName(value));
}

/** Las órdenes de un particular no llevan código Bejerman: se emiten como cliente ocasional
 * (`@@@#@@`) con el nombre y documento del propietario que capturó el ingreso. */
export function deliveryOrderIsOccasionalCustomer(order) {
  return !clean(order?.bejermanCustomerCode) && isOccasionalCustomerName(order?.customerName);
}

export function deliveryOrderOccasionalCustomerReady(order) {
  return Boolean(clean(order?.propietarioNombre) && clean(order?.propietarioDoc));
}

/** Identidad de un particular: el documento del propietario, normalizado a dígitos como lo
 * guarda el maestro. Es lo único que distingue a una persona de otra, porque todos comparten
 * la misma fila `customers` y ninguno tiene código Bejerman. */
export function deliveryOrderOccasionalCustomerDocument(order) {
  if (!deliveryOrderIsOccasionalCustomer(order)) return "";
  return clean(order?.propietarioDoc).replace(/\D/g, "");
}

export function deliveryOrderBejermanCustomerLabel(order) {
  const code = clean(order?.bejermanCustomerCode);
  if (code) return code;
  if (deliveryOrderIsOccasionalCustomer(order)) {
    const owner = clean(order?.propietarioNombre);
    const doc = clean(order?.propietarioDoc);
    return ["Cliente ocasional", owner, doc ? `Doc. ${doc}` : ""].filter(Boolean).join(" - ");
  }
  return "";
}

export function deliveryOrderCommercialLabel(order) {
  return [
    clean(order?.sellerName),
    clean(order?.operationCompanyLabel),
    clean(order?.commercialCondition),
    deliveryOrderPriceCurrency(order) === "USD" ? "U$S" : "$",
    clean(order?.commercialExchangeRate) ? `TC ${clean(order?.commercialExchangeRate)}` : "",
  ]
    .filter(Boolean)
    .join(" - ");
}

export function deliveryOrderRentalEquipmentKey(option) {
  const ingresoId = clean(option?.ingresoId);
  if (ingresoId) return `os:${ingresoId}`;
  const deviceId = clean(option?.deviceId);
  if (deviceId) return `dev:${deviceId}`;
  return "";
}

export function deliveryOrderSelectedRentalKeys(items) {
  const keys = new Set();
  for (const item of items || []) {
    const itemKey = deliveryOrderRentalEquipmentKey(item);
    if (itemKey) keys.add(itemKey);
    for (const partida of item?.partidas || []) {
      const partidaKey = deliveryOrderRentalEquipmentKey(partida);
      if (partidaKey) keys.add(partidaKey);
    }
  }
  return keys;
}

export function hasPartidaRowData(row) {
  return Boolean(
    clean(row?.partida) ||
    clean(row?.partidaExpirationDate) ||
    clean(row?.stockAvailableQuantity)
  );
}

export function isDeliveryOrderBlankItem(item) {
  return (
    !clean(item?.id) &&
    !clean(item?.ingresoId) &&
    !clean(item?.deviceId) &&
    !clean(item?.articleCode) &&
    !clean(item?.description) &&
    !(Array.isArray(item?.partidas) ? item.partidas : []).some(hasPartidaRowData)
  );
}

export function deliveryOrderPartidaFromRentalOption(option, fallbackDeposit = "STL") {
  const stock = option?.stockAvailableQuantity == null ? "" : String(option.stockAvailableQuantity);
  return {
    ingresoId: option?.ingresoId == null ? "" : String(option.ingresoId),
    deviceId: option?.deviceId == null ? "" : String(option.deviceId),
    partida: clean(option?.partida || option?.equipmentSerial),
    assignedQuantity: "1",
    partidaExpirationDate: clean(option?.partidaExpirationDate),
    stockDepositCode: clean(option?.stockDepositCode) || fallbackDeposit || "STL",
    stockAvailableQuantity: stock,
    stockCheckedAt: clean(option?.stockCheckedAt),
  };
}

export function deliveryOrderAppendRentalEquipment(
  items,
  option,
  { priceCurrency = "ARS", fallbackDeposit = "STL" } = {}
) {
  const currentItems = Array.isArray(items) ? items : [];
  const articleCode = clean(option?.articleCode);
  const newPartida = deliveryOrderPartidaFromRentalOption(option, fallbackDeposit);

  // Si ya existe un renglón con el mismo código de artículo, se incorpora como una nueva partida a ese renglón
  const existingIndex = articleCode
    ? currentItems.findIndex(
        (it) => clean(it?.articleCode).toUpperCase() === articleCode.toUpperCase()
      )
    : -1;

  if (existingIndex >= 0) {
    const existingItem = currentItems[existingIndex];
    const currentPartidas = Array.isArray(existingItem.partidas) ? existingItem.partidas : [];

    const optionIngresoId = clean(option?.ingresoId);
    const optionDeviceId = clean(option?.deviceId);
    const optionSerial = clean(option?.partida || option?.equipmentSerial).toUpperCase();

    const alreadyPresent = currentPartidas.some((p) => {
      if (optionIngresoId && clean(p.ingresoId) === optionIngresoId) return true;
      if (optionDeviceId && clean(p.deviceId) === optionDeviceId) return true;
      if (optionSerial && clean(p.partida).toUpperCase() === optionSerial) return true;
      return false;
    });

    if (alreadyPresent) {
      return { items: currentItems, targetIndex: existingIndex, added: false };
    }

    let nextPartidas;
    const emptyRowIndex = currentPartidas.findIndex((p) => !hasPartidaRowData(p));
    if (emptyRowIndex >= 0) {
      nextPartidas = [...currentPartidas];
      nextPartidas[emptyRowIndex] = newPartida;
    } else {
      nextPartidas = [...currentPartidas, newPartida];
    }

    const activeRows = nextPartidas.filter(hasPartidaRowData);
    const totalAssigned = activeRows.reduce(
      (sum, row) => sum + (parseNumber(row.assignedQuantity) || 1),
      0
    );
    const currentQty = parseNumber(existingItem.quantity) || 0;
    const nextQuantity = String(Math.max(currentQty, totalAssigned));

    const existingLegend = clean(existingItem.remitoArticleLegend);
    const optionLegend = clean(option?.remitoArticleLegend || option?.accessories);
    const remitoArticleLegend = existingLegend || optionLegend;

    const updatedItem = {
      ...existingItem,
      quantity: nextQuantity,
      remitoArticleLegend,
      articleRequiresPartida: true,
      stockDepositCode: existingItem.stockDepositCode || fallbackDeposit || "STL",
      partidas: nextPartidas,
      ...(nextPartidas.length > 1 ? { ingresoId: "", deviceId: "" } : {}),
    };

    const nextItems = currentItems.map((it, idx) => (idx === existingIndex ? updatedItem : it));
    return { items: nextItems, targetIndex: existingIndex, added: true };
  }

  // Si el artículo no está en la orden, se agrega como renglón nuevo (o reemplaza el renglón inicial vacío)
  const isOnlyBlank = currentItems.length === 1 && isDeliveryOrderBlankItem(currentItems[0]);
  const targetIndex = isOnlyBlank ? 0 : currentItems.length;

  const stock = option?.stockAvailableQuantity == null ? "" : String(option.stockAvailableQuantity);
  const newItem = {
    id: "",
    ingresoId: option?.ingresoId == null ? "" : String(option.ingresoId),
    deviceId: option?.deviceId == null ? "" : String(option.deviceId),
    quantity: "1",
    articleCode,
    articleName: clean(option?.articleName),
    description: clean(option?.description || option?.equipmentDetail || option?.equipmentModel),
    remitoArticleLegend: clean(option?.remitoArticleLegend || option?.accessories || ""),
    unitPrice: "",
    priceCurrency: clean(priceCurrency).toUpperCase() === "USD" ? "USD" : "ARS",
    discountPercent: "",
    partida: clean(option?.partida || option?.equipmentSerial),
    partidaExpirationDate: clean(option?.partidaExpirationDate),
    stockDepositCode: clean(option?.stockDepositCode) || fallbackDeposit || "STL",
    stockAvailableQuantity: stock,
    stockCheckedAt: clean(option?.stockCheckedAt),
    articleRequiresPartida: true,
    partidas: [newPartida],
  };

  const nextItems = isOnlyBlank ? [newItem] : [...currentItems, newItem];
  return { items: nextItems, targetIndex, added: true };
}
