  // web/src/lib/api.js
import { MOTIVO_OPTIONS } from "./constants";

  // === BASE del API ===
  // Si VITE_API_URL viene con sufijo /api, lo normalizamos para evitar /api/api.
  function normalizeApiBase(rawValue) {
    const value = String(rawValue || "").trim().replace(/\/+$/, "");
    if (!value) return "";
    return value.endsWith("/api") ? value.slice(0, -4) : value;
  }

  const BASE = normalizeApiBase(import.meta.env.VITE_API_URL);

  export function resolveApiUrl(path) {
    const value = String(path || "");
    return value.startsWith("http") ? value : `${BASE}${value}`;
  }

  const bejermanActivityListeners = new Set();
  const bejermanActivityRequests = new Map();
  let bejermanActivitySeq = 0;

  function normalizedActivityPath(path) {
    const value = String(path || "");
    if (!value) return "";
    try {
      const origin = typeof window !== "undefined" && window.location ? window.location.origin : "http://localhost";
      const url = value.startsWith("http") ? new URL(value) : new URL(resolveApiUrl(value), origin);
      return `${url.pathname}${url.search}`;
    } catch (_) {
      return value;
    }
  }

  function bejermanActivityInfo(path, method = "GET") {
    const requestPath = normalizedActivityPath(path);
    const pathname = requestPath.split("?")[0] || "";
    const upperMethod = String(method || "GET").toUpperCase();

    if (pathname.includes("/api/auth/bejerman-")) return null;
    if (
      pathname.includes("/api/bejerman/jobs/") ||
      pathname.includes("/api/bejerman/pdf-output-settings/") ||
      pathname.includes("/api/bejerman/ingress-companies/")
    ) {
      return null;
    }
    if (pathname.includes("/api/cobranzas/facturacion/documentos/") && pathname.includes("/pdf/")) {
      return {
        title: "Preparando PDF de facturación",
        status: "Consultando Bejerman",
        detail: "NEXORA está esperando el comprobante para abrirlo cuando esté listo.",
      };
    }
    if (pathname.includes("/api/cobranzas/facturacion/documentos/")) {
      return {
        title: "Consultando facturación",
        status: "Buscando comprobantes en Bejerman",
        detail: "NEXORA está cargando la información solicitada.",
      };
    }
    if (pathname.includes("/api/cobranzas/remitos/") && pathname.includes("/pdf/")) {
      return {
        title: "Preparando PDF de remito",
        status: "Consultando Bejerman",
        detail: "NEXORA está esperando el PDF del remito.",
      };
    }
    if (pathname.includes("/api/cobranzas/remitos/")) {
      return {
        title: "Consultando remitos",
        status: "Buscando remitos en Bejerman",
        detail: "NEXORA está cargando la información solicitada.",
      };
    }
    if (pathname.includes("/api/catalogos/clientes/sincronizar-bejerman/")) {
      return {
        title: "Sincronizando clientes",
        status: "Consultando Bejerman",
        detail: "NEXORA está actualizando datos de clientes.",
      };
    }
    if (pathname.includes("/api/catalogos/clientes/bejerman-candidatos/")) {
      return {
        title: "Consultando clientes",
        status: "Buscando candidatos en Bejerman",
        detail: "NEXORA está cargando coincidencias.",
      };
    }
    if (pathname.includes("/api/ingresos/ris/preflight/") || pathname.includes("/ris/preflight/")) {
      return {
        title: "Validando remito",
        status: "Consultando Bejerman",
        detail: "NEXORA está verificando los datos antes de emitir.",
      };
    }
    if (pathname.includes("/api/ingresos/") && pathname.includes("/bejerman-estado/")) {
      return {
        title: "Consultando estado",
        status: "Buscando el equipo en Bejerman",
        detail: "NEXORA está cargando el estado actualizado.",
      };
    }
    if (pathname.includes("/api/ordenes-entrega/remito-bejerman/") && pathname.includes("/pdf/")) {
      return {
        title: "Preparando PDF de remito",
        status: "Consultando Bejerman",
        detail: "NEXORA está esperando el PDF del remito.",
      };
    }
    if (pathname.includes("/api/ordenes-entrega/remito-bejerman/historial/")) {
      return {
        title: "Consultando historial de remitos",
        status: "Buscando datos en Bejerman",
        detail: "NEXORA está cargando el historial solicitado.",
      };
    }
    if (pathname.includes("/api/ordenes-entrega/bejerman-")) {
      return {
        title: "Consultando Bejerman",
        status: "Buscando artículos y stock",
        detail: "NEXORA está cargando datos de Bejerman.",
      };
    }
    if (pathname.includes("/api/bejerman/purchase-entries/") && upperMethod !== "GET") {
      return {
        title: "Procesando ingreso de mercadería",
        status: "Enviando datos a Bejerman",
        detail: "NEXORA está esperando la respuesta de Bejerman.",
      };
    }
    if (pathname.includes("/api/bejerman/purchase-")) {
      return {
        title: "Consultando mercadería",
        status: "Buscando datos en Bejerman",
        detail: "NEXORA está cargando la información solicitada.",
      };
    }
    if (pathname.includes("/api/bejerman/")) {
      return {
        title: "Consultando Bejerman",
        status: "Esperando respuesta",
        detail: "NEXORA está cargando información de Bejerman.",
      };
    }
    return null;
  }

  function currentBejermanActivity() {
    const items = Array.from(bejermanActivityRequests.values());
    const latest = items[items.length - 1] || null;
    return {
      active: items.length > 0,
      count: items.length,
      title: latest?.title || "Consultando Bejerman",
      status: latest?.status || "Esperando respuesta",
      detail: latest?.detail || "NEXORA está cargando información de Bejerman.",
    };
  }

  function notifyBejermanActivity() {
    const state = currentBejermanActivity();
    bejermanActivityListeners.forEach((listener) => {
      try {
        listener(state);
      } catch (_) {}
    });
  }

  function beginBejermanActivity(path, { method = "GET", skipBejermanActivity = false, bejermanActivity = null } = {}) {
    if (skipBejermanActivity) return () => {};
    const info = bejermanActivity || bejermanActivityInfo(path, method);
    if (!info) return () => {};
    const id = ++bejermanActivitySeq;
    bejermanActivityRequests.set(id, info);
    notifyBejermanActivity();
    return () => {
      if (!bejermanActivityRequests.delete(id)) return;
      notifyBejermanActivity();
    };
  }

  export function subscribeBejermanActivity(listener) {
    if (typeof listener !== "function") return () => {};
    bejermanActivityListeners.add(listener);
    listener(currentBejermanActivity());
    return () => {
      bejermanActivityListeners.delete(listener);
    };
  }

  function htmlErrorMessage(status, fallback = "No se pudo completar la descarga.") {
    if ([502, 503, 504].includes(Number(status))) {
      return "Bejerman no pudo devolver el PDF en este momento. Probá de nuevo en unos minutos.";
    }
    return fallback;
  }

  function looksLikeHtmlResponse(text, contentType = "") {
    const value = String(text || "").trimStart().toLowerCase();
    return String(contentType || "").toLowerCase().includes("html") || value.startsWith("<!doctype") || value.startsWith("<html");
  }

  /* ===== Token en memoria (compatibilidad) ===== */
  let token = null;
  export const setToken = (t) => {
    token = t;
  };

  /* ===== Logout forzado ante 401 ===== */
  let forcingLogout = false;
  function forceLogout() {
    if (forcingLogout) return;
    forcingLogout = true;
    try {
      setToken(null);
    } finally {
      const path = window.location.pathname || "";
      const search = window.location.search || "";
      const hash = window.location.hash || "";
      const current = `${path}${search}${hash}` || "/";
      // No redirigir si estamos en rutas pblicas de auth
      const safePaths = new Set(["/login", "/restablecer", "/recuperar"]);
      if (!safePaths.has(path)) {
        const next = encodeURIComponent(current);
        window.location.replace(`/login?next=${next}`);
        return;
      }
      // Mantenernos en la ruta pblica actual
      forcingLogout = false;
    }
  }

  /* ===== Wrapper HTTP ===== */
  async function http(path, { method = "GET", body, headers, skipBejermanActivity = false, bejermanActivity = null } = {}) {
    const endBejermanActivity = beginBejermanActivity(path, { method, skipBejermanActivity, bejermanActivity });
    try {
      const res = await fetch(`${BASE}${path}`, {
        method,
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(headers || {}),
        },
        body: body ? JSON.stringify(body) : undefined,
      });

    const ct = res.headers.get("content-type") || "";
    const isJSON = ct.includes("application/json");
    const data = isJSON ? await res.json() : await res.text();
    

    if (res.status === 401) {
      // Evitar redirigir desde pginas pblicas de auth
      const p = window.location.pathname || "";
      const publicAuth = p.startsWith("/restablecer") || p.startsWith("/recuperar") || p === "/login";
      if (!publicAuth) forceLogout();
    }

    // Algunos despliegues pueden responder 403 con mensajes de no autenticado.
    if (res.status === 403) {
      const msg = (typeof data === "string" ? data : (data?.detail || ""))?.toString().toLowerCase();
      const looksUnauth =
        msg.includes("credentials were not provided") ||
        msg.includes("not authenticated") ||
        msg.includes("no autenticado") ||
        msg.includes("token expirado") ||
        msg.includes("token inválido") ||
        msg.includes("token inválido");
      if (looksUnauth) {
        const p = window.location.pathname || "";
        const publicAuth = p.startsWith("/restablecer") || p.startsWith("/recuperar") || p === "/login";
        if (!publicAuth) forceLogout();
      }
    }

    if (!res.ok) {
      const msg =
        typeof data === "string" ? data : data.detail || JSON.stringify(data);
      const err = new Error(`${res.status} ${res.statusText}: ${msg}`);
      err.status = res.status;
      err.data = data;
      err.response = res;
      throw err;
    }
      return data;
    } finally {
      endBejermanActivity();
    }
  }

  /* API cruda para quien prefiera */
  export const api = {
    get: (p, opts) => http(p, { ...opts, method: "GET" }),
    post: (p, body, opts) => http(p, { ...opts, method: "POST", body }),
    put: (p, body, opts) => http(p, { ...opts, method: "PUT", body }),
    patch: (p, body, opts) => http(p, { ...opts, method: "PATCH", body }),
    del: (p, opts) => http(p, { ...opts, method: "DELETE" }),
    delete: (p, opts) => http(p, { ...opts, method: "DELETE" }),
  };
  export default api;

  export function fetchWithAuth(path, opts = {}) {
    const authHeader = token ? { Authorization: `Bearer ${token}` } : {};
    const {
      headers: extraHeaders,
      skipBejermanActivity = false,
      bejermanActivity = null,
      ...restOpts
    } = opts || {};
    const method = restOpts?.method || "GET";
    const endBejermanActivity = beginBejermanActivity(path, { method, skipBejermanActivity, bejermanActivity });
    return fetch(resolveApiUrl(path), {
      credentials: "include",
      ...restOpts,
      headers: {
        ...authHeader,
        ...(extraHeaders || {}),
      },
    }).finally(endBejermanActivity);
  }

  /* ================== AUTH ================== */
  export const postLogin = (email, password) =>
    api.post("/api/auth/login/", { email, password });
  export const postAuthForgot = (email) =>
    api.post("/api/auth/forgot/", { email });
  export const postAuthReset = (token, password) =>
    api.post("/api/auth/reset/", { token, password });
  export const getAuthSession = () => api.get("/api/auth/session/");
  export const postAuthLogout = () => api.post("/api/auth/logout/");
  export const getBejermanCredentials = () => api.get("/api/auth/bejerman-credentials/");
  export const postBejermanCredentials = (payload) =>
    api.post("/api/auth/bejerman-credentials/", payload);
  export const getBejermanSellerCode = () => api.get("/api/auth/bejerman-seller-code/");
  export const postBejermanSellerCode = (payload) =>
    api.post("/api/auth/bejerman-seller-code/", payload);


  /* =============== USUARIOS ================= */
  export const getUsuarios = () => api.get("/api/usuarios/");
  export const postUsuario = (payload) => api.post("/api/usuarios/", payload);
  export const patchUsuarioActivo = (id, activo) =>
    api.patch(`/api/usuarios/${id}/activar/`, { activo });
  // Enviar enlace de restablecimiento/invitacin por email
  export const patchUsuarioReset = (id) =>
    api.patch(`/api/usuarios/${id}/reset-pass/`, {});
  export const patchUsuarioRolePerm = (id, payload) =>
    api.patch(`/api/usuarios/${id}/roleperm/`, payload);
  export const deleteUsuario = (id) => api.del(`/api/usuarios/${id}/`);
  export const getPermisosCatalogo = () => api.get("/api/permisos/catalogo/");
  export const getUsuarioPermisos = (id) => api.get(`/api/usuarios/${id}/permisos/`);
  export const putUsuarioPermisos = (id, payload) =>
    api.put(`/api/usuarios/${id}/permisos/`, payload);
  export const postUsuarioPermisosReset = (id) =>
    api.post(`/api/usuarios/${id}/permisos/reset/`, {});
  export const getUsuarioNotificaciones = (id) =>
    api.get(`/api/usuarios/${id}/notificaciones/`);
  export const putUsuarioNotificaciones = (id, payload) =>
    api.put(`/api/usuarios/${id}/notificaciones/`, payload);
  export const getNotificaciones = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/notificaciones/${qs ? `?${qs}` : ""}`);
  };
  export const postNotificacionClick = (id) =>
    api.post(`/api/notificaciones/${id}/click/`, {});
  export const postNotificacionesReadAll = () =>
    api.post("/api/notificaciones/read-all/", {});
  export const getPushNotificationConfig = () =>
    api.get("/api/notificaciones/push/config/");
  export const postPushNotificationSubscription = (payload) =>
    api.post("/api/notificaciones/push/subscription/", payload);
  export const deletePushNotificationSubscription = (payload = {}) =>
    api.delete("/api/notificaciones/push/subscription/", { body: payload });
  export const getNotificationSettings = () =>
    api.get("/api/notificaciones/configuracion/");
  export const putNotificationSettings = (payload) =>
    api.put("/api/notificaciones/configuracion/", payload);
  export const postNotificationEmail = (payload) =>
    api.post("/api/notificaciones/configuracion/emails/", payload);
  export const patchNotificationEmail = (id, payload) =>
    api.patch(`/api/notificaciones/configuracion/emails/${id}/`, payload);
  export const deleteNotificationEmail = (id) =>
    api.delete(`/api/notificaciones/configuracion/emails/${id}/`);

  /* =============== catalogos =============== */


const catalogCache = {
  marcas: null,
  tipos: new Map(),
  modelos: new Map(),
  variantes: new Map(),
};

const catalogCacheKey = (...parts) => parts.filter(part => part !== undefined && part !== null).join(":");

export const clearCatalogCache = () => {
  catalogCache.marcas = null;
  catalogCache.tipos.clear();
  catalogCache.modelos.clear();
  catalogCache.variantes.clear();
};

export async function getCatalogMarcas(force = false) {
  if (!force && catalogCache.marcas) {
    return catalogCache.marcas;
  }
  const data = await api.get("/api/catalogo/marcas/");
  catalogCache.marcas = data;
  return data;
}

export async function getCatalogTipos(marcaId, force = false) {
  const key = String(marcaId ?? "");
  if (!force && catalogCache.tipos.has(key)) {
    return catalogCache.tipos.get(key);
  }
  if (marcaId == null || marcaId === "") {
    catalogCache.tipos.set(key, []);
    return [];
  }
  const data = await api.get(`/api/catalogo/marcas/${encodeURIComponent(marcaId)}/tipos/`);
  catalogCache.tipos.set(key, data);
  return data;
}

export async function getCatalogModelos(marcaId, tipoId, force = false) {
  const key = catalogCacheKey(marcaId, tipoId);
  if (!force && catalogCache.modelos.has(key)) {
    return catalogCache.modelos.get(key);
  }
  if (!marcaId || !tipoId) {
    catalogCache.modelos.set(key, []);
    return [];
  }
  const data = await api.get(
    `/api/catalogo/marcas/${encodeURIComponent(marcaId)}/tipos/${encodeURIComponent(tipoId)}/modelos/`
  );
  catalogCache.modelos.set(key, data);
  return data;
}

export async function getCatalogVariantes(marcaId, tipoId, modeloId, force = false) {
  const key = catalogCacheKey("catalog", marcaId, tipoId, modeloId);
  if (!force && catalogCache.variantes.has(key)) {
    return catalogCache.variantes.get(key);
  }
  if (!marcaId || !modeloId) {
    catalogCache.variantes.set(key, []);
    return [];
  }
  const data = await api.get(
    `/api/catalogo/marcas/${encodeURIComponent(marcaId)}/modelos/${encodeURIComponent(modeloId)}/variantes/`
  );
  catalogCache.variantes.set(key, data);
  return data;
}

export async function getVariantesPorModelo(modeloId, force = false) {
  const key = catalogCacheKey("model", modeloId);
  if (!force && catalogCache.variantes.has(key)) {
    return catalogCache.variantes.get(key);
  }
  if (!modeloId) {
    catalogCache.variantes.set(key, []);
    return [];
  }
  const data = await api.get(`/api/catalogos/modelos/${encodeURIComponent(modeloId)}/variantes/`);
  catalogCache.variantes.set(key, data);
  return data;
}

// Marcas que soportan un tipo dado (por nombre)
export async function getMarcasPorTipo(tipoNombre) {
  const name = encodeURIComponent(tipoNombre || "");
  if (!name) return [];
  return api.get(`/api/catalogo/tipos/${name}/marcas/`);
}

// Tipos (ABM por marca)
export const postCatalogTipo = (payload) =>
  api.post("/api/catalogo/tipos-equipo/", payload);

export const patchCatalogTipo = (tipoId, payload) =>
  api.patch(`/api/catalogo/tipos-equipo/${tipoId}/`, payload);

export const deleteCatalogTipo = (tipoId) =>
  api.del(`/api/catalogo/tipos-equipo/${tipoId}/`);

export const postCatalogModelo = (payload) =>
  api.post("/api/catalogo/modelos/", payload);

export const patchCatalogModelo = (modeloId, payload) =>
  api.patch(`/api/catalogo/modelos/${modeloId}/`, payload);

export const deleteCatalogModelo = (modeloId) =>
  api.del(`/api/catalogo/modelos/${modeloId}/`);

// Aliases de compat (antes se llamaban 'serie')
export const postCatalogSerie = postCatalogModelo;
export const patchCatalogSerie = patchCatalogModelo;
export const deleteCatalogSerie = deleteCatalogModelo;

export const postCatalogVariante = (payload) =>
  api.post("/api/catalogo/variantes/", payload);

export const patchCatalogVariante = (varianteId, payload) =>
  api.patch(`/api/catalogo/variantes/${varianteId}/`, payload);

export const deleteCatalogVariante = (varianteId) =>
  api.del(`/api/catalogo/variantes/${varianteId}/`);


  export const getClientes = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/catalogos/clientes/${qs ? `?${qs}` : ""}`);
  };
  export const getClientesBasico = () => api.get("/api/clientes/");
  export const postCliente = (payload) =>
    api.post("/api/catalogos/clientes/", payload);
  export const patchCliente = (id, payload) =>
    api.patch(`/api/catalogos/clientes/${id}/`, payload);
  export const deleteCliente = (id) =>
    api.del(`/api/catalogos/clientes/${id}/`);
  export const postClienteMerge = (sourceId, targetId) =>
    api.post("/api/catalogos/clientes/merge/", { source_id: sourceId, target_id: targetId });
  export const postClienteBejermanSync = (payload = {}) =>
    api.post("/api/catalogos/clientes/sincronizar-bejerman/", payload);
  export const getClienteBejermanCandidates = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/catalogos/clientes/bejerman-candidatos/${qs ? `?${qs}` : ""}`);
  };
  export const getRoles = () => api.get("/api/catalogos/roles/");
  export const getMarcas = () => api.get("/api/catalogos/marcas/");
  export const postMarca = (nombre) =>
    api.post("/api/catalogos/marcas/", { nombre });
  export const deleteMarca = (id) =>
    api.del(`/api/catalogos/marcas/${id}/`);
  // Eliminacin en cascada: borra la marca y TODOS sus modelos
  export const deleteMarcaCascade = (id) =>
    api.del(`/api/catalogos/marcas/${id}/eliminar-con-modelos/`);
  export const patchMarca = (id, payload) =>
    api.patch(`/api/catalogos/marcas/${id}/`, payload);

  // Unificar marcas
  export const postMarcaMerge = (sourceId, targetId, opts = {}) =>
    api.post(`/api/catalogos/marcas/merge/`, { source_id: sourceId, target_id: targetId, ...(opts || {}) });

  export const getTiposEquipo = () =>
    api.get("/api/catalogos/tipos-equipo/");

  export const getTestProtocols = () =>
    api.get("/api/catalogos/tests/protocolos/");
  export const postTestProtocol = (payload) =>
    api.post("/api/catalogos/tests/protocolos/", payload);
  export const getTestProtocol = (protocolId) =>
    api.get(`/api/catalogos/tests/protocolos/${protocolId}/`);
  export const patchTestProtocol = (protocolId, payload) =>
    api.patch(`/api/catalogos/tests/protocolos/${protocolId}/`, payload);
  export const deleteTestProtocol = (protocolId) =>
    api.del(`/api/catalogos/tests/protocolos/${protocolId}/`);

  // ABM Tipos de equipo (catlogo general)
  export const getTiposEquipoAdmin = () =>
    api.get("/api/catalogos/tipos-equipo-admin/");
  export const postTipoEquipo = (nombre) =>
    api.post("/api/catalogos/tipos-equipo-admin/", { nombre });
  export const patchTipoEquipo = (id, payload) =>
    api.patch(`/api/catalogos/tipos-equipo-admin/${id}/`, payload);
  export const deleteTipoEquipo = (id) =>
    api.del(`/api/catalogos/tipos-equipo-admin/${id}/`);

  export const patchModeloTipoEquipo = (marcaId, modeloId, payload) =>
    api.patch(`/api/catalogos/marcas/${marcaId}/modelos/${modeloId}/tipo-equipo/`, payload);

  export const getModelosByBrand = (brandId) =>
    api.get(`/api/catalogos/marcas/${brandId}/modelos/`);
  export const getModelos = getModelosByBrand; // alias por compatibilidad
export const postModelo = (brandId, payloadOrNombre) => {
  const payload = typeof payloadOrNombre === "string"
    ? { nombre: payloadOrNombre }
    : (payloadOrNombre || {});
  return api.post(`/api/catalogos/marcas/${brandId}/modelos/`, payload);
};
  export const deleteModelo = (id) =>
    api.del(`/api/catalogos/modelos/${id}/`);
  export const patchModelo = (id, payload) =>
    api.patch(`/api/catalogos/modelos/${id}/`, payload);

  // Unificar modelos (mueve devices del source al target y elimina el duplicado)
  export const postModelMerge = (sourceId, targetId) =>
    api.post(`/api/catalogos/modelos/merge/`, { source_id: sourceId, target_id: targetId });

  export const getUbicaciones = () => api.get("/api/catalogos/ubicaciones/");
  export const getMotivos = async () => {
    try {
      const res = await api.get("/api/catalogos/motivos/");
      const arr = Array.isArray(res) ? res : [];
      return arr.length ? arr : (MOTIVO_OPTIONS || []);
    } catch (_) {
      return MOTIVO_OPTIONS || [];
    }
  };
  export const getAccesoriosCatalogo = () => api.get("/api/catalogos/accesorios/");
  export const getRepuestosCatalogo = (params = {}) => {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.limit) qs.set("limit", params.limit);
    const equipoHints = Array.isArray(params.equipo_hint)
      ? params.equipo_hint
      : params.equipo_hint
      ? [params.equipo_hint]
      : [];
    equipoHints
      .map((value) => String(value || "").trim())
      .filter(Boolean)
      .forEach((value) => qs.append("equipo_hint", value));
    const qstr = qs.toString();
    return api.get(`/api/catalogos/repuestos/${qstr ? `?${qstr}` : ""}`);
  };
  export const getRepuestos = (params = {}) => {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.limit) qs.set("limit", params.limit);
    if (params.offset) qs.set("offset", params.offset);
    if (params.order) qs.set("order", params.order);
    if (params.dir) qs.set("dir", params.dir);
    const qstr = qs.toString();
    return api.get(`/api/repuestos/${qstr ? `?${qstr}` : ""}`);
  };
  export const getRepuestosSubrubros = () =>
    api.get("/api/repuestos/subrubros/");
  export const postRepuestosSubrubro = (payload) =>
    api.post("/api/repuestos/subrubros/", payload);
  export const patchRepuestosSubrubro = (subrubroCodigo, payload) =>
    api.patch(`/api/repuestos/subrubros/${subrubroCodigo}/`, payload);
  export const deleteRepuestosSubrubro = (subrubroCodigo) =>
    api.del(`/api/repuestos/subrubros/${subrubroCodigo}/`);
  export const getRepuestosConfig = () => api.get("/api/repuestos/config/");
  export const patchRepuestosConfig = (payload) =>
    api.patch("/api/repuestos/config/", payload);
  export const getRepuestoDetalle = (repuestoId) =>
    api.get(`/api/repuestos/${repuestoId}/`);
  export const postRepuesto = (payload) =>
    api.post("/api/repuestos/", payload);
  export const patchRepuesto = (repuestoId, payload) =>
    api.patch(`/api/repuestos/${repuestoId}/`, payload);
  export const postRepuestosMovimientoCompra = (payload) =>
    api.post("/api/repuestos/movimientos/compra/", payload);
  export const getRepuestosMovimientos = (params = {}) => {
    const qs = new URLSearchParams();
    if (params.repuesto_id) qs.set("repuesto_id", params.repuesto_id);
    if (params.limit) qs.set("limit", params.limit);
    const qstr = qs.toString();
    return api.get(`/api/repuestos/movimientos/${qstr ? `?${qstr}` : ""}`);
  };
  export const getRepuestosCambios = (params = {}) => {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.limit) qs.set("limit", params.limit);
    if (params.offset) qs.set("offset", params.offset);
    const qstr = qs.toString();
    return api.get(`/api/repuestos/cambios/${qstr ? `?${qstr}` : ""}`);
  };
  export const getRepuestosStockPermisos = () =>
    api.get("/api/repuestos/stock-permisos/");
  export const postRepuestosStockPermiso = (payload) =>
    api.post("/api/repuestos/stock-permisos/", payload);
  export const patchRepuestosStockPermiso = (permId, payload) =>
    api.patch(`/api/repuestos/stock-permisos/${permId}/`, payload);
  export const deleteRepuesto = (repuestoId) =>
    api.del(`/api/repuestos/${repuestoId}/`);

  export const getProveedoresExternos = () =>
    api.get("/api/catalogos/proveedores-externos/");
  export const postProveedorExterno = (payload) =>
    api.post("/api/catalogos/proveedores-externos/", payload);
  export const deleteProveedorExterno = (id) =>
    api.del(`/api/catalogos/proveedores-externos/${id}/`);

  /* =============== INGRESOS ================= */
  export const postNuevoIngreso = (payload) =>
    api.post("/api/ingresos/nuevo/", payload);
  export const postNuevoIngresoLote = (payload) =>
    api.post("/api/ingresos/nuevo/lote/", payload);
  export const getBejermanIngressCompanies = () =>
    api.get("/api/bejerman/ingress-companies/");
  export const getIngresoRisStatus = (ingresoId) =>
    api.get(`/api/ingresos/${ingresoId}/ris/`);
  export const postRisPreflight = (payload) =>
    api.post("/api/ingresos/ris/preflight/", payload);
  export const postIngresoRisPreflight = (ingresoId) =>
    api.post(`/api/ingresos/${ingresoId}/ris/preflight/`, {});
  export const postRisPreflightCustomerFix = (payload) =>
    api.post("/api/ingresos/ris/preflight/customer-fix/", payload);
  export const postRisPreflightArticleFix = (payload) =>
    api.post("/api/ingresos/ris/preflight/article-fix/", payload);
  export const postIngresoRisEmitir = (ingresoId) =>
    api.post(`/api/ingresos/${ingresoId}/ris/emitir/`);
  export const getIngresoRisPdfBlob = (ingresoId) =>
    getBlob(`/api/ingresos/${ingresoId}/ris/pdf/`);
  export const getSerialBarcodeBlob = (value, params = {}) => {
    const qs = new URLSearchParams({ value: value || "", ...(params || {}) }).toString();
    return getBlob(`/api/barcodes/serial/?${qs}`);
  };
  export const getIngresoBarcodeBlob = (ingresoId) =>
    getBlob(`/api/ingresos/${ingresoId}/barcode/`);
  export const postDerivarIngreso = (ingresoId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/derivar/`, payload);
  export const getDerivacionesPorIngreso = (ingresoId) =>
    api.get(`/api/ingresos/${ingresoId}/derivaciones/`);
  export const postDerivacionDevuelto = (ingresoId, derivId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/derivaciones/${derivId}/devolver/`, payload);
  // Accesorios por ingreso
  export const getAccesoriosPorIngreso = (ingresoId) =>
    api.get(`/api/ingresos/${ingresoId}/accesorios/`);
  export const postAccesorioIngreso = (ingresoId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/accesorios/`, payload);
  export const deleteAccesorioIngreso = (ingresoId, itemId) =>
    api.del(`/api/ingresos/${ingresoId}/accesorios/${itemId}/`);

  // Accesorios de alquiler por ingreso
  export const getAccesoriosAlquilerPorIngreso = (ingresoId) =>
    api.get(`/api/ingresos/${ingresoId}/alquiler/accesorios/`);
  export const postAccesorioAlquilerIngreso = (ingresoId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/alquiler/accesorios/`, payload);
  export const deleteAccesorioAlquilerIngreso = (ingresoId, itemId) =>
    api.del(`/api/ingresos/${ingresoId}/alquiler/accesorios/${itemId}/`);

  export const getIngresoFotos = (ingresoId, params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/ingresos/${ingresoId}/fotos/${qs ? `?${qs}` : ""}`);
  };

  export async function uploadIngresoFotos(ingresoId, files) {
    const form = new FormData();
    (files || []).forEach((file) => {
      if (file) form.append('files', file);
    });
    const res = await fetch(`${BASE}/api/ingresos/${ingresoId}/fotos/`, {
      method: "POST",
      credentials: "include",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: form,
    });
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (res.status === 401 || res.status === 403) {
      forceLogout();
    }
    if (!res.ok) {
      const detail = typeof data === "string" ? data : data.detail || JSON.stringify(data);
      throw new Error(`${res.status} ${res.statusText}: ${detail}`);
    }
    return data;
  }

  // ---- Descarga/lectura de binarios con autorizacin ----
  function toAbsoluteUrl(pathOrUrl) {
    if (!pathOrUrl) return "";
    if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
    // Acepta paths relativos empezando con '/'
    return `${BASE}${pathOrUrl}`;
  }

  function parseDispositionFilename(header) {
    if (!header) return null;
    // Priorizar filename*=UTF-8''...
    const star = header.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
    if (star && star[1]) {
      try {
        return decodeURIComponent(star[1].trim());
      } catch (_) {
        // fallthrough
      }
    }
    const simple = header.match(/filename\s*=\s*"([^"]+)"/i) || header.match(/filename\s*=\s*([^;]+)/i);
    if (simple && simple[1]) return simple[1].trim().replace(/^"|"$/g, "");
    return null;
  }

  export async function fetchBlobAuth(pathOrUrl) {
    const url = toAbsoluteUrl(pathOrUrl);
    const res = await fetch(url, {
      method: "GET",
      credentials: "include",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    });
    if (res.status === 401) {
      forceLogout();
    }
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ""}`);
    }
    const blob = await res.blob();
    return { blob, res };
  }

  export async function downloadAuth(pathOrUrl, fallbackName = "archivo") {
    const { blob, res } = await fetchBlobAuth(pathOrUrl);
    const dispo = res.headers.get("content-disposition") || "";
    const name = parseDispositionFilename(dispo) || fallbackName || "archivo";
    const url = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  export const patchIngresoFoto = (ingresoId, mediaId, payload) =>
    api.patch(`/api/ingresos/${ingresoId}/fotos/${mediaId}/`, payload);

  export const deleteIngresoFoto = (ingresoId, mediaId) =>
    api.del(`/api/ingresos/${ingresoId}/fotos/${mediaId}/`);

  // Bsqueda por referencia de accesorio
  export const buscarAccesorioPorRef = (ref) =>
    api.get(`/api/accesorios/buscar/?ref=${encodeURIComponent(ref||"")}`);
  // Lectura de QR / código de barras
  export const lookupScan = (code) =>
    api.get(`/api/scan/lookup/?code=${encodeURIComponent(code||"")}`);
  // Entregar (requiere remito; opcional factura y fecha; si resolucion=cambio: serial_confirm requerido)
  export const postEntregarIngreso = (ingresoId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/entregar/`, payload);
  // Marcar baja
  export const postBajaIngreso = (ingresoId) =>
    api.post(`/api/ingresos/${ingresoId}/baja/`, {});
  // Marcar alta (reactivar desde baja)
  export const postAltaIngreso = (ingresoId) =>
    api.post(`/api/ingresos/${ingresoId}/alta/`, {});
  export const postIngresoConvertirPropioMg = (ingresoId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/convertir-propio-mg/`, payload || {});
  // Correcciones históricas forzadas
  export const postIngresoCorreccionHistorica = (ingresoId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/correcciones-historicas/`, payload || {});
  export const getPendientesGeneral = () => api.get("/api/ingresos/pendientes/");
  export const getPendientesPresupuesto = () =>
    api.get("/api/presupuestos/pendientes/");
  export const getAprobadosParaReparar = () =>
    api.get("/api/ingresos/aprobados-para-reparar/");
  export const getAprobadosYReparados = () =>
    api.get("/api/ingresos/aprobados-reparados/");
  export const getLiberados = () => api.get("/api/ingresos/liberados/");
  export const getTecnicos = () => api.get("/api/catalogos/tecnicos/");

  export const getWorkResumen = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/trabajo/resumen/${qs ? `?${qs}` : ""}`);
  };
  export const getWorkObjetivos = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/trabajo/objetivos/${qs ? `?${qs}` : ""}`);
  };
  export const putWorkObjetivos = (payload) =>
    api.put("/api/trabajo/objetivos/", payload);
  export const getWorkAlertRules = () => api.get("/api/trabajo/reglas-alerta/");
  export const patchWorkAlertRules = (payload) =>
    api.patch("/api/trabajo/reglas-alerta/", payload);
  export const getGlobalSearch = (q) =>
    api.get(`/api/busqueda/global/?q=${encodeURIComponent(q || "")}`);

  const buildQuery = (params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === "") return;
      if (Array.isArray(value)) {
        value.forEach((item) => {
          if (item === undefined || item === null || item === "") return;
          qs.append(key, String(item));
        });
        return;
      }
      qs.set(key, String(value));
    });
    return qs.toString();
  };

  export const getBejermanJobs = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/jobs/${qs ? `?${qs}` : ""}`);
  };

  export const getBejermanPdfOutputSettings = () =>
    api.get("/api/bejerman/pdf-output-settings/");

  export const putBejermanPdfOutputSettings = (payload = {}) =>
    api.put("/api/bejerman/pdf-output-settings/", payload);

  export const postBejermanJobRetry = (jobId) =>
    api.post(`/api/bejerman/jobs/${jobId}/retry/`, {});

  export const getBejermanRemitoProcesses = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/remitos/${qs ? `?${qs}` : ""}`);
  };

  export const postBejermanRemitoProcessRetry = (source, processId, payload = {}) =>
    api.post(`/api/bejerman/remitos/${encodeURIComponent(source)}/${encodeURIComponent(processId)}/retry/`, payload);

  export const getBejermanArticles = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/articles/${qs ? `?${qs}` : ""}`);
  };

  export const getBejermanArticleMappings = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/article-mappings/${qs ? `?${qs}` : ""}`);
  };

  export const postBejermanArticleMapping = (payload) =>
    api.post("/api/bejerman/article-mappings/", payload);

  export const getBejermanPurchaseProviders = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/purchase-providers/${qs ? `?${qs}` : ""}`);
  };

  export const getBejermanPurchaseArticles = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/purchase-articles/${qs ? `?${qs}` : ""}`);
  };

  export const getBejermanPurchaseEntries = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/purchase-entries/${qs ? `?${qs}` : ""}`);
  };

  export const postBejermanPurchaseEntry = (payload = {}) =>
    api.post("/api/bejerman/purchase-entries/", payload);

  export const getBejermanPurchaseEntry = (entryId) =>
    api.get(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/`);

  export const patchBejermanPurchaseEntry = (entryId, payload = {}) =>
    api.patch(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/`, payload);

  export const deleteBejermanPurchaseEntry = (entryId) =>
    api.delete(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/`);

  export const postBejermanPurchaseEntryLine = (entryId, payload = {}) =>
    api.post(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/lines/`, payload);

  export const patchBejermanPurchaseEntryLine = (entryId, lineId, payload = {}) =>
    api.patch(
      `/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/lines/${encodeURIComponent(lineId)}/`,
      payload
    );

  export const deleteBejermanPurchaseEntryLine = (entryId, lineId) =>
    api.delete(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/lines/${encodeURIComponent(lineId)}/`);

  export const postBejermanPurchaseEntryScan = (entryId, lineId, payload = {}) =>
    api.post(
      `/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/lines/${encodeURIComponent(lineId)}/scans/`,
      payload
    );

  export const patchBejermanPurchaseEntryScan = (entryId, scanId, payload = {}) =>
    api.patch(
      `/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/scans/${encodeURIComponent(scanId)}/`,
      payload
    );

  export const deleteBejermanPurchaseEntryScan = (entryId, scanId) =>
    api.delete(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/scans/${encodeURIComponent(scanId)}/`);

  export const postBejermanPurchaseEntryValidate = (entryId, payload = {}) =>
    api.post(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/validate/`, payload);

  export const postBejermanPurchaseEntryEmit = (entryId, payload = {}) =>
    api.post(`/api/bejerman/purchase-entries/${encodeURIComponent(entryId)}/emit/`, payload);

  export const getBejermanPurchaseHistory = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/bejerman/purchase-entries/historial/${qs ? `?${qs}` : ""}`);
  };

  /* =============== NEXORA: ÓRDENES Y COBRANZAS =============== */
  export const getDeliveryOrders = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/ordenes-entrega/${qs ? `?${qs}` : ""}`);
  };

  export const getDeliveryOrder = (orderId) =>
    api.get(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/`);

  export const postDeliveryOrder = (payload) =>
    api.post("/api/ordenes-entrega/", payload);

  export const postDeliveryOrderDriveSync = (payload = {}) =>
    api.post("/api/ordenes-entrega/sincronizar-drive/", payload);

  export const patchDeliveryOrder = (orderId, payload = {}) =>
    api.patch(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/`, payload);

  export const postDeliveryOrderPrepared = (orderId, payload = {}) =>
    api.post(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/preparar/`, payload);

  export const postDeliveryOrderDelivered = (orderId, payload = {}) =>
    api.post(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/entregar/`, payload);

  export const postDeliveryOrderInvoiced = (orderId, payload = {}) =>
    api.post(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/facturar/`, payload);

  export const postDeliveryOrderNotBillable = (orderId, payload = {}) =>
    api.post(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/no-facturar/`, payload);

  export const postDeliveryOrderCancel = (orderId, payload = {}) =>
    api.post(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/cancelar/`, payload);

  export const patchDeliveryOrderRemitoLocation = (orderId, payload = {}) =>
    api.patch(`/api/ordenes-entrega/${encodeURIComponent(orderId)}/remito-ubicacion/`, payload);

  export const patchDeliveryOrderItemArticle = (orderId, itemId, payload = {}) =>
    api.patch(
      `/api/ordenes-entrega/${encodeURIComponent(orderId)}/items/${encodeURIComponent(itemId)}/articulo/`,
      payload
    );

  export const patchDeliveryOrderItemPartidas = (orderId, itemId, partidas = []) =>
    api.patch(
      `/api/ordenes-entrega/${encodeURIComponent(orderId)}/items/${encodeURIComponent(itemId)}/partidas/`,
      { partidas }
    );

  export const postDeliveryOrderBejermanRemito = (payload = {}) =>
    api.post("/api/ordenes-entrega/remito-bejerman/", payload);

  export const getDeliveryOrderRemitoHistory = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/ordenes-entrega/remito-bejerman/historial/${qs ? `?${qs}` : ""}`);
  };

  export const getDeliveryOrderBejermanArticles = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/ordenes-entrega/bejerman-articulos/${qs ? `?${qs}` : ""}`);
  };

  export const getDeliveryOrderBejermanDeposits = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/ordenes-entrega/bejerman-depositos/${qs ? `?${qs}` : ""}`);
  };

  export const getDeliveryOrderBejermanArticleStock = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/ordenes-entrega/bejerman-articulos-stock/${qs ? `?${qs}` : ""}`);
  };

  export const getDeliveryOrderRentalAvailableEquipment = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/ordenes-entrega/alquiler/equipos-disponibles/${qs ? `?${qs}` : ""}`);
  };

  export const getDeliveryOrderRemitoPdfBlob = (groupId) =>
    getBlob(`/api/ordenes-entrega/remito-bejerman/${encodeURIComponent(groupId)}/pdf/`);

  export const deliveryOrderInvoicePdfUrl = (orderId) =>
    `/api/ordenes-entrega/${encodeURIComponent(orderId)}/factura/pdf/`;

  export const getDeliveryOrderInvoicePdfBlob = (orderId) =>
    getBlob(deliveryOrderInvoicePdfUrl(orderId));

  export const getRouteSheet = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/hoja-ruta/${qs ? `?${qs}` : ""}`);
  };

  export const postRouteStop = (payload = {}) =>
    api.post("/api/hoja-ruta/", payload);

  export const patchRouteStop = (stopId, payload = {}) =>
    api.patch(`/api/hoja-ruta/${encodeURIComponent(stopId)}/`, payload);

  export const postRouteStopComplete = (stopId, payload = {}) =>
    api.post(`/api/hoja-ruta/${encodeURIComponent(stopId)}/completar/`, payload);

  export const postRouteStopPostpone = (stopId, payload = {}) =>
    api.post(`/api/hoja-ruta/${encodeURIComponent(stopId)}/posponer/`, payload);

  export const postRouteStopCancel = (stopId, payload = {}) =>
    api.post(`/api/hoja-ruta/${encodeURIComponent(stopId)}/cancelar/`, payload);

  export const postRouteStopsReorder = (payload = {}) =>
    api.post("/api/hoja-ruta/reordenar/", payload);

  export const getRouteLocations = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/hoja-ruta/lugares/${qs ? `?${qs}` : ""}`);
  };

  export const postRouteLocation = (payload = {}) =>
    api.post("/api/hoja-ruta/lugares/", payload);

  export const patchRouteLocation = (locationId, payload = {}) =>
    api.patch(`/api/hoja-ruta/lugares/${encodeURIComponent(locationId)}/`, payload);

  export const getRouteSuggestedDeliveryOrders = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/hoja-ruta/ordenes-sugeridas/${qs ? `?${qs}` : ""}`);
  };

  export const getBillingCustomers = () =>
    api.get("/api/cobranzas/facturacion/clientes/");

  export const getBillingDocuments = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/cobranzas/facturacion/documentos/${qs ? `?${qs}` : ""}`);
  };

  export const getBillingDocumentPdfBlob = (documentId, customerCode) => {
    const qs = buildQuery({ customerCode });
    return getBlob(`/api/cobranzas/facturacion/documentos/${encodeURIComponent(documentId)}/pdf/${qs ? `?${qs}` : ""}`);
  };

  export const getBillingRemitos = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/cobranzas/remitos/${qs ? `?${qs}` : ""}`);
  };

  export const billingRemitoPdfUrl = (documentId, params = {}) => {
    const qs = buildQuery(params);
    return `/api/cobranzas/remitos/${encodeURIComponent(documentId)}/pdf/${qs ? `?${qs}` : ""}`;
  };

  export const getBillingRemitoPdfBlob = (documentId, params = {}) => {
    return getBlob(billingRemitoPdfUrl(documentId, params));
  };

  export const getServiceOrdersToBill = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/cobranzas/os-a-facturar/${qs ? `?${qs}` : ""}`);
  };

  export const getServiceOrderBillingPdfBlob = (ingresoId) =>
    getBlob(`/api/cobranzas/os-a-facturar/${encodeURIComponent(ingresoId)}/pdf/`);

  export const postServiceOrderInvoice = (ingresoId, payload) =>
    api.post(`/api/cobranzas/os-a-facturar/${encodeURIComponent(ingresoId)}/factura/`, payload);

  export const getHistoricoIngresos = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/ingresos/${qs ? `?${qs}` : ""}`);
  };
  // Compatibilidad: antes se llamaba así
  export const getGeneralEquipos = getHistoricoIngresos;

  // Devices (tabla de equipos)
  export const getDevices = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/equipos/${qs ? `?${qs}` : ""}`);
  };
  export const postDeviceDirectCreate = (payload) =>
    api.post("/api/devices/alta-directa/", payload);
  export const postDevicePreventivoPlan = (deviceId, payload) =>
    api.post(`/api/equipos/${deviceId}/preventivo-plan/`, payload);
  export const patchDevicePreventivoPlan = (deviceId, payload) =>
    api.patch(`/api/equipos/${deviceId}/preventivo-plan/`, payload);
  export const postDevicePreventivoRevision = (deviceId, payload) =>
    api.post(`/api/equipos/${deviceId}/preventivo-revisiones/`, payload);
  export const getDevicePreventivoRepuestos = (deviceId) =>
    api.get(`/api/equipos/${deviceId}/preventivo-repuestos/`);
  export const postDevicePreventivoRepuesto = (deviceId, payload) =>
    api.post(`/api/equipos/${deviceId}/preventivo-repuestos/`, payload);
  export const patchDevicePreventivoRepuesto = (deviceId, itemId, payload) =>
    api.patch(`/api/equipos/${deviceId}/preventivo-repuestos/${itemId}/`, payload);
  export const deleteDevicePreventivoRepuesto = (deviceId, itemId) =>
    api.del(`/api/equipos/${deviceId}/preventivo-repuestos/${itemId}/`);
  export const postDeviceMgVenta = (deviceId, payload) =>
    api.post(`/api/equipos/${deviceId}/mg/venta/`, payload);
  export const postDeviceMgReactivar = (deviceId, payload = {}) =>
    api.post(`/api/equipos/${deviceId}/mg/reactivar/`, payload);
  export const getPreventivosAgenda = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/preventivos/agenda/${qs ? `?${qs}` : ""}`);
  };
  export const getPreventivosClientes = (params = {}) => {
    const qs = buildQuery(params);
    return api.get(`/api/preventivos/clientes/${qs ? `?${qs}` : ""}`);
  };
  export const postCustomerPreventivoPlan = (customerId, payload) =>
    api.post(`/api/clientes/${customerId}/preventivo-plan/`, payload);
  export const patchCustomerPreventivoPlan = (customerId, payload) =>
    api.patch(`/api/clientes/${customerId}/preventivo-plan/`, payload);
  export const getCustomerPreventivoRevisiones = (customerId) =>
    api.get(`/api/clientes/${customerId}/preventivo-revisiones/`);
  export const postCustomerPreventivoRevision = (customerId, payload = {}) =>
    api.post(`/api/clientes/${customerId}/preventivo-revisiones/`, payload);
  export const getPreventivoRevision = (revisionId) =>
    api.get(`/api/preventivos/revisiones/${revisionId}/`);
  export const postPreventivoRevisionItem = (revisionId, payload) =>
    api.post(`/api/preventivos/revisiones/${revisionId}/items/`, payload);
  export const patchPreventivoRevisionItem = (revisionId, itemId, payload) =>
    api.patch(`/api/preventivos/revisiones/${revisionId}/items/${itemId}/`, payload);
  export const postPreventivoRevisionCerrar = (revisionId, payload) =>
    api.post(`/api/preventivos/revisiones/${revisionId}/cerrar/`, payload);
  export const getDeviceEditable = (deviceId) =>
    api.get(`/api/devices/${deviceId}/identificadores/`);
  export const patchDeviceEditable = (deviceId, payload) =>
    api.patch(`/api/devices/${deviceId}/identificadores/`, payload);
  export const patchDeviceIdentificadores = patchDeviceEditable;
  export const postDevicesMerge = (payload) =>
    api.post("/api/devices/merge/", payload);
  // Check garantía de reparación por N/S
  export const checkGarantiaReparacion = (numero_serie, numero_interno) => {
    const params = new URLSearchParams();
    if (numero_serie) params.set("numero_serie", numero_serie);
    if (numero_interno) params.set("numero_interno", numero_interno);
    const qs = params.toString();
    return api.get(`/api/equipos/garantia-reparacion/${qs ? `?${qs}` : ""}`);
  };

  // Check garantía de fábrica (por N/S + opcional marca/modelo para aplicar excepciones)
  export const checkGarantiaFabrica = (numero_serie, marca, opts = null) => {
    const params = new URLSearchParams();
    if (numero_serie) params.set("numero_serie", numero_serie);
    if (marca) params.set("marca", marca);
    if (opts && opts.brand_id != null) params.set("brand_id", opts.brand_id);
    if (opts && opts.model_id != null) params.set("model_id", opts.model_id);
    if (opts && opts.company_key) params.set("company_key", opts.company_key);
    const qs = params.toString();
    return api.get(`/api/equipos/garantia-fabrica/${qs ? `?${qs}` : ""}`);
  };
  // Garantías: políticas (excepciones administrables)
  export const listWarrantyRules = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/garantias/politicas/${qs ? `?${qs}` : ""}`);
  };
  export const createWarrantyRule = (payload) =>
    api.post(`/api/garantias/politicas/`, payload);
  export const patchWarrantyRule = (id, payload) =>
    api.patch(`/api/garantias/politicas/${id}/`, payload);
  export const deleteWarrantyRule = (id) =>
    api.delete(`/api/garantias/politicas/${id}/`);
  export const getGeneralPorCliente = (customerId) =>
    api.get(`/api/clientes/${customerId}/general/`);

  export async function getIngreso(id, params = null) {
    const qs = params ? new URLSearchParams(params).toString() : "";
    return api.get(`/api/ingresos/${id}/${qs ? `?${qs}` : ""}`);
  }

  export async function getIngresoBejermanEstado(id) {
    return api.get(`/api/ingresos/${id}/bejerman-estado/`);
  }

  export async function patchIngreso(id, payload) {
    return api.patch(`/api/ingresos/${id}/`, payload);
  }

  export async function getIngresoTest(id) {
    return api.get(`/api/ingresos/${id}/test/`);
  }

  export async function patchIngresoTest(id, payload) {
    return api.patch(`/api/ingresos/${id}/test/`, payload);
  }

  export async function getIngresoTestPdfBlob(id) {
    return getBlob(`/api/ingresos/${id}/test/pdf/`);
  }

  export const patchIngresoTecnico = (ingresoId, tecnico_id) =>
    api.patch(`/api/ingresos/${ingresoId}/asignar-tecnico/`, { tecnico_id });

  // Solicitud de asignacin por tcnico
  export const postSolicitarAsignacion = (ingresoId) =>
    api.post(`/api/ingresos/${ingresoId}/solicitar-asignacion/`, {});
  // Solicitud de baja de equipo (sin permiso de baja directa)
  export const postSolicitarBaja = (ingresoId, payload) =>
    api.post(`/api/ingresos/${ingresoId}/solicitar-baja/`, payload || {});
  export const postRechazarSolicitudBaja = (ingresoId) =>
    api.post(`/api/ingresos/${ingresoId}/solicitar-baja/rechazar/`, {});

  export const patchModeloTecnico = (marcaId, modeloId, tecnico_id) =>
    api.patch(
      `/api/catalogos/marcas/${marcaId}/modelos/${modeloId}/tecnico/`,
      { tecnico_id }
    );

  // Variante simple por modelo (v1)
  export const patchModeloVariante = (marcaId, modeloId, variante) =>
    api.patch(
      `/api/catalogos/marcas/${marcaId}/modelos/${modeloId}/variante/`,
      { variante }
    );

  export const patchMarcaTecnico = (marcaId, tecnico_id) =>
    api.patch(`/api/catalogos/marcas/${marcaId}/tecnico/`, { tecnico_id });

  // Aplica el tcnico de la marca a TODOS los modelos (sobrescribe)
  export const postMarcaAplicarTecnico = (marcaId) =>
    api.post(`/api/catalogos/marcas/${marcaId}/tecnico/aplicar-a-modelos/`);

  /* =============== PRESUPUESTOS =============== */
  export const getQuote = (ingresoId, params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") qs.set(key, String(value));
    });
    return api.get(`/api/quotes/${ingresoId}/${qs.toString() ? `?${qs.toString()}` : ""}`);
  };

  export const postQuoteItem = (ingresoId, payload) =>
    api.post(`/api/quotes/${ingresoId}/items/`, payload);

  export const patchQuoteItem = (ingresoId, itemId, payload) =>
    api.patch(`/api/quotes/${ingresoId}/items/${itemId}/`, payload);

  export const deleteQuoteItem = (ingresoId, itemId) =>
    api.del(`/api/quotes/${ingresoId}/items/${itemId}/`);

  export const patchQuoteResumen = (ingresoId, payload /* {mano_obra} */) =>
    api.patch(`/api/quotes/${ingresoId}/resumen/`, payload);

  export const postQuoteEmitir = (ingresoId, payload /* {autorizado_por, forma_pago, plazo_entrega_txt, garantia_txt, mant_oferta_txt} */) =>
    api.post(`/api/quotes/${ingresoId}/emitir/`, payload);

  export const postQuoteAprobar = (ingresoId) =>
    api.post(`/api/quotes/${ingresoId}/aprobar/`);

  export const postQuoteRechazar = (ingresoId, payload = {}) =>
    api.post(`/api/quotes/${ingresoId}/rechazar/`, payload);

  export const postQuoteNuevaVersion = (ingresoId) =>
    api.post(`/api/quotes/${ingresoId}/versiones/`);

  export const postQuoteNoAplica = (ingresoId) =>
    api.post(`/api/quotes/${ingresoId}/no-aplica/`);

  export const postQuoteQuitarNoAplica = (ingresoId) =>
    api.post(`/api/quotes/${ingresoId}/no-aplica/quitar/`);

  // === GET binario (Blob) con auth y cookies ===
  export async function getBlob(path, opts = {}) {
    const res = await fetchWithAuth(path, {
      method: "GET",
      ...opts,
    });

    if (!res.ok) {
      const contentType = res.headers.get("content-type") || "";
      const text = await res.text().catch(() => "");
      let detail = text;
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
        detail = data?.detail || JSON.stringify(data);
      } catch (_) {
        if (looksLikeHtmlResponse(text, contentType)) {
          detail = htmlErrorMessage(res.status, `No se pudo completar la descarga (HTTP ${res.status}).`);
        }
      }
      const err = new Error(detail || `HTTP ${res.status}`);
      err.status = res.status;
      err.data = data;
      err.response = res;
      throw err;
    }
    return await res.blob();
  }

  export const postQuoteAnular = (ingresoId) =>
    api.post(`/api/quotes/${ingresoId}/anular/`);

  // Cerrar reparacin (setea la resolucin)
  export async function postCerrarReparacion(id, body) {
    // body = { resolucion: "reparado" | "no_reparado" | "no_se_encontro_falla" | "presupuesto_rechazado" | "cambio", serial_cambio?: string }
    return api.post(`/api/ingresos/${id}/cerrar/`, body);
  }

  // Marcar controlado sin defecto (equipos propios revisados sin falla)
  export async function postMarcarControladoSinDefecto(id) {
    return api.post(`/api/ingresos/${id}/controlado-sin-defecto/`);
  }

  export async function postMarcarNoSeRepara(id) {
    return api.post(`/api/ingresos/${id}/no-se-repara/`);
  }

  export async function postQuitarEstadoFinalReparacion(id) {
    return api.post(`/api/ingresos/${id}/quitar-estado-final-reparacion/`);
  }

  export async function postMarcarParaReparar(id) {
    return api.post(`/api/ingresos/${id}/reparar/`);
  }

  export async function postQuitarReparar(id) {
    return api.post(`/api/ingresos/${id}/quitar-reparar/`);
  }

  export async function postHabilitarReparacionCotizacion(id) {
    return api.post(`/api/ingresos/${id}/habilitar-reparacion/`);
  }

  export async function postMarcarReparado(id) {
    return api.post(`/api/ingresos/${id}/reparado/`);
  }

  // Historial de cambios por ingreso
  export const getIngresoHistorial = (ingresoId) =>
    api.get(`/api/ingresos/${ingresoId}/historial/`);


  /* =============== Mtricas ================= */
  export const getMetricasResumen = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/metricas/resumen/${qs ? `?${qs}` : ""}`);
  };
  export const getMetricasSeries = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/metricas/series/${qs ? `?${qs}` : ""}`);
  };
  export const getMetricasFinanzas = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/metricas/finanzas/${qs ? `?${qs}` : ""}`);
  };
  export const getMetricasFinanzasLiberados = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/metricas/finanzas/liberados/${qs ? `?${qs}` : ""}`);
  };
  export const getMetricasActividadTecnicos = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/metricas/actividad-tecnicos/${qs ? `?${qs}` : ""}`);
  };
  export const getMetricasCalibracion = (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api.get(`/api/metricas/calibracion/${qs ? `?${qs}` : ""}`);
  };
  export const getMetricasConfig = () => api.get(`/api/metricas/config/`);
  export const getFeriados = () => api.get(`/api/metricas/feriados/`);
  export const postFeriado = (fecha, nombre) => api.post(`/api/metricas/feriados/`, { fecha, nombre });
  export const deleteFeriado = (fecha) => api.del(`/api/metricas/feriados/?fecha=${encodeURIComponent(fecha||"")}`);
