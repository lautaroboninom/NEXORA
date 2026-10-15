import useNewVersionAvailable from "../hooks/useNewVersionAvailable";

/**
 * Cartel para las pestañas que quedaron abiertas durante un rebuild: siguen con el
 * programa viejo hasta recargar. No recarga solo para no perder lo que se esté cargando.
 */
export default function NewVersionBanner() {
  const disponible = useNewVersionAvailable();
  if (!disponible) return null;

  return (
    <div role="status" className="border-b border-blue-200 bg-blue-50 text-sm text-blue-900">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-2 px-4 py-2">
        <span>Hay una versión nueva de NEXORA. Guardá lo que estés cargando y actualizá.</span>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="rounded-md bg-blue-600 px-3 py-1.5 font-medium text-white shadow hover:bg-blue-700"
        >
          Actualizar
        </button>
      </div>
    </div>
  );
}
