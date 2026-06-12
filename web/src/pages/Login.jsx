import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import Footer from "../components/Footer.jsx";

function normalizeApiBase(rawValue) {
  const value = String(rawValue || "").trim().replace(/\/+$/, "");
  if (!value) return "";
  return value.endsWith("/api") ? value.slice(0, -4) : value;
}

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [backendOk, setBackendOk] = useState(true);

  const nav = useNavigate();
  const loc = useLocation();
  const { login } = useAuth();

  const params = new URLSearchParams(loc.search || "");
  const nextParam = params.get("next");
  const from = nextParam || loc.state?.from?.pathname || "/";

  useEffect(() => {
    (async () => {
      try {
        const base = normalizeApiBase(import.meta.env.VITE_API_URL);
        const res = await fetch(`${base}/api/ping/`, {
          method: "GET",
          credentials: "omit",
          cache: "no-store",
        });
        if (!res.ok) throw new Error(`Ping failed: ${res.status}`);
        setBackendOk(true);
      } catch {
        setBackendOk(false);
      }
    })();
  }, []);

  async function onSubmit(event) {
    event.preventDefault();
    setErr("");
    setLoading(true);
    try {
      await login(email.trim().toLowerCase(), password);
      nav(from, { replace: true });
    } catch (error) {
      const msg = error?.message || "Credenciales inválidas";
      if (!backendOk) {
        setErr("Backend no disponible en /api. Verifique que la API esté levantada y accesible.");
      } else {
        setErr(msg);
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex flex-col">
      <main className="flex-1">
        <div className="max-w-md mx-auto mt-16 card">
          <div className="mb-4 flex justify-center">
            <img
              src="/branding/logotipo-nexora.png"
              alt="NEXORA"
              className="w-72 max-w-full object-contain"
              onError={(event) => {
                event.currentTarget.onerror = null;
                event.currentTarget.src = "/branding/logo-nexora.png";
              }}
            />
          </div>
          {!backendOk && (
            <div className="mb-3 rounded bg-yellow-100 p-2 text-sm text-yellow-800">
              Backend no disponible.
            </div>
          )}
          <div className="h1 mb-4">Ingresar</div>
          <form className="space-y-3" onSubmit={onSubmit}>
            <input
              className="input"
              type="email"
              placeholder="Mail"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              required
            />
            <input
              className="input"
              type="password"
              placeholder="Contraseña"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
            {err && <div className="text-sm text-red-600">{err}</div>}
            <button className="btn w-full" type="submit" disabled={loading}>
              {loading ? "Ingresando..." : "Entrar"}
            </button>

            <Link to="/recuperar" className="mt-1 inline-block text-sm text-blue-700 underline">
              ¿Olvidaste tu contraseña?
            </Link>
          </form>
        </div>
      </main>
      <Footer />
    </div>
  );
}
