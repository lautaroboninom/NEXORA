const clean = (value) => String(value ?? "").trim();

export function formatOrderQuantity(value) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  if (!Number.isFinite(parsed) || parsed <= 0) return "1";
  if (Number.isInteger(parsed)) return String(parsed);
  return parsed.toLocaleString("es-AR", { maximumFractionDigits: 2 });
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

  const quantity = Number.parseFloat(String(item?.quantity ?? "").replace(",", "."));
  if (Number.isFinite(quantity) && quantity > 1) return "";

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
    clean(order?.commercialExchangeRate) ? `TC ${clean(order?.commercialExchangeRate)}` : "",
  ]
    .filter(Boolean)
    .join(" - ");
}
