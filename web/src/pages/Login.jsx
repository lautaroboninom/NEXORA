// web/src/pages/Login.jsx
import { useEffect, useState } from "react";
import { Link, useNavigate, useLocation } from "react-router-dom";
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

  // Verificacion rapida del backend para diferenciar error de red vs. credenciales
  useEffect(() => {
    (async () => {
      try {
        const base = normalizeApiBase(import.meta.env.VITE_API_URL);
        const pingUrl = `${base}/api/ping/`;
        const res = await fetch(pingUrl, {
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

  async function onSubmit(e) {
    e.preventDefault();
    setErr("");
    setLoading(true);
    try {
      await login(email.trim().toLowerCase(), password);
      nav(from, { replace: true });
    } catch (e) {
      const msg = e?.message || "Credenciales invalidas";
      if (!backendOk) {
        setErr("Backend no disponible en /api. Verifica que la API este levantada y accesible.");
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
          <div className="flex justify-center mb-4">
            <img
              src="/branding/logo-app.png"
              alt="SEPID Reparaciones"
              className="h-12 object-contain"
              onError={(e) => {
                e.currentTarget.onerror = null;
                e.currentTarget.src = "/icons/logo-app-180.png";
              }}
            />
          </div>
          {!backendOk && (
            <div className="mb-3 text-sm bg-yellow-100 text-yellow-800 p-2 rounded">
              Backend no disponible.
            </div>
          )}
          <div className="h1 mb-4">Ingresar</div>
          <form className="space-y-3" onSubmit={onSubmit}>
            <input
              className="input"
              type="email"
              placeholder="...@sepid.com.ar"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
            />
            <input
              className="input"
              type="password"
              placeholder="Contrasena"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
            {err && <div className="text-red-600 text-sm">{err}</div>}
            <button className="btn w-full" type="submit" disabled={loading}>
              {loading ? "Ingresando..." : "Entrar"}
            </button>

            <Link to="/recuperar" className="text-sm text-blue-700 underline inline-block mt-1">
              Olvidaste tu contrasena?
            </Link>
          </form>
        </div>
      </main>
      <Footer />
    </div>
  );
}

