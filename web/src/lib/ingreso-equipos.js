/**
 * Identidad de un equipo dentro de la lista "Equipos en remito".
 *
 * La clave es número de serie **más** tipo de equipo. Sólo con la serie no alcanza: el
 * calentador humidificador no trae serie propia y se carga con la del CPAP con el que vino,
 * así que dos equipos legítimamente distintos comparten número. Mirando sólo la serie, el
 * segundo parece el primero cargado dos veces.
 *
 * Cuando no hay serie se usa el número interno (MG/NM/NV/CE), que es el otro identificador
 * con el que entran los equipos.
 */
export function equipmentIdentityKey(item) {
  const normalize = (value) => String(value || "").replace(/[\s-]/g, "").toUpperCase();
  const serial = normalize(item?.payload?.equipo?.numero_serie);
  const interno = normalize(item?.payload?.equipo?.numero_interno);
  const identifier = serial || interno;
  return identifier ? `${identifier}|${equipmentTipoKey(item)}` : "";
}

/**
 * El tipo de equipo con el que se compara, normalizado.
 *
 * Sale del payload —que lleva el tipo del modelo elegido, que es el que el backend estampa en
 * `devices.tipo_equipo` y el que entra en el índice único— y cae al selector de la pantalla
 * para los equipos que se cargaron antes de que el payload lo incluyera.
 */
export function equipmentTipoKey(item) {
  const fromPayload = item?.payload?.equipo?.tipo_equipo;
  const fromEditor = item?.editor?.tipoSel;
  return String(fromPayload || fromEditor || "").trim().toLowerCase();
}

/**
 * Busca en la lista otro equipo con la misma identidad.
 *
 * Devuelve el índice (para poder decir "ya está como #3") o -1. El equipo que se está
 * editando no cuenta como choque contra sí mismo.
 */
export function findDuplicateEquipmentIndex(items, candidate) {
  const key = equipmentIdentityKey(candidate);
  if (!key) return -1;
  return (items || []).findIndex(
    (item) => item?.id !== candidate?.id && equipmentIdentityKey(item) === key
  );
}

/**
 * ¿Alguno de los equipos ya cargados es el que acaba de devolver el autocompletado?
 *
 * Cuando el CPAP ya está en la lista y el operador tipea la misma serie para cargar su
 * humidificador, el lookup vuelve a traer el CPAP: hay que preguntarle cuál está ingresando en
 * vez de llenarle la pantalla con el equipo que ya cargó. `skipItemId` deja afuera el renglón
 * que se está editando, que obviamente coincide consigo mismo.
 */
export function findStagedEquipmentIndex(items, device, { skipItemId = null } = {}) {
  const candidate = {
    id: "__lookup__",
    payload: {
      equipo: {
        numero_serie: device?.numero_serie || "",
        numero_interno: device?.numero_interno || "",
        tipo_equipo: device?.tipo_equipo || "",
      },
    },
  };
  const pool = skipItemId ? (items || []).filter((item) => item?.id !== skipItemId) : items || [];
  const index = findDuplicateEquipmentIndex(pool, candidate);
  if (index < 0) return -1;
  // El índice tiene que servir para decir "ya está como #N" sobre la lista original.
  return (items || []).indexOf(pool[index]);
}
