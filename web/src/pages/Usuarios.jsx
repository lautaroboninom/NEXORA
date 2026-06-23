import { useEffect, useMemo, useRef, useState } from "react";
import {
  deleteUsuario,
  getPermisosCatalogo,
  getRoles,
  getUsuarioNotificaciones,
  getUsuarioPermisos,
  getUsuarios,
  patchUsuarioActivo,
  patchUsuarioReset,
  patchUsuarioRolePerm,
  postUsuario,
  postUsuarioPermisosReset,
  putUsuarioNotificaciones,
  putUsuarioPermisos,
} from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { can, PERMISSION_CODES } from "@/lib/permissions";

const Input = (p) => <input {...p} className="border rounded p-2 w-full" />;
const Select = (p) => <select {...p} className="border rounded p-2 w-full" />;
const Btn = ({ variant = "solid", className = "", ...props }) => {
  const base = "px-3 py-2 rounded text-sm";
  const styles =
    variant === "solid"
      ? "bg-blue-600 text-white hover:bg-blue-700"
      : variant === "danger"
        ? "bg-red-600 text-white hover:bg-red-700"
        : "border hover:bg-gray-50";
  return <button className={`${base} ${styles} ${className}`} {...props} />;
};

const EFFECT_LABELS = {
  inherit: "Heredar",
  allow: "Permitir",
  deny: "Bloquear",
};

const NOTIFICATION_LABELS = {
  inherit: "Heredar",
  on: "Activada",
  off: "Desactivada",
};

function notificationOverrideToUi(value) {
  if (value === true) return "on";
  if (value === false) return "off";
  return "inherit";
}

function notificationUiToApi(value) {
  if (value === "on") return true;
  if (value === "off") return false;
  return null;
}

