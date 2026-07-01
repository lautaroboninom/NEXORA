import { deviceIdentifierPartsOf } from "../lib/ui-helpers";

export default function DeviceIdentifier({
  row,
  fallback = "-",
  className = "",
  primaryClassName = "",
  secondaryClassName = "text-[11px] leading-tight text-gray-500",
}) {
  const parts = deviceIdentifierPartsOf(row, fallback);
  const identifiers = Array.isArray(parts.identifiers) ? parts.identifiers : [];
  const title = identifiers.length
    ? identifiers.map((item) => item.value).join(" - ")
    : (parts.secondary ? `${parts.primary} - ${parts.secondary}` : parts.primary);

  if (identifiers.length) {
    return (
      <span className={`inline-flex flex-col gap-0.5 ${className}`.trim()} title={title}>
        {identifiers.map((item, idx) => (
          <span key={item.kind || item.label} className={idx === 0 ? primaryClassName : secondaryClassName}>
            <span className="font-mono">{item.value}</span>
          </span>
        ))}
      </span>
    );
  }

  if (!parts.secondary) {
    return (
      <span className={className} title={title}>
        {parts.primary}
      </span>
    );
  }

  return (
    <span className={`inline-flex flex-col gap-0.5 ${className}`.trim()} title={title}>
      <span className={primaryClassName}>{parts.primary}</span>
      <span className={secondaryClassName}>{parts.secondary}</span>
    </span>
  );
}
