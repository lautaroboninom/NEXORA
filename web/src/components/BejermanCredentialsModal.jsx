import { useState } from "react";
import { postBejermanCredentials } from "../lib/api";

function errorText(error) {
  return error?.data?.detail || error?.detail || error?.message || "No se pudieron validar las credenciales.";
}

export default function BejermanCredentialsModal({ open, onSaved, onExit }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  if (!open) return null;

  async function submit(event) {
    event.preventDefault();
    setError("");
    setSaving(true);
    try {
      await postBejermanCredentials({ username, password });
      setPassword("");
      await onSaved?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  }

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
          Ingrese sus credenciales personales para operar con Bejerman desde NEXORA.
        </p>

        <form onSubmit={submit} className="mt-5 space-y-4">
          <div>
            <label className="text-sm font-medium text-gray-700">Usuario Bejerman</label>
            <input
              className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm uppercase outline-none focus:border-blue-500"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoFocus
              autoComplete="username"
              required
            />
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

          {error && (
            <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              className="rounded border border-gray-300 px-3 py-2 text-sm hover:bg-gray-50"
              onClick={onExit}
              disabled={saving}
            >
              Salir
            </button>
            <button
              type="submit"
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
              disabled={saving}
            >
              {saving ? "Validando..." : "Guardar"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
