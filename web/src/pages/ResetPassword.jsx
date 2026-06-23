import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertCircle, ArrowLeft, CheckCircle2, Eye, EyeOff, KeyRound, Loader2, LockKeyhole, XCircle } from "lucide-react";
import AuthLayout from "../components/AuthLayout.jsx";
import { postAuthReset } from "../lib/api";

function Requirement({ ok, children }) {
  const Icon = ok ? CheckCircle2 : XCircle;
  return (
    <li className={`flex items-center gap-2 ${ok ? "text-emerald-700" : "text-gray-500"}`}>
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </li>
  );
}

function PasswordInput({ id, label, value, onChange, visible, onToggle, autoFocus = false }) {
  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium text-gray-800" htmlFor={id}>
        {label}
      </label>
      <div className="relative">
        <LockKeyhole className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" aria-hidden="true" />
        <input
          id={id}
          className="input pl-10 pr-11"
          type={visible ? "text" : "password"}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          autoComplete="new-password"
          autoFocus={autoFocus}
          required
        />
        <button
          type="button"
          className="absolute right-1.5 top-1/2 flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-md text-gray-500 hover:bg-gray-100 hover:text-gray-800"
          onClick={onToggle}
          aria-label={visible ? "Ocultar contraseña" : "Mostrar contraseña"}
        >
          {visible ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
        </button>
      </div>
    </div>
  );
}

export default function ResetPassword() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("t") || searchParams.get("token") || "";
  const [password, setPassword] = useState("");
  const [repeatPassword, setRepeatPassword] = useState("");
  const [err, setErr] = useState("");
  const [done, setDone] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showRepeatPassword, setShowRepeatPassword] = useState(false);

  const passwordReady = password.length >= 8;
  const passwordsMatch = Boolean(repeatPassword) && password === repeatPassword;
  const canSubmit = Boolean(token) && passwordReady && passwordsMatch && !saving;

  const aside = useMemo(
    () => (
      <div className="space-y-4 text-sm text-gray-700">
        <div className="rounded-lg border border-blue-100 bg-white p-4">
          El enlace de recuperación es temporal y se invalida después de usarlo.
        </div>
        <ul className="space-y-3">
          <Requirement ok={passwordReady}>Mínimo 8 caracteres</Requirement>
          <Requirement ok={!repeatPassword || passwordsMatch}>Ambas contraseñas coinciden</Requirement>
        </ul>
      </div>
    ),
    [passwordReady, passwordsMatch, repeatPassword]
  );

  async function submit(event) {
    event.preventDefault();
    setErr("");
    if (!token) {
      setErr("El enlace no es válido o está incompleto.");
      return;
    }
    if (!passwordReady) {
      setErr("La contraseña debe tener al menos 8 caracteres.");
      return;
    }
    if (!passwordsMatch) {
      setErr("Las contraseñas no coinciden.");
      return;
    }
    setSaving(true);
    try {
      await postAuthReset(token, password);
      setDone(true);
    } catch (error) {
      setErr(error?.message || "No pudimos restablecer la contraseña. Solicitá un nuevo enlace.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <AuthLayout
      title="Restablecer contraseña"
      subtitle="Creá una clave nueva para recuperar el acceso a tu usuario."
      aside={aside}
    >
      {!token ? (
        <div className="space-y-5">
          <div className="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-red-800">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
            <div>
              <h2 className="font-semibold">Enlace inválido</h2>
              <p className="mt-1 text-sm leading-6">Abrí el enlace completo del correo o solicitá uno nuevo.</p>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Link className="btn flex items-center justify-center gap-2" to="/recuperar">
              <KeyRound className="h-4 w-4" aria-hidden="true" />
              Solicitar enlace
            </Link>
            <Link className="flex items-center justify-center gap-2 rounded-md border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50" to="/login">
              <ArrowLeft className="h-4 w-4" aria-hidden="true" />
              Volver a ingresar
            </Link>
          </div>
        </div>
      ) : done ? (
        <div className="space-y-5">
          <div className="flex items-start gap-3 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-emerald-900">
            <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
            <div>
              <h2 className="font-semibold">Contraseña actualizada</h2>
              <p className="mt-1 text-sm leading-6">Ya podés ingresar con tu nueva clave.</p>
            </div>
          </div>
          <Link className="btn flex w-full items-center justify-center gap-2" to="/login">
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            Ir al ingreso
          </Link>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-5">
          <PasswordInput
            id="new-password"
            label="Nueva contraseña"
            value={password}
            onChange={setPassword}
            visible={showPassword}
            onToggle={() => setShowPassword((value) => !value)}
            autoFocus
          />
          <PasswordInput
            id="repeat-password"
            label="Repetir contraseña"
            value={repeatPassword}
            onChange={setRepeatPassword}
            visible={showRepeatPassword}
            onToggle={() => setShowRepeatPassword((value) => !value)}
          />

          {err && (
            <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <span>{err}</span>
            </div>
          )}

          <button type="submit" disabled={!canSubmit} className="btn flex w-full items-center justify-center gap-2 disabled:cursor-not-allowed disabled:bg-gray-300">
            {saving ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <KeyRound className="h-4 w-4" aria-hidden="true" />}
            {saving ? "Guardando contraseña" : "Guardar contraseña"}
          </button>

          <Link to="/login" className="inline-flex items-center gap-2 text-sm font-medium text-blue-700 hover:text-blue-800">
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            Volver a ingresar
          </Link>
        </form>
      )}
    </AuthLayout>
  );
}
