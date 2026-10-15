import { createContext, useContext, useEffect, useState } from "react";
import { getAuthSession, postAuthLogout, postLogin, setToken } from "../lib/api";
import { registerFeatures } from "@/lib/features";
import { normalizePermissionsMap } from "@/lib/permissions";

// Normalizador local para no generar dependencia circular
function sanitizeUser(u) {
  if (!u) return null;
  const rol = String(u.rol ?? "").trim().toLowerCase();
  const customPermissions = u.custom_permissions || {};
  const customCount = Number(u.permisos_personalizados || Object.keys(customPermissions).length || 0);
  return {
    ...u,
    rol,
    permissions: normalizePermissionsMap(u.permissions),
    custom_permissions: customPermissions,
    has_custom_permissions: Boolean(u.has_custom_permissions || customCount > 0),
    permisos_personalizados: customCount,
  };
}

const AuthCtx = createContext(null);
export const useAuth = () => useContext(AuthCtx);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  async function refreshSession() {
    const data = await getAuthSession();
    const u = data?.user ?? data;
    if (u) {
      if (data?.features) registerFeatures(data.features);
      setUser(sanitizeUser(u));
      return sanitizeUser(u);
    }
    setUser(null);
    return null;
  }

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const data = await getAuthSession();
        if (!active) return;
        // La API de sesion devuelve los campos del usuario en el nivel raiz.
        // Mantener compatibilidad si alguna vez vuelve como { user, features }.
        const u = data?.user ?? data;
        if (u) {
          if (data?.features) registerFeatures(data.features);
          setUser(sanitizeUser(u));
        }
      } catch (err) {
        if (import.meta.env.DEV) {
          console.debug("Auth session bootstrap failed", err);
        }
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  // Los permisos se editan desde Usuarios mientras la persona tiene NEXORA abierto. Sin esto
  // el menú y los botones seguían con lo viejo hasta recargar, aunque el backend ya aplicara
  // el cambio. Se refresca al volver a la pestaña, como mucho una vez por minuto.
  useEffect(() => {
    if (!user?.id) return undefined;
    let lastRefresh = Date.now();
    const onFocus = () => {
      if (document.visibilityState === "hidden") return;
      if (Date.now() - lastRefresh < 60_000) return;
      lastRefresh = Date.now();
      refreshSession().catch(() => {});
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  async function login(email, password) {
    const data = await postLogin(email, password); // { token, user }
    const cleanUser = sanitizeUser(data.user);
    registerFeatures(data.features);
    setToken(data.token);
    setUser(cleanUser);
    setLoading(false);
  }

  async function logout() {
    setToken(null);
    try {
      await postAuthLogout();
    } catch (err) {
      if (import.meta.env.DEV) {
        console.debug("Auth logout request failed", err);
      }
    } finally {
      setUser(null);
      setLoading(false);
    }
  }

  return (
    <AuthCtx.Provider value={{ user, login, logout, loading, refreshSession }}>
      {children}
    </AuthCtx.Provider>
  );
}

export default AuthProvider;

