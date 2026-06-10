import { useState } from "react";
import { postAuthForgot } from "../lib/api";
import Footer from "../components/Footer.jsx";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [err, setErr] = useState("");
  const [sending, setSending] = useState(false);

  async function submit(e){
    e.preventDefault();
    setErr("");
    if (sending) return;
    setSending(true);
    try{
      await postAuthForgot(email);
      setSent(true);
    }catch(e){
      setErr(e?.message || "Error");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="min-h-screen flex flex-col">
      <main className="flex-1">
        <div className="max-w-sm mx-auto mt-16 border rounded p-4 bg-white">
          <h1 className="text-xl font-semibold mb-3">Recuperar contraseña</h1>
          {sent ? (
            <div className="text-sm text-gray-700">
              Si el correo existe, te enviamos un enlace para restablecer la contraseña.
              Revisa tu bandeja de entrada y el spam.
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-3">
              {err && <div className="bg-red-100 text-red-700 p-2 rounded">{err}</div>}
              <input className="border rounded p-2 w-full" placeholder="mail"
                     value={email} onChange={e=>setEmail(e.target.value)} />
              <button type="submit" disabled={sending || !email.trim()} className={`w-full p-2 rounded text-white ${sending ? "bg-blue-400 cursor-not-allowed" : "bg-blue-600"}`}>
                {sending ? "Enviando..." : "Enviar enlace"}
              </button>
            </form>
          )}
        </div>
      </main>
      <Footer />
    </div>
  );
}

