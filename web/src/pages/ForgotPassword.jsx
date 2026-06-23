import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, ArrowLeft, CheckCircle2, Loader2, Mail, RotateCcw } from "lucide-react";
import AuthLayout from "../components/AuthLayout.jsx";
import { postAuthForgot } from "../lib/api";

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function StepList() {
  const steps = ["Ingresá tu correo", "Revisá el enlace", "Definí una nueva clave"];
  return (
    <ol className="space-y-3 text-sm text-gray-700">
      {steps.map((step, index) => (
        <li key={step} className="flex items-center gap-3">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-blue-200 bg-white text-xs font-semibold text-blue-700">
            {index + 1}
          </span>
          <span>{step}</span>
        </li>
      ))}
    </ol>
  );
}

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sentEmail, setSentEmail] = useState("");
  const [err, setErr] = useState("");
  const [sending, setSending] = useState(false);

  const normalizedEmail = useMemo(() => email.trim().toLowerCase(), [email]);
  const emailLooksValid = !normalizedEmail || emailPattern.test(normalizedEmail);
  const canSubmit = Boolean(normalizedEmail) && emailLooksValid && !sending;
  const sent = Boolean(sentEmail);

  async function submit(event) {
    event.preventDefault();
    setErr("");
    if (!emailPattern.test(normalizedEmail)) {
      setErr("Ingresá un correo válido.");
      return;
    }
    if (sending) return;
    setSending(true);
    try {
      await postAuthForgot(normalizedEmail);
      setSentEmail(normalizedEmail);
    } catch (error) {
      setErr(error?.message || "No pudimos procesar la solicitud. Intentá nuevamente.");
    } finally {
      setSending(false);
    }
  }

  function requestAnotherEmail() {
    setSentEmail("");
    setEmail("");
    setErr("");
  }

  return (
    <AuthLayout
      title="Recuperar contraseña"
      subtitle="Solicitá un enlace temporal para volver a ingresar a NEXORA."
      aside={<StepList />}
    >
      {sent ? (
        <div className="space-y-5">
          <div className="flex items-start gap-3 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-emerald-900">
            <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
            <div>
              <h2 className="text-base font-semibold">Solicitud registrada</h2>
              <p className="mt-1 text-sm leading-6">
                Si el usuario está activo, enviamos un enlace a <span className="font-medium">{sentEmail}</span>.
                El enlace vence a los pocos minutos.
              </p>
            </div>
          </div>

          <div className="rounded-lg border border-gray-200 bg-white p-4 text-sm text-gray-700">
            Revisá la bandeja de entrada y correo no deseado. Si no llega, verificá que el correo sea el de tu usuario de NEXORA.
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <Link className="btn flex items-center justify-center gap-2" to="/login">
              <ArrowLeft className="h-4 w-4" aria-hidden="true" />
              Volver a ingresar
            </Link>
            <button
              type="button"
              className="flex items-center justify-center gap-2 rounded-md border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
              onClick={requestAnotherEmail}
            >
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
              Usar otro correo
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-5">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-gray-800" htmlFor="recovery-email">
              Correo del usuario
            </label>
            <div className="relative">
              <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" aria-hidden="true" />
              <input
                id="recovery-email"
                className={`input pl-10 ${emailLooksValid ? "" : "border-red-400 focus:outline-red-500"}`}
                type="email"
                placeholder="usuario@sepid.com.ar"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="email"
                required
              />
            </div>
            {!emailLooksValid && <p className="mt-2 text-sm text-red-600">Ingresá un correo válido.</p>}
          </div>

          {err && (
            <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <span>{err}</span>
            </div>
          )}

          <button type="submit" disabled={!canSubmit} className="btn flex w-full items-center justify-center gap-2 disabled:cursor-not-allowed disabled:bg-gray-300">
            {sending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Mail className="h-4 w-4" aria-hidden="true" />}
            {sending ? "Enviando enlace" : "Enviar enlace"}
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
