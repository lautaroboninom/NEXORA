import { useEffect, useRef } from "react";

/**
 * Checkbox de selección. `indeterminate` sólo se puede setear por DOM, no por
 * atributo, y es lo que distingue "ninguna elegida" de "algunas elegidas" en el
 * checkbox de la cabecera.
 */
export default function SelectionCheckbox({
  checked = false,
  indeterminate = false,
  onChange,
  disabled = false,
  label,
  className = "",
}) {
  const ref = useRef(null);

  useEffect(() => {
    if (ref.current) ref.current.indeterminate = Boolean(indeterminate) && !checked;
  }, [indeterminate, checked]);

  return (
    <input
      ref={ref}
      type="checkbox"
      checked={Boolean(checked)}
      onChange={onChange}
      disabled={disabled}
      aria-label={label}
      className={`h-4 w-4 cursor-pointer ${className}`}
    />
  );
}
