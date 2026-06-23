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

export function deliveryOrderItemPartidaLabel(item, order) {
  const partidas = Array.isArray(item?.partidas) ? item.partidas : [];
  const partidaText =
    deliveryOrderItemEffectivePartida(item, order) ||
    partidas
      .map((partida) => clean(partida?.partida))
      .filter(Boolean)
      .join(", ");
  const deposit = clean(item?.stockDepositCode) || clean(partidas.find((partida) => clean(partida?.stockDepositCode))?.stockDepositCode);
  return [partidaText ? `Partida ${partidaText}` : "", deposit ? `Depósito ${deposit}` : ""].filter(Boolean).join(" - ");
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
