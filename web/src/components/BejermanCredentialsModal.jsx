import { useEffect, useState } from "react";
import { getBejermanWorkstations, postBejermanCredentials } from "../lib/api";

const COMMON_WORKSTATIONS = [
  { code: "STEC", label: "Servicio Técnico" },
  { code: "VTAS", label: "Ventas" },
  { code: "DEPO", label: "Depósito" },
  { code: "GER", label: "Gerencia" },
  { code: "ADMV", label: "Administración" },
];

function errorText(error) {
  return error?.data?.detail || error?.detail || error?.message || "No se pudieron validar las credenciales.";
}

export default function BejermanCredentialsModal({
  open,
  initialWorkstation = "",
  allowPersistentDismissal = true,
  onSaved,
  onExit,
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [workstation, setWorkstation] = useState("");
  const [workstationOptions, setWorkstationOptions] = useState([]);
  const [lookupUsername, setLookupUsername] = useState("");
  const [lookupMessage, setLookupMessage] = useState("");
  const [lookingUp, setLookingUp] = useState(false);
  const [neverShowAgain, setNeverShowAgain] = useState(false);
  const [saving, setSaving] = useState(false);
  const [exiting, setExiting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setUsername("");
    setPassword("");
    setWorkstation(String(initialWorkstation || "").trim().toUpperCase());
    setWorkstationOptions([]);
    setLookupUsername("");
    setLookupMessage("");
    setNeverShowAgain(false);
    setError("");
  }, [open, initialWorkstation]);

  if (!open) return null;

  async function lookupWorkstations() {
    const cleanUsername = username.trim().toUpperCase();
    if (cleanUsername.length < 2 || cleanUsername === lookupUsername || lookingUp) return;

    setLookingUp(true);
    setLookupMessage("");
    setLookupUsername(cleanUsername);
    try {
      const data = await getBejermanWorkstations(cleanUsername);
      const options = Array.isArray(data?.options) ? data.options : [];
      const recommended = String(data?.recommended || "").trim().toUpperCase();
      setWorkstationOptions(options);
      setWorkstation((current) => {
        const normalizedCurrent = String(current || "").trim().toUpperCase();
        if (recommended) return recommended;
        if (options.some((option) => option.code === normalizedCurrent)) return normalizedCurrent;
        return options.length === 1 ? options[0].code : normalizedCurrent;
      });
      setLookupMessage(data?.detail || "");
    } catch (err) {
      setWorkstationOptions([]);
      setLookupMessage(
        `${errorText(err)} Podés completar el puesto manualmente.`,
      );
    } finally {
      setLookingUp(false);
    }
  }

  async function submit(event) {
    event.preventDefault();
    setError("");
    setSaving(true);
    try {
      await postBejermanCredentials({ username, password, workstation });
      setPassword("");
      await onSaved?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  }

  async function exitModal() {
    setError("");
    setExiting(true);
    try {
      await onExit?.({ neverShowAgain: allowPersistentDismissal && neverShowAgain });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setExiting(false);
    }
  }

  const busy = saving || exiting;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4">
      <section
        className="w-full max-w-md rounded-lg border border-gray-200 bg-white p-5 shadow-xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="bejerman-credentials-title"
      >
        <h2 id="bejerman-credentials-title" className="text-lg font-semibold text-gray-950">
          Credenciales Bejerman
        </h2>
        <p className="mt-2 text-sm text-gray-600">
          Ingresá tus credenciales personales. NEXORA buscará los puestos habilitados en Bejerman.
        </p>

        <form onSubmit={submit} className="mt-5 space-y-4">
          <div>
            <label className="text-sm font-medium text-gray-700">Usuario Bejerman</label>
            <input
              className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm uppercase outline-none focus:border-blue-500"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
                setLookupUsername("");
                setLookupMessage("");
                setWorkstationOptions([]);
              }}
              onBlur={lookupWorkstations}
              autoFocus
              autoComplete="username"
              required
            />
          </div>

          <div>
            <label className="text-sm font-medium text-gray-700">Puesto Bejerman</label>
            {workstationOptions.length > 0 ? (
              <select
                className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
                value={workstation}
                onChange={(event) => setWorkstation(event.target.value)}
                required
              >
                <option value="">Seleccionar puesto</option>
                {workstationOptions.map((option) => (
                  <option key={option.code} value={option.code}>
                    {option.code} — {option.label || option.code}
                  </option>
                ))}
              </select>
            ) : (
              <>
                <input
                  className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm uppercase outline-none focus:border-blue-500"
                  value={workstation}
                  onChange={(event) => setWorkstation(event.target.value)}
                  list="bejerman-workstation-options"
                  placeholder="Ej.: STEC o VTAS"
                  autoComplete="off"
                  required
                />
                <datalist id="bejerman-workstation-options">
                  {COMMON_WORKSTATIONS.map((option) => (
                    <option key={option.code} value={option.code}>
                      {option.label}
                    </option>
                  ))}
                </datalist>
              </>
            )}
            <p className={`mt-1 text-xs ${lookupMessage ? "text-gray-600" : "text-gray-500"}`}>
              {lookingUp
                ? "Consultando puestos habilitados..."
                : lookupMessage || "Se completa automáticamente al salir del campo Usuario Bejerman."}
            </p>
          </div>

          <div>
            <label className="text-sm font-medium text-gray-700">Clave Bejerman</label>
            <input
              className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          {allowPersistentDismissal && (
            <label className="flex cursor-pointer items-start gap-2 rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
              <input
                className="mt-0.5"
                type="checkbox"
                checked={neverShowAgain}
                onChange={(event) => setNeverShowAgain(event.target.checked)}
              />
              <span>
                No volver a mostrar este mensaje automáticamente.
                Podrás abrirlo desde el botón <strong>Bejerman</strong> de la barra superior.
              </span>
            </label>
          )}

          {error && (
            <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              className="rounded border border-gray-300 px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-60"
              onClick={exitModal}
              disabled={busy}
            >
              {exiting ? "Cerrando..." : "Ahora no"}
            </button>
            <button
              type="submit"
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
              disabled={busy || lookingUp}
            >
              {saving ? "Validando..." : "Guardar"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