export default function Usuarios() {
  const { user: yo, refreshSession } = useAuth();
  const isJefe = yo?.rol === "jefe";
  const canManageUsers = can(yo, PERMISSION_CODES.ACTION_USERS_MANAGE);
  const canManagePermissions =
    isJefe && can(yo, PERMISSION_CODES.ACTION_USERS_MANAGE_PERMISSIONS);
  const canChangeRole = isJefe && canManageUsers;

  const [rows, setRows] = useState([]);
  const [roles, setRoles] = useState([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [nuevo, setNuevo] = useState({ nombre: "", email: "", rol: "tecnico" });

  const [menuOpenId, setMenuOpenId] = useState(null);
  const [menuPos, setMenuPos] = useState(null);
  const menuRef = useRef(null);
  const menuButtonRefs = useRef({});

  const [permOpen, setPermOpen] = useState(false);
  const [permCatalog, setPermCatalog] = useState([]);
  const [permTarget, setPermTarget] = useState(null);
  const [permData, setPermData] = useState(null);
  const [permOverrides, setPermOverrides] = useState({});
  const [permSearch, setPermSearch] = useState("");
  const [permLoading, setPermLoading] = useState(false);
  const [permSaving, setPermSaving] = useState(false);
  const [permResetting, setPermResetting] = useState(false);
  const [permErr, setPermErr] = useState("");

  const [notifOpen, setNotifOpen] = useState(false);
  const [notifTarget, setNotifTarget] = useState(null);
  const [notifData, setNotifData] = useState(null);
  const [notifOverrides, setNotifOverrides] = useState({});
  const [notifSearch, setNotifSearch] = useState("");
  const [notifLoading, setNotifLoading] = useState(false);
  const [notifSaving, setNotifSaving] = useState(false);
  const [notifErr, setNotifErr] = useState("");

  useEffect(() => {
    if (!canManageUsers) {
      setRoles([]);
      return;
    }
    let alive = true;
    (async () => {
      try {
        const os = await getRoles();
        if (alive) setRoles(Array.isArray(os) ? os : []);
      } catch (_) {}
    })();
    return () => {
      alive = false;
    };
  }, [canManageUsers]);

  useEffect(() => {
    const onClick = (event) => {
      if (!menuOpenId) return;
      const menuEl = menuRef.current;
      const btnEl = menuButtonRefs.current?.[menuOpenId];
      const target = event.target;
      if (menuEl && menuEl.contains(target)) return;
      if (btnEl && btnEl.contains(target)) return;
      setMenuOpenId(null);
      setMenuPos(null);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [menuOpenId]);

  useEffect(() => {
    if (!menuOpenId) return;
    const recalc = () => {
      const btnEl = menuButtonRefs.current?.[menuOpenId];
      if (!btnEl) return;
      const rect = btnEl.getBoundingClientRect();
      const menuWidth = 176; // w-44
      const menuHeightEstimate = 176;
      let left = rect.right - menuWidth;
      let top = rect.bottom + 6;
      left = Math.max(8, Math.min(left, window.innerWidth - menuWidth - 8));
      if (top + menuHeightEstimate > window.innerHeight - 8) {
        top = Math.max(8, rect.top - menuHeightEstimate - 6);
      }
      setMenuPos({ top, left });
    };
    recalc();
    window.addEventListener("resize", recalc);
    window.addEventListener("scroll", recalc, true);
    return () => {
      window.removeEventListener("resize", recalc);
      window.removeEventListener("scroll", recalc, true);
    };
  }, [menuOpenId, canManagePermissions, canManageUsers]);

  useEffect(() => {
    if (!menuOpenId) return;
    if (!rows.some((u) => Number(u.id) === Number(menuOpenId))) {
      setMenuOpenId(null);
      setMenuPos(null);
    }
  }, [rows, menuOpenId]);

  const roleLabel = useMemo(
    () => (val) => roles.find((o) => o.value === val)?.label || val,
    [roles],
  );
  const menuUser = useMemo(
    () => rows.find((u) => Number(u.id) === Number(menuOpenId)) || null,
    [rows, menuOpenId],
  );

  function toggleActionsMenu(uid) {
    const id = Number(uid);
    if (Number(menuOpenId) === id) {
      setMenuOpenId(null);
      setMenuPos(null);
      return;
    }
    setMenuOpenId(id);
  }

  async function load() {
    setErr("");
    try {
      setRows(await getUsuarios());
    } catch (e) {
      setErr(normalizeErr(e));
      setRows([]);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const onNew = (k) => (e) => setNuevo((prev) => ({ ...prev, [k]: e.target.value }));

  async function crear(e) {
    e.preventDefault();
    if (!canManageUsers) return;
    try {
      await postUsuario(nuevo);
      setNuevo({ nombre: "", email: "", rol: "tecnico" });
      setMsg("Usuario creado o actualizado. Mail de bienvenida enviado.");
      await load();
    } catch (e2) {
      setErr(normalizeErr(e2));
    }
  }

  async function toggleActivo(u) {
    if (!canManageUsers) return;
    try {
      await patchUsuarioActivo(u.id, !u.activo);
      setMsg(`Usuario ${!u.activo ? "activado" : "desactivado"}`);
      await load();
    } catch (e) {
      setErr(normalizeErr(e));
    }
  }

  async function cambiarRol(u, rol) {
    if (!canChangeRole) return;
    try {
      await patchUsuarioRolePerm(u.id, { rol });
      setMsg("Rol actualizado");
      await load();
    } catch (e) {
      setErr(normalizeErr(e));
    }
  }

  async function enviarLinkPw(u) {
    if (!canManageUsers) return;
    try {
      await patchUsuarioReset(u.id);
      setMsg("Enlace de restablecimiento enviado");
    } catch (e) {
      setErr(normalizeErr(e));
    }
  }

  async function borrar(u) {
    if (!canManageUsers) return;
    if (!window.confirm(`Eliminar usuario ${u.email}?`)) return;
    try {
      await deleteUsuario(u.id);
      setMsg("Usuario eliminado");
      await load();
    } catch (e) {
      setErr(normalizeErr(e));
    }
  }

  async function ensurePermCatalog() {
    if (permCatalog.length) return permCatalog;
    const data = await getPermisosCatalogo();
    const list = Array.isArray(data?.permissions) ? data.permissions : [];
    setPermCatalog(list);
    return list;
  }

  async function openPermEditor(u) {
    if (!canManagePermissions) return;
    setMenuOpenId(null);
    setMenuPos(null);
    setPermOpen(true);
    setPermLoading(true);
    setPermErr("");
    setPermTarget(u);
    setPermData(null);
    setPermOverrides({});
    setPermSearch("");
    try {
      await ensurePermCatalog();
      const data = await getUsuarioPermisos(u.id);
      setPermData(data || null);
      setPermOverrides({ ...(data?.overrides || {}) });
    } catch (e) {
      setPermErr(normalizeErr(e));
    } finally {
      setPermLoading(false);
    }
  }

  function closePermEditor() {
    setPermOpen(false);
    setPermTarget(null);
    setPermData(null);
    setPermOverrides({});
    setPermSearch("");
    setPermErr("");
    setPermLoading(false);
    setPermSaving(false);
    setPermResetting(false);
  }

  const modalEditable = Boolean(permData?.editable && canManagePermissions);

  const groupedPermissions = useMemo(() => {
    const list = Array.isArray(permCatalog) ? permCatalog : [];
    const needle = (permSearch || "").trim().toLowerCase();
    const groups = new Map();
    list.forEach((item) => {
      const text = `${item?.label || ""} ${item?.code || ""} ${item?.group || ""} ${item?.type || ""}`.toLowerCase();
      if (needle && !text.includes(needle)) return;
      const groupName = item?.group || "Otros";
      if (!groups.has(groupName)) groups.set(groupName, []);
      groups.get(groupName).push(item);
    });
    return Array.from(groups.entries()).sort((a, b) =>
      a[0].localeCompare(b[0], "es", { sensitivity: "base" }),
    );
  }, [permCatalog, permSearch]);

  const permDirty = useMemo(() => {
    if (!permData) return false;
    const original = permData.overrides || {};
    const allCodes = (permCatalog || []).map((p) => p.code);
    return allCodes.some((code) => {
      const next = permOverrides?.[code] || "inherit";
      const prev = original?.[code] || "inherit";
      return next !== prev;
    });
  }, [permCatalog, permData, permOverrides]);

  function setOverride(code, effect) {
    setPermOverrides((prev) => ({ ...prev, [code]: effect }));
  }

  async function savePermisos() {
    if (!permTarget || !modalEditable) return;
    try {
      setPermSaving(true);
      setPermErr("");
      const data = await putUsuarioPermisos(permTarget.id, { overrides: permOverrides });
      setPermData(data || null);
      setPermOverrides({ ...(data?.overrides || {}) });
      setMsg("Permisos actualizados");
      if (Number(permTarget.id) === Number(yo?.id)) {
        await refreshSession?.();
      }
      await load();
    } catch (e) {
      setPermErr(normalizeErr(e));
    } finally {
      setPermSaving(false);
    }
  }

  async function resetPermisos() {
    if (!permTarget || !modalEditable) return;
    if (!window.confirm("¿Restablecer todos los permisos a herencia del rol base?")) return;
    try {
      setPermResetting(true);
      setPermErr("");
      const data = await postUsuarioPermisosReset(permTarget.id);
      setPermData(data || null);
      setPermOverrides({ ...(data?.overrides || {}) });
      setMsg("Permisos restablecidos");
      if (Number(permTarget.id) === Number(yo?.id)) {
        await refreshSession?.();
      }
      await load();
    } catch (e) {
      setPermErr(normalizeErr(e));
    } finally {
      setPermResetting(false);
    }
  }

  async function openNotifEditor(u) {
    if (!canManagePermissions) return;
    setMenuOpenId(null);
    setMenuPos(null);
    setNotifOpen(true);
    setNotifLoading(true);
    setNotifErr("");
    setNotifTarget(u);
    setNotifData(null);
    setNotifOverrides({});
    setNotifSearch("");
    try {
      const data = await getUsuarioNotificaciones(u.id);
      const overrides = {};
      (data?.items || []).forEach((item) => {
        overrides[item.key] = notificationOverrideToUi(item.override_enabled);
      });
      setNotifData(data || null);
      setNotifOverrides(overrides);
    } catch (e) {
      setNotifErr(normalizeErr(e));
    } finally {
      setNotifLoading(false);
    }
  }

  function closeNotifEditor() {
    setNotifOpen(false);
    setNotifTarget(null);
    setNotifData(null);
    setNotifOverrides({});
    setNotifSearch("");
    setNotifErr("");
    setNotifLoading(false);
    setNotifSaving(false);
  }

  const notifEditable = Boolean(notifData && canManagePermissions);

  const groupedNotifications = useMemo(() => {
    const list = Array.isArray(notifData?.items) ? notifData.items : [];
    const needle = (notifSearch || "").trim().toLowerCase();
    const groups = new Map();
    list.forEach((item) => {
      const text = `${item?.label || ""} ${item?.key || ""} ${item?.group || ""} ${item?.description || ""}`.toLowerCase();
      if (needle && !text.includes(needle)) return;
      const groupName = item?.group || "Otros";
      if (!groups.has(groupName)) groups.set(groupName, []);
      groups.get(groupName).push(item);
    });
    return Array.from(groups.entries()).sort((a, b) =>
      a[0].localeCompare(b[0], "es", { sensitivity: "base" }),
    );
  }, [notifData, notifSearch]);

  const notifDirty = useMemo(() => {
    const list = Array.isArray(notifData?.items) ? notifData.items : [];
    return list.some((item) => {
      const next = notifOverrides?.[item.key] || "inherit";
      const prev = notificationOverrideToUi(item.override_enabled);
      return next !== prev;
    });
  }, [notifData, notifOverrides]);

  function setNotifOverride(key, value) {
    setNotifOverrides((prev) => ({ ...prev, [key]: value }));
  }

  function notificationEffective(item) {
    const override = notifOverrides?.[item.key] || "inherit";
    if (override === "on") return true;
    if (override === "off") return false;
    return !!item?.default_enabled;
  }

  async function saveNotificaciones() {
    if (!notifTarget || !notifEditable) return;
    try {
      setNotifSaving(true);
      setNotifErr("");
      const preferences = {};
      (notifData?.items || []).forEach((item) => {
        preferences[item.key] = notificationUiToApi(notifOverrides?.[item.key] || "inherit");
      });
      const data = await putUsuarioNotificaciones(notifTarget.id, { preferences });
      const overrides = {};
      (data?.items || []).forEach((item) => {
        overrides[item.key] = notificationOverrideToUi(item.override_enabled);
      });
      setNotifData(data || null);
      setNotifOverrides(overrides);
      setMsg("Notificaciones actualizadas");
    } catch (e) {
      setNotifErr(normalizeErr(e));
    } finally {
      setNotifSaving(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">Usuarios</h1>
        <span className="text-gray-500">({roleLabel(yo?.rol)})</span>
      </div>

      {err && <div className="bg-red-100 text-red-700 p-2 rounded">{err}</div>}
      {msg && <div className="bg-green-100 text-green-700 p-2 rounded">{msg}</div>}

      {canManageUsers && (
        <form onSubmit={crear} className="border rounded p-4 grid md:grid-cols-5 gap-3">
          <div className="md:col-span-2">
            <label className="text-sm">Nombre</label>
            <Input value={nuevo.nombre} onChange={onNew("nombre")} required />
          </div>
          <div className="md:col-span-2">
            <label className="text-sm">Email</label>
            <Input type="email" value={nuevo.email} onChange={onNew("email")} required />
          </div>
          <div>
            <label className="text-sm">Rol</label>
            <Select value={nuevo.rol} onChange={onNew("rol")}>
              {roles.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="md:col-span-1 self-end">
            <Btn type="submit" className="w-full">
              Guardar
            </Btn>
          </div>
        </form>
      )}

      <div className="overflow-auto border rounded">
        <table className="min-w-full">
          <thead>
            <tr className="bg-gray-50 text-left">
              <th className="p-2">ID</th>
              <th className="p-2">Nombre</th>
              <th className="p-2">Email</th>
              <th className="p-2">Rol</th>
              <th className="p-2">Bejerman</th>
              <th className="p-2 w-[92px] whitespace-nowrap" title="Permisos personalizados">
                Perm.
              </th>
              <th className="p-2">Activo</th>
              <th className="p-2 w-[52px] text-center" title="Acciones">
                <span className="sr-only">Acciones</span>
                <span aria-hidden="true">⋮</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((u) => {
              const canOpenActions = canManageUsers || canManagePermissions;
              return (
                <tr key={u.id} className="border-t">
                  <td className="p-2">{u.id}</td>
                  <td className="p-2">{u.nombre}</td>
                  <td className="p-2">{u.email}</td>
                  <td className="p-2">
                    {canChangeRole ? (
                      <Select value={u.rol} onChange={(e) => cambiarRol(u, e.target.value)}>
                        {roles.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <span className="px-2 py-1 rounded bg-gray-100">{roleLabel(u.rol)}</span>
                    )}
                  </td>
                  <td className="p-2">
                    {u.bejerman_credentials_valid ? (
                      <span className="inline-flex rounded bg-emerald-50 px-2 py-1 text-xs font-medium text-emerald-700">
                        Validado {u.bejerman_credentials_username || ""}
                      </span>
                    ) : u.bejerman_credentials_configured ? (
                      <span
                        className="inline-flex rounded bg-amber-50 px-2 py-1 text-xs font-medium text-amber-700"
                        title={u.bejerman_credentials_last_error || ""}
                      >
                        Revisar
                      </span>
                    ) : (
                      <span className="inline-flex rounded bg-gray-100 px-2 py-1 text-xs text-gray-600">
                        Pendiente
                      </span>
                    )}
                  </td>
                  <td className="p-2 w-[92px]">
                    <span className="inline-flex min-w-7 justify-center rounded bg-gray-100 px-2 py-1 text-sm">
                      {Number(u?.permisos_personalizados || 0)}
                    </span>
                  </td>
                  <td className="p-2">
                    {canManageUsers ? (
                      <label className="inline-flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={!!u.activo}
                          onChange={() => toggleActivo(u)}
                        />
                        <span className="text-sm">{u.activo ? "Activo" : "Inactivo"}</span>
                      </label>
                    ) : (
                      <span className="text-sm">{u.activo ? "Activo" : "Inactivo"}</span>
                    )}
                  </td>
                  <td className="p-2 w-[52px] text-right">
                    {canOpenActions ? (
                      <div className="inline-flex justify-end">
                        <button
                          type="button"
                          className="h-8 w-8 rounded border hover:bg-gray-50"
                          aria-label="Abrir acciones"
                          ref={(el) => {
                            if (el) menuButtonRefs.current[u.id] = el;
                            else delete menuButtonRefs.current[u.id];
                          }}
                          onClick={() => toggleActionsMenu(u.id)}
                        >
                          {"\u22EE"}
                        </button>
                      </div>
                    ) : (
                      <span className="text-gray-400">-</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {!rows.length && (
              <tr>
                <td colSpan="8" className="p-4 text-center text-gray-500">
                  Sin usuarios
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {menuOpenId && menuUser && menuPos && (
        <div
          ref={menuRef}
          className="fixed w-44 rounded border bg-white shadow-lg z-40"
          style={{ top: `${menuPos.top}px`, left: `${menuPos.left}px` }}
        >
          <div className="py-1 text-left">
            {canManageUsers && (
              <button
                type="button"
                className="w-full px-3 py-2 text-sm hover:bg-gray-50 text-left"
                onClick={() => {
                  setMenuOpenId(null);
                  setMenuPos(null);
                  enviarLinkPw(menuUser);
                }}
              >
                Enviar link
              </button>
            )}
            {canManagePermissions && (
              <button
                type="button"
                className="w-full px-3 py-2 text-sm hover:bg-gray-50 text-left"
                onClick={() => openPermEditor(menuUser)}
              >
                Editar permisos
              </button>
            )}
            {canManagePermissions && (
              <button
                type="button"
                className="w-full px-3 py-2 text-sm hover:bg-gray-50 text-left"
                onClick={() => openNotifEditor(menuUser)}
              >
                Notificaciones
              </button>
            )}
            {canManageUsers && (
              <button
                type="button"
                className="w-full px-3 py-2 text-sm text-red-700 hover:bg-red-50 text-left"
                onClick={() => {
                  setMenuOpenId(null);
                  setMenuPos(null);
                  borrar(menuUser);
                }}
              >
                Eliminar
              </button>
            )}
          </div>
        </div>
      )}

      {permOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={closePermEditor}
        >
          <div
            className="bg-white rounded shadow-xl w-full max-w-6xl max-h-[88vh] overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="border-b px-4 py-3 flex items-center justify-between gap-3">
              <div>
                <div className="text-lg font-semibold">Editar permisos</div>
                <div className="text-sm text-gray-600">
                  {permTarget ? `${permTarget.nombre} (${permTarget.email})` : "-"}
                </div>
              </div>
              <button
                type="button"
                className="text-sm text-gray-500 hover:text-gray-900"
                onClick={closePermEditor}
              >
                Cerrar
              </button>
            </div>

            <div className="p-4 space-y-3 overflow-auto max-h-[calc(88vh-72px)]">
              {permErr && (
                <div className="bg-red-100 text-red-700 border border-red-300 rounded p-2">
                  {permErr}
                </div>
              )}
              {permLoading ? (
                <div className="text-sm text-gray-500">Cargando permisos...</div>
              ) : (
                <>
                  {!modalEditable && permData && (
                    <div className="bg-amber-100 text-amber-900 border border-amber-300 rounded p-2">
                      Este usuario no es editable por permisos granulares (rol jefe).
                    </div>
                  )}

                  <div className="flex flex-col md:flex-row md:items-center gap-3">
                    <input
                      type="text"
                      className="border rounded p-2 w-full md:max-w-md"
                      placeholder="Buscar permiso por nombre, código o módulo"
                      value={permSearch}
                      onChange={(e) => setPermSearch(e.target.value)}
                    />
                    <div className="text-xs text-gray-500">
                      {groupedPermissions.reduce((acc, [, items]) => acc + items.length, 0)} permiso(s)
                    </div>
                    <div className="md:ml-auto flex gap-2">
                      <Btn
                        variant="ghost"
                        type="button"
                        onClick={resetPermisos}
                        disabled={!modalEditable || permResetting}
                      >
                        {permResetting ? "Restableciendo..." : "Restablecer"}
                      </Btn>
                      <Btn
                        type="button"
                        onClick={savePermisos}
                        disabled={!modalEditable || permSaving || !permDirty}
                      >
                        {permSaving ? "Guardando..." : "Guardar"}
                      </Btn>
                    </div>
                  </div>

                  <div className="space-y-4">
                    {groupedPermissions.map(([groupName, items]) => (
                      <div key={groupName} className="border rounded">
                        <div className="px-3 py-2 bg-gray-50 border-b text-sm font-medium">
                          {groupName}
                        </div>
                        <div className="divide-y">
                          {items.map((item) => {
                            const code = item.code;
                            const override = permOverrides?.[code] || "inherit";
                            const effective = !!permData?.effective_permissions?.[code];
                            return (
                              <div key={code} className="px-3 py-2 flex flex-col md:flex-row md:items-center gap-2">
                                <div className="flex-1 min-w-0">
                                  <div className="text-sm font-medium">{item.label || code}</div>
                                  <div className="text-xs text-gray-500">{code}</div>
                                </div>
                                <div className="text-xs">
                                  <span
                                    className={`inline-flex px-2 py-1 rounded border ${effective ? "bg-green-50 text-green-700 border-green-200" : "bg-red-50 text-red-700 border-red-200"}`}
                                  >
                                    {effective ? "Efectivo: permitido" : "Efectivo: bloqueado"}
                                  </span>
                                </div>
                                <div className="w-full md:w-48">
                                  <select
                                    className="border rounded p-2 w-full text-sm"
                                    value={override}
                                    disabled={!modalEditable}
                                    onChange={(e) => setOverride(code, e.target.value)}
                                  >
                                    <option value="inherit">{EFFECT_LABELS.inherit}</option>
                                    <option value="allow">{EFFECT_LABELS.allow}</option>
                                    <option value="deny">{EFFECT_LABELS.deny}</option>
                                  </select>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                    {!groupedPermissions.length && (
                      <div className="text-sm text-gray-500">No hay permisos para el filtro actual.</div>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {notifOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={closeNotifEditor}
        >
          <div
            className="bg-white rounded shadow-xl w-full max-w-5xl max-h-[88vh] overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="border-b px-4 py-3 flex items-center justify-between gap-3">
              <div>
                <div className="text-lg font-semibold">Notificaciones</div>
                <div className="text-sm text-gray-600">
                  {notifTarget ? `${notifTarget.nombre} (${notifTarget.email})` : "-"}
                </div>
              </div>
              <button
                type="button"
                className="text-sm text-gray-500 hover:text-gray-900"
                onClick={closeNotifEditor}
              >
                Cerrar
              </button>
            </div>

            <div className="p-4 space-y-3 overflow-auto max-h-[calc(88vh-72px)]">
              {notifErr && (
                <div className="bg-red-100 text-red-700 border border-red-300 rounded p-2">
                  {notifErr}
                </div>
              )}

              {notifLoading ? (
                <div className="text-sm text-gray-500">Cargando notificaciones...</div>
              ) : (
                <>
                  <div className="flex flex-col md:flex-row md:items-center gap-3">
                    <input
                      type="text"
                      className="border rounded p-2 w-full md:max-w-md"
                      placeholder="Buscar notificación por nombre, código o módulo"
                      value={notifSearch}
                      onChange={(e) => setNotifSearch(e.target.value)}
                    />
                    <div className="text-xs text-gray-500">
                      {groupedNotifications.reduce((acc, [, items]) => acc + items.length, 0)} tipo(s)
                    </div>
                    <div className="md:ml-auto flex gap-2">
                      <Btn
                        type="button"
                        onClick={saveNotificaciones}
                        disabled={!notifEditable || notifSaving || !notifDirty}
                      >
                        {notifSaving ? "Guardando..." : "Guardar"}
                      </Btn>
                    </div>
                  </div>

                  <div className="space-y-4">
                    {groupedNotifications.map(([groupName, items]) => (
                      <div key={groupName} className="border rounded">
                        <div className="px-3 py-2 bg-gray-50 border-b text-sm font-medium">
                          {groupName}
                        </div>
                        <div className="divide-y">
                          {items.map((item) => {
                            const override = notifOverrides?.[item.key] || "inherit";
                            const effective = notificationEffective(item);
                            return (
                              <div
                                key={item.key}
                                className="px-3 py-2 flex flex-col md:flex-row md:items-center gap-2"
                              >
                                <div className="flex-1 min-w-0">
                                  <div className="text-sm font-medium">
                                    {item.label || item.key}
                                  </div>
                                  <div className="text-xs text-gray-500">
                                    {item.description || item.key}
                                  </div>
                                  <div className="mt-1 text-[11px] text-gray-400">
                                    Solo aplica cuando el usuario es destinatario. Predeterminado del rol: {item.default_enabled ? "activada" : "desactivada"}
                                  </div>
                                </div>
                                <div className="text-xs">
                                  <span
                                    className={`inline-flex px-2 py-1 rounded border ${effective ? "bg-green-50 text-green-700 border-green-200" : "bg-red-50 text-red-700 border-red-200"}`}
                                  >
                                    {effective ? "Como destinatario: activada" : "Como destinatario: desactivada"}
                                  </span>
                                </div>
                                <div className="w-full md:w-48">
                                  <select
                                    className="border rounded p-2 w-full text-sm"
                                    value={override}
                                    disabled={!notifEditable}
                                    onChange={(e) => setNotifOverride(item.key, e.target.value)}
                                  >
                                    <option value="inherit">{NOTIFICATION_LABELS.inherit}</option>
                                    <option value="on">{NOTIFICATION_LABELS.on}</option>
                                    <option value="off">{NOTIFICATION_LABELS.off}</option>
                                  </select>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                    {!groupedNotifications.length && (
                      <div className="text-sm text-gray-500">
                        No hay notificaciones para el filtro actual.
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function normalizeErr(e) {
  try {
    const t = e?.message || "";
    if (t.startsWith("{")) {
      const parsed = JSON.parse(t);
      return parsed?.detail || t;
    }
    return t || "Error";
  } catch (_) {
    return e?.message || "Error";
  }
}







