const hoursFormatter = new Intl.NumberFormat("es-AR", {
  maximumFractionDigits: 2,
  useGrouping: false,
});

export function hoursTextFromMinutes(minutes) {
  return hoursFormatter.format(Number(minutes) / 60);
}

export function minutesFromHoursText(value) {
  const text = String(value ?? "").trim();
  if (!/^(?:\d+(?:[.,]\d*)?|[.,]\d+)$/.test(text)) return null;
  const hours = Number(text.replace(",", "."));
  if (!Number.isFinite(hours) || hours > 8760) return null;
  const minutes = Math.round(hours * 60);
  return hours > 0 && minutes === 0 ? null : minutes;
}
