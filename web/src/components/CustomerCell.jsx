import { ingresoCustomerBaseName, ingresoCustomerName } from "../lib/ui-helpers";

/**
 * Celda de la columna Cliente de los listados de Servicio Técnico.
 *
 * "Particular" y "MG BIO" son clientes contenedores: la fila decía "Particular"
 * y el operador no tenía forma de saber de quién era el equipo. Cuando el
 * ingreso trae dueño cargado, ese es el nombre principal y el contenedor pasa a
 * la línea chica.
 */
export default function CustomerCell({ row }) {
  const base = ingresoCustomerBaseName(row);
  return (
    <>
      <div>{ingresoCustomerName(row) || "-"}</div>
      {/* "Cliente base" y no "Cliente": arriba ya hay un nombre, y decirle "Cliente" a los
          dos se lee como una contradicción. Mismo texto que `deviceCustomerSubtitle`. */}
      {base ? <div className="text-xs text-gray-500">Cliente base: {base}</div> : null}
    </>
  );
}
