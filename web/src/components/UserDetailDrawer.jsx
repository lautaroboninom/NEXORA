import { useEffect, useMemo, useState } from "react";

import {
  deleteUsuarioBejermanCredentials,
  getUsuarioBejermanWorkstations,
  patchUsuario,
  putUsuarioBejermanCredentials,
} from "@/lib/api";
import { describeUserAgent, formatUserDate } from "@/lib/usuarios";

const inputClass =
  "w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-100";

function errorMessage(error) {
  return (
    error?.data?.detail ||
    error?.message ||
    "No se pudo completar la operación."
  );
}

function invitationLabel(user) {
  if (user?.invitation_status === "pending") return "Enlace pendiente";
  if (user?.invitation_status === "expired") return "Último enlace vencido";
  if (user?.invitation_status === "used") return "Último enlace utilizado";
  return "Sin enlaces emitidos";
}

function Definition({ label, children }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">
        {label}
      </dt>
      <dd className="mt-1 text-sm text-gray-900">{children || "—"}</dd>
    </div>
  );
}

function Section({ title, description, children, action }) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold text-gray-900">{title}</h3>
          {description && (
            <p className="mt-1 text-xs leading-5 text-gray-500">{description}</p>
          )}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export default function UserDetailDrawer({
  user,
  roles,
  isJefe,
  canManageUsers,
  canManagePermissions,
  onClose,
  onRefresh,
  onOpenPermissions,
  onOpenNotifications,
  onResetPassword,
  onToggleActive,
  onDelete,
}) {
  const [profile, setProfile] = useState({
    nombre: "",
    email: "",
    rol: "tecnico",
    bejerman_workstation: "",
    bejerman_seller_code: "",
  });
  const [credentials, setCredentials] = useState({
    username: "",
    password: "",
    workstation: "",
  });
  const [workstations, setWorkstations] = useState([]);
  const [discovering, setDiscovering] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);
  const [savingCredentials, setSavingCredentials] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!user) return;
    setProfile({
      nombre: user.nombre || "",
      email: user.email || "",
      rol: user.rol || "tecnico",
      bejerman_workstation: user.bejerman_workstation || "",
      bejerman_seller_code: user.bejerman_seller_code || "",
    });
    setCredentials({
      username: "",
      password: "",
      workstation: user.bejerman_workstation || "",
    });
    setWorkstations([]);
    setError("");
    setMessage("");
  }, [user?.id]);

  const userAgentLabel = useMemo(
    () => describeUserAgent(user?.last_login_user_agent),
    [user?.last_login_user_agent],
  );

  if (!user) return null;

  async function refresh(nextUser) {
    await onRefresh?.(nextUser || null);
  }

  async function saveProfile(event) {
    event.preventDefault();
    setSavingProfile(true);
    setError("");
    setMessage("");
    try {
      const next = await patchUsuario(user.id, {
        nombre: profile.nombre,
        email: profile.email,
        rol: profile.rol,
      });
      setMessage("Datos del usuario actualizados.");
      await refresh(next);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSavingProfile(false);
    }
  }

  async function saveBejermanPreferences(event) {
    event.preventDefault();
    setSavingProfile(true);
    setError("");
    setMessage("");
    try {
      const next = await patchUsuario(user.id, {
        bejerman_workstation: profile.bejerman_workstation,
        bejerman_seller_code:
          profile.rol === "ventas" ? profile.bejerman_seller_code : "",
      });
      setMessage("Configuración Bejerman actualizada.");
      await refresh(next);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSavingProfile(false);
    }
  }

  async function discoverWorkstations() {
    const username = credentials.username.trim();
    if (!username) {
      setError("Ingresá el usuario Bejerman para detectar sus puestos.");
      return;
    }
    setDiscovering(true);
    setError("");
    setMessage("");
    try {
      const data = await getUsuarioBejermanWorkstations(user.id, username);
      const options = Array.isArray(data?.options) ? data.options : [];
      setWorkstations(options);
      const recommended = data?.recommended || options[0]?.code || "";
      if (recommended) {
        setCredentials((current) => ({
          ...current,
          workstation: recommended,
        }));
      }
      setMessage(
        options.length
          ? data?.detail || "Puestos Bejerman detectados."
          : data?.detail || "No se detectaron puestos para ese usuario.",
      );
    } catch (requestError) {
      setWorkstations([]);
      setError(errorMessage(requestError));
    } finally {
      setDiscovering(false);
    }
  }

  async function saveCredentials(event) {
    event.preventDefault();
    setSavingCredentials(true);
    setError("");
    setMessage("");
    try {
      const data = await putUsuarioBejermanCredentials(user.id, credentials);
      setCredentials((current) => ({ ...current, username: "", password: "" }));
      setMessage("Credenciales Bejerman validadas y guardadas.");
      await refresh(data?.user || null);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSavingCredentials(false);
    }
  }

  async function reactivatePrompt() {
    setBusyAction("prompt");
    setError("");
    setMessage("");
    try {
      const next = await patchUsuario(user.id, {
        bejerman_credentials_prompt_dismissed: false,
      });
      setMessage("El aviso de credenciales volverá a mostrarse.");
      await refresh(next);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusyAction("");
    }
  }

  async function clearCredentials() {
    if (
      !window.confirm(
        `¿Quitar las credenciales Bejerman de ${user.nombre}? El aviso volverá a aparecer en su próxima sesión.`,
      )
    ) {
      return;
    }
    setBusyAction("clear-credentials");
    setError("");
    setMessage("");
    try {
      const data = await deleteUsuarioBejermanCredentials(user.id);
      setMessage("Credenciales Bejerman eliminadas.");
      await refresh(data?.user || null);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusyAction("");
    }
  }

  async function resetPassword() {
    setBusyAction("reset");
    setError("");
    setMessage("");
    try {
      const result = await onResetPassword?.(user);
      setMessage(
        result?.sent
          ? "Enlace de acceso enviado correctamente."
          : "El enlace se generó, pero el correo no pudo enviarse.",
      );
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusyAction("");
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-label={`Detalle de ${user.nombre}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <aside className="flex h-full w-full max-w-2xl flex-col bg-gray-50 shadow-2xl">
        <header className="flex items-start justify-between gap-4 border-b bg-white px-5 py-4">
          <div className="min-w-0">
            <div className="text-xs font-medium uppercase tracking-wide text-gray-500">
              Usuario #{user.id}
            </div>
            <h2 className="truncate text-xl font-bold text-gray-900">{user.nombre}</h2>
            <p className="truncate text-sm text-gray-600">{user.email}</p>
          </div>
          <button
            type="button"
            className="rounded-md border px-3 py-2 text-sm hover:bg-gray-50"
            onClick={onClose}
          >
            Cerrar
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto p-4 md:p-5">
          {error && (
            <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          )}
          {message && (
            <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">
              {message}
            </div>
          )}

          <Section
            title="Cuenta NEXORA"
            description="Identidad, alta, contraseña y último acceso registrado."
          >
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Definition label="Fecha de alta">
                {formatUserDate(user.creado_en)}
              </Definition>
              <Definition label="Contraseña">
                {user.password_configured ? "Configurada" : "Todavía no configurada"}
              </Definition>
              <Definition label="Invitación o restablecimiento">
                {invitationLabel(user)}
                {user.invitation_expires_at && (
                  <span className="mt-1 block text-xs text-gray-500">
                    Vence: {formatUserDate(user.invitation_expires_at)}
                  </span>
                )}
              </Definition>
              <Definition label="Último acceso">
                {formatUserDate(user.last_login_at)}
              </Definition>
              <Definition label="IP del último acceso">
                {user.last_login_ip || "Sin registro"}
              </Definition>
              <Definition label="Navegador y sistema">
                {user.last_login_at ? userAgentLabel : "Sin registro"}
              </Definition>
            </dl>

            {canManageUsers && (
              <form
                className="mt-5 grid grid-cols-1 gap-3 border-t pt-4 sm:grid-cols-2"
                onSubmit={saveProfile}
              >
                <label className="text-sm font-medium text-gray-700">
                  Nombre
                  <input
                    className={`${inputClass} mt-1`}
                    value={profile.nombre}
                    required
                    onChange={(event) =>
                      setProfile((current) => ({
                        ...current,
                        nombre: event.target.value,
                      }))
                    }
                  />
                </label>
                <label className="text-sm font-medium text-gray-700">
                  Email
                  <input
                    className={`${inputClass} mt-1`}
                    type="email"
                    value={profile.email}
                    required
                    onChange={(event) =>
                      setProfile((current) => ({
                        ...current,
                        email: event.target.value,
                      }))
                    }
                  />
                </label>
                <label className="text-sm font-medium text-gray-700">
                  Rol
                  <select
                    className={`${inputClass} mt-1`}
                    value={profile.rol}
                    onChange={(event) =>
                      setProfile((current) => ({
                        ...current,
                        rol: event.target.value,
                      }))
                    }
                  >
                    {roles.map((role) => (
                      <option key={role.value} value={role.value}>
                        {role.label}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="flex items-end justify-end">
                  <button
                    type="submit"
                    className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
                    disabled={savingProfile}
                  >
                    {savingProfile ? "Guardando..." : "Guardar datos"}
                  </button>
                </div>
              </form>
            )}
          </Section>

          <Section
            title="Bejerman"
            description="La clave está cifrada y nunca se muestra ni se devuelve desde NEXORA."
          >
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Definition label="Estado">
                {user.bejerman_credentials_valid
                  ? "Validado"
                  : user.bejerman_credentials_configured
                    ? "Requiere revisión"
                    : "Pendiente de configuración"}
              </Definition>
              <Definition label="Usuario guardado">
                {user.bejerman_credentials_username || "Sin configurar"}
              </Definition>
              <Definition label="Puesto">
                {user.bejerman_workstation || "Se resolverá según el rol"}
              </Definition>
              <Definition label="Última validación">
                {formatUserDate(user.bejerman_credentials_validated_at)}
              </Definition>
              <Definition label="Aviso al iniciar">
                {user.bejerman_credentials_prompt_dismissed
                  ? "Ocultado por el usuario"
                  : "Habilitado"}
              </Definition>
              <Definition label="Código de vendedor">
                {user.bejerman_seller_code || "Sin asignar"}
              </Definition>
            </div>

            {user.bejerman_credentials_last_error && (
              <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
                <div className="font-medium">Último problema informado</div>
                <div className="mt-1">{user.bejerman_credentials_last_error}</div>
              </div>
            )}

            {isJefe && (
              <>
                <form
                  className="mt-5 space-y-3 border-t pt-4"
                  onSubmit={saveCredentials}
                >
                  <div>
                    <div className="text-sm font-semibold text-gray-900">
                      Configurar o reemplazar credenciales
                    </div>
                    <p className="mt-1 text-xs text-gray-500">
                      Ingresá nuevamente el usuario y la clave. La configuración
                      anterior se conserva si la validación falla.
                    </p>
                  </div>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <label className="text-sm font-medium text-gray-700">
                      Usuario Bejerman
                      <input
                        className={`${inputClass} mt-1`}
                        value={credentials.username}
                        placeholder={user.bejerman_credentials_username || "Ej.: SPVTA"}
                        autoComplete="off"
                        onBlur={() => {
                          if (credentials.username.trim() && !workstations.length) {
                            discoverWorkstations();
                          }
                        }}
                        onChange={(event) => {
                          setCredentials((current) => ({
                            ...current,
                            username: event.target.value.toUpperCase(),
                          }));
                          setWorkstations([]);
                        }}
                        required
                      />
                    </label>
                    <label className="text-sm font-medium text-gray-700">
                      Nueva clave Bejerman
                      <input
                        className={`${inputClass} mt-1`}
                        type="password"
                        value={credentials.password}
                        autoComplete="new-password"
                        onChange={(event) =>
                          setCredentials((current) => ({
                            ...current,
                            password: event.target.value,
                          }))
                        }
                        required
                      />
                    </label>
                    <label className="text-sm font-medium text-gray-700">
                      Puesto Bejerman
                      {workstations.length ? (
                        <select
                          className={`${inputClass} mt-1`}
                          value={credentials.workstation}
                          onChange={(event) =>
                            setCredentials((current) => ({
                              ...current,
                              workstation: event.target.value,
                            }))
                          }
                        >
                          {workstations.map((option) => (
                            <option key={option.code} value={option.code}>
                              {option.label || option.code} ({option.code})
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          className={`${inputClass} mt-1 uppercase`}
                          value={credentials.workstation}
                          placeholder="Se detectará automáticamente"
                          onChange={(event) =>
                            setCredentials((current) => ({
                              ...current,
                              workstation: event.target.value.toUpperCase(),
                            }))
                          }
                        />
                      )}
                    </label>
                    <div className="flex flex-wrap items-end justify-end gap-2">
                      <button
                        type="button"
                        className="rounded-md border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-60"
                        disabled={discovering}
                        onClick={discoverWorkstations}
                      >
                        {discovering ? "Detectando..." : "Detectar puestos"}
                      </button>
                      <button
                        type="submit"
                        className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
                        disabled={savingCredentials}
                      >
                        {savingCredentials ? "Validando..." : "Validar y guardar"}
                      </button>
                    </div>
                  </div>
                </form>

                <form
                  className="mt-4 grid grid-cols-1 gap-3 border-t pt-4 sm:grid-cols-2"
                  onSubmit={saveBejermanPreferences}
                >
                  <label className="text-sm font-medium text-gray-700">
                    Puesto preferido
                    <input
                      className={`${inputClass} mt-1 uppercase`}
                      value={profile.bejerman_workstation}
                      onChange={(event) =>
                        setProfile((current) => ({
                          ...current,
                          bejerman_workstation: event.target.value.toUpperCase(),
                        }))
                      }
                    />
                  </label>
                  <label className="text-sm font-medium text-gray-700">
                    Código de vendedor
                    <input
                      className={`${inputClass} mt-1 uppercase`}
                      value={profile.bejerman_seller_code}
                      maxLength={4}
                      disabled={profile.rol !== "ventas"}
                      placeholder={
                        profile.rol === "ventas" ? "Ej.: V001" : "Solo para Ventas"
                      }
                      onChange={(event) =>
                        setProfile((current) => ({
                          ...current,
                          bejerman_seller_code: event.target.value.toUpperCase(),
                        }))
                      }
                    />
                  </label>
                  <div className="flex flex-wrap gap-2 sm:col-span-2 sm:justify-end">
                    <button
                      type="button"
                      className="rounded-md border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-60"
                      disabled={
                        busyAction === "prompt" ||
                        !user.bejerman_credentials_prompt_dismissed
                      }
                      onClick={reactivatePrompt}
                    >
                      Volver a mostrar el aviso
                    </button>
                    <button
                      type="submit"
                      className="rounded-md border border-blue-200 px-3 py-2 text-sm text-blue-700 hover:bg-blue-50 disabled:opacity-60"
                      disabled={savingProfile}
                    >
                      Guardar configuración
                    </button>
                    <button
                      type="button"
                      className="rounded-md border border-red-200 px-3 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-60"
                      disabled={
                        busyAction === "clear-credentials" ||
                        !user.bejerman_credentials_configured
                      }
                      onClick={clearCredentials}
                    >
                      Quitar credenciales
                    </button>
                  </div>
                </form>
              </>
            )}
          </Section>

          <Section
            title="Permisos y notificaciones"
            description="Los valores personalizados modifican lo heredado por el rol."
          >
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="rounded-md bg-gray-50 p-3">
                <div className="text-sm font-medium">Permisos personalizados</div>
                <div className="mt-1 text-2xl font-bold">
                  {Number(user.permisos_personalizados || 0)}
                </div>
                <div className="mt-1 text-xs text-gray-500">
                  {Number(user.permisos_permitidos || 0)} permitidos ·{" "}
                  {Number(user.permisos_bloqueados || 0)} bloqueados
                </div>
                {canManagePermissions && (
                  <button
                    type="button"
                    className="mt-3 text-sm font-medium text-blue-700 hover:underline"
                    onClick={() => onOpenPermissions?.(user)}
                  >
                    Editar permisos
                  </button>
                )}
              </div>
              <div className="rounded-md bg-gray-50 p-3">
                <div className="text-sm font-medium">
                  Notificaciones personalizadas
                </div>
                <div className="mt-1 text-2xl font-bold">
                  {Number(user.notificaciones_personalizadas || 0)}
                </div>
                <div className="mt-1 text-xs text-gray-500">
                  El resto mantiene el valor predeterminado del rol.
                </div>
                {canManagePermissions && (
                  <button
                    type="button"
                    className="mt-3 text-sm font-medium text-blue-700 hover:underline"
                    onClick={() => onOpenNotifications?.(user)}
                  >
                    Editar notificaciones
                  </button>
                )}
              </div>
            </div>
          </Section>

          {canManageUsers && (
            <Section
              title="Acciones de cuenta"
              description="Las acciones sensibles solicitan confirmación y respetan la protección del último Jefe activo."
            >
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className="rounded-md border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-60"
                  disabled={busyAction === "reset" || !user.activo}
                  onClick={resetPassword}
                >
                  {busyAction === "reset" ? "Generando..." : "Enviar enlace de acceso"}
                </button>
                <button
                  type="button"
                  className="rounded-md border px-3 py-2 text-sm hover:bg-gray-50"
                  onClick={() => onToggleActive?.(user)}
                >
                  {user.activo ? "Desactivar usuario" : "Activar usuario"}
                </button>
              </div>
              <div className="mt-5 border-t border-red-100 pt-4">
                <div className="text-sm font-semibold text-red-800">
                  Zona de peligro
                </div>
                <p className="mt-1 text-xs text-gray-500">
                  Priorizá desactivar la cuenta. La eliminación no se puede deshacer.
                </p>
                <button
                  type="button"
                  className="mt-3 rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white hover:bg-red-700"
                  onClick={() => onDelete?.(user)}
                >
                  Eliminar usuario
                </button>
              </div>
            </Section>
          )}
        </div>
      </aside>
    </div>
  );
}
