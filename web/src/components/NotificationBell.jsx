import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bell, BellRing, CheckCheck, Mail, Plus, RefreshCw, Settings, Smartphone, Trash2, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  deleteNotificationEmail,
  deletePushNotificationSubscription,
  getNotificationSettings,
  getNotificaciones,
  getPushNotificationConfig,
  postNotificationEmail,
  postNotificacionClick,
  postNotificacionesReadAll,
  postPushNotificationSubscription,
  putNotificationSettings,
} from "@/lib/api";

const REFRESH_MS = 60000;
const SERVICE_WORKER_ENABLED = import.meta.env.VITE_SW === "1";
const CHANNELS = [
  { key: "bell", label: "Campana", Icon: Bell },
  { key: "email", label: "Mail", Icon: Mail },
  { key: "push", label: "Teléfono", Icon: Smartphone },
];
const CHANNEL_BY_KEY = new Map(CHANNELS.map((channel) => [channel.key, channel]));

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("es-AR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function severityClass(severity) {
  if (severity === "critical") return "bg-red-500";
  if (severity === "warning") return "bg-amber-500";
  return "bg-blue-500";
}

function browserSupportsPush() {
  return (
    SERVICE_WORKER_ENABLED &&
    typeof window !== "undefined" &&
    window.isSecureContext &&
    "Notification" in window &&
    "PushManager" in window &&
    "serviceWorker" in navigator
  );
}

function pushUnavailableMessage() {
  if (!SERVICE_WORKER_ENABLED) return "Las notificaciones del teléfono no están activas en este entorno.";
  if (typeof window === "undefined") return "Las notificaciones del teléfono no están disponibles.";
  if (!window.isSecureContext) return "Abrí NEXORA por HTTPS para activar las notificaciones del teléfono.";
  if (!("Notification" in window)) return "Este navegador no permite notificaciones del teléfono.";
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    return "Este navegador no permite notificaciones push.";
  }
  return "";
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = `${base64String}${padding}`.replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

function groupSettingsItems(items) {
  const groups = [];
  const byName = new Map();
  for (const item of items || []) {
    const group = item.group || "General";
    if (!byName.has(group)) {
      const entry = { group, items: [] };
      byName.set(group, entry);
      groups.push(entry);
    }
    byName.get(group).items.push(item);
  }
  return groups;
}

function channelsForItem(item) {
  const raw = Array.isArray(item?.allowed_channels)
    ? item.allowed_channels
    : Array.isArray(item?.channels)
      ? item.channels
      : CHANNELS.map((channel) => channel.key);
  const seen = new Set();
  const out = [];
  for (const key of raw) {
    const channel = CHANNEL_BY_KEY.get(String(key || "").trim());
    if (!channel || seen.has(channel.key)) continue;
    seen.add(channel.key);
    out.push(channel);
  }
  return out.length ? out : CHANNELS;
}

function ToggleButton({ checked, disabled, label, title, onClick, Icon }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={title || label}
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded border text-xs transition disabled:opacity-50 ${
        checked
          ? "border-blue-300 bg-blue-50 text-blue-700"
          : "border-gray-200 bg-white text-gray-400 hover:bg-gray-50"
      }`}
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

export default function NotificationBell() {
  const navigate = useNavigate();
  const rootRef = useRef(null);
  const mountedRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [clickingId, setClickingId] = useState(null);
  const [markingAll, setMarkingAll] = useState(false);
  const [pushConfig, setPushConfig] = useState(null);
  const [pushLoading, setPushLoading] = useState(false);
  const [pushMessage, setPushMessage] = useState("");
  const [settings, setSettings] = useState(null);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [emailDraft, setEmailDraft] = useState("");
  const [emailSaving, setEmailSaving] = useState(false);

  const badgeText = useMemo(() => {
    if (!unreadCount) return "";
    return unreadCount > 99 ? "99+" : String(unreadCount);
  }, [unreadCount]);

  const settingsGroups = useMemo(() => groupSettingsItems(settings?.items || []), [settings]);

  const refresh = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setLoading(true);
    setError("");
    try {
      const data = await getNotificaciones({ limit: 20 });
      if (!mountedRef.current) return;
      setItems(Array.isArray(data?.items) ? data.items : []);
      setUnreadCount(Number(data?.unread_count || 0));
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err?.message || "No se pudieron cargar las notificaciones.");
    } finally {
      if (mountedRef.current && !quiet) setLoading(false);
    }
  }, []);

  const refreshPushConfig = useCallback(async () => {
    try {
      const data = await getPushNotificationConfig();
      if (!mountedRef.current) return;
      setPushConfig(data || null);
    } catch (_) {
      if (!mountedRef.current) return;
      setPushConfig(null);
    }
  }, []);

  const loadSettings = useCallback(async () => {
    setSettingsLoading(true);
    setSettingsError("");
    try {
      const data = await getNotificationSettings();
      if (!mountedRef.current) return;
      setSettings(data || null);
      if (data?.push) setPushConfig(data.push);
    } catch (err) {
      if (!mountedRef.current) return;
      setSettingsError(err?.message || "No se pudo cargar la configuración.");
    } finally {
      if (mountedRef.current) setSettingsLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    refresh({ quiet: true });
    refreshPushConfig();
    const timer = window.setInterval(() => refresh({ quiet: true }), REFRESH_MS);
    const onFocus = () => refresh({ quiet: true });
    window.addEventListener("focus", onFocus);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh, refreshPushConfig]);

  useEffect(() => {
    if (!open) return undefined;
    const onMouseDown = (event) => {
      if (rootRef.current?.contains(event.target)) return;
      setOpen(false);
      setSettingsOpen(false);
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        setOpen(false);
        setSettingsOpen(false);
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  async function handleClickNotification(item) {
    if (!item?.id || clickingId) return;
    setClickingId(item.id);
    setError("");
    try {
      const data = await postNotificacionClick(item.id);
      const wasUnread = !item.read_at;
      const now = new Date().toISOString();
      setItems((prev) =>
        prev.map((row) =>
          Number(row.id) === Number(item.id)
            ? { ...row, read_at: row.read_at || now, clicked_at: row.clicked_at || now }
            : row
        )
      );
      if (wasUnread) setUnreadCount((prev) => Math.max(0, Number(prev || 0) - 1));
      setOpen(false);
      setSettingsOpen(false);
      const href = data?.href || item.href;
      if (href) navigate(href);
    } catch (err) {
      setError(err?.message || "No se pudo abrir la notificación.");
    } finally {
      setClickingId(null);
    }
  }

  async function handleMarkAllRead() {
    if (!unreadCount || markingAll) return;
    setMarkingAll(true);
    setError("");
    try {
      await postNotificacionesReadAll();
      const now = new Date().toISOString();
      setItems((prev) => prev.map((row) => ({ ...row, read_at: row.read_at || now })));
      setUnreadCount(0);
    } catch (err) {
      setError(err?.message || "No se pudieron marcar las notificaciones.");
    } finally {
      setMarkingAll(false);
    }
  }

  async function ensureServiceWorkerRegistration() {
    let registration = await navigator.serviceWorker.getRegistration();
    if (!registration) {
      registration = await navigator.serviceWorker.register("/sw.js");
    }
    return registration || navigator.serviceWorker.ready;
  }

  async function handleEnablePhonePush() {
    if (pushLoading) return;
    setPushLoading(true);
    setPushMessage("");
    setError("");
    setSettingsError("");
    try {
      const unavailableMessage = pushUnavailableMessage();
      if (unavailableMessage) throw new Error(unavailableMessage);
      const config = pushConfig?.publicKey ? pushConfig : await getPushNotificationConfig();
      if (!config?.available || !config?.publicKey) {
        throw new Error("Las notificaciones del teléfono no están configuradas en NEXORA.");
      }
      const permission = await window.Notification.requestPermission();
      if (permission !== "granted") {
        throw new Error("El permiso de notificaciones quedó bloqueado. Revisá los permisos del navegador.");
      }
      const registration = await ensureServiceWorkerRegistration();
      const existing = await registration.pushManager.getSubscription();
      const subscription =
        existing ||
        (await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(config.publicKey),
        }));
      await postPushNotificationSubscription(subscription.toJSON());
      setPushConfig({ ...config, active: true });
      setSettings((prev) => (prev ? { ...prev, push: { ...(prev.push || {}), ...config, active: true } } : prev));
      setPushMessage("Notificaciones del teléfono activadas.");
    } catch (err) {
      const message = err?.message || "No se pudieron activar las notificaciones del teléfono.";
      setError(message);
      setSettingsError(message);
    } finally {
      setPushLoading(false);
    }
  }

  async function handleDisablePhonePush() {
    if (pushLoading) return;
    setPushLoading(true);
    setPushMessage("");
    setError("");
    setSettingsError("");
    try {
      const registration = browserSupportsPush() ? await navigator.serviceWorker.getRegistration() : null;
      const subscription = registration ? await registration.pushManager.getSubscription() : null;
      const endpoint = subscription?.endpoint || "";
      if (subscription) await subscription.unsubscribe().catch(() => false);
      await deletePushNotificationSubscription(endpoint ? { endpoint } : {});
      setPushConfig((prev) => ({ ...(prev || {}), active: false }));
      setSettings((prev) => (prev ? { ...prev, push: { ...(prev.push || {}), active: false } } : prev));
      setPushMessage("Notificaciones del teléfono desactivadas.");
    } catch (err) {
      const message = err?.message || "No se pudieron desactivar las notificaciones del teléfono.";
      setError(message);
      setSettingsError(message);
    } finally {
      setPushLoading(false);
    }
  }

  async function openSettings() {
    setSettingsOpen(true);
    setError("");
    if (!settings) await loadSettings();
  }

  async function handleToggleChannel(item, channel) {
    if (!item?.key || settingsSaving) return;
    const currentChannels = Object.fromEntries(
      channelsForItem(item).map(({ key }) => [key, Boolean(item.effective_channels?.[key])])
    );
    const nextChannels = { ...currentChannels, [channel]: !currentChannels[channel] };
    setSettingsSaving(`${item.key}:${channel}`);
    setSettingsError("");
    try {
      const data = await putNotificationSettings({ preferences: { [item.key]: nextChannels } });
      if (!mountedRef.current) return;
      setSettings(data || null);
      if (data?.push) setPushConfig(data.push);
      refresh({ quiet: true });
    } catch (err) {
      if (!mountedRef.current) return;
      setSettingsError(err?.message || "No se pudo guardar la configuración.");
    } finally {
      if (mountedRef.current) setSettingsSaving("");
    }
  }

  async function handleAddEmail(event) {
    event.preventDefault();
    const email = emailDraft.trim();
    if (!email || emailSaving) return;
    setEmailSaving(true);
    setSettingsError("");
    try {
      await postNotificationEmail({ email });
      setEmailDraft("");
      await loadSettings();
    } catch (err) {
      setSettingsError(err?.message || "No se pudo agregar el email.");
    } finally {
      setEmailSaving(false);
    }
  }

  async function handleDeleteEmail(emailId) {
    if (!emailId || emailSaving) return;
    setEmailSaving(true);
    setSettingsError("");
    try {
      await deleteNotificationEmail(emailId);
      await loadSettings();
    } catch (err) {
      setSettingsError(err?.message || "No se pudo quitar el email.");
    } finally {
      setEmailSaving(false);
    }
  }

  function renderPushControl({ compact = false } = {}) {
    return (
      <div className={compact ? "border-b px-3 py-2" : "space-y-2"}>
        <button
          type="button"
          className={`inline-flex w-full items-center justify-center gap-2 rounded border px-3 py-2 text-sm font-medium disabled:opacity-50 ${
            pushConfig?.active
              ? "border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100"
              : "border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100"
          }`}
          onClick={pushConfig?.active ? handleDisablePhonePush : handleEnablePhonePush}
          disabled={pushLoading}
          title={pushConfig?.active ? "Desactivar notificaciones del teléfono" : "Activar notificaciones del teléfono"}
        >
          <BellRing className="h-4 w-4" aria-hidden="true" />
          <span>{pushConfig?.active ? "Desactivar en este teléfono" : "Activar en este teléfono"}</span>
        </button>
        {pushMessage && <div className="text-xs text-gray-500">{pushMessage}</div>}
      </div>
    );
  }

  function renderSettings() {
    const canManageExtraEmails = Boolean(settings?.capabilities?.canManageExtraEmails);
    return (
      <>
        <div className="border-b px-3 py-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-gray-900">Configuración</div>
              <div className="text-xs text-gray-500">Canales de notificación de tu usuario</div>
            </div>
            <button
              type="button"
              className="inline-flex h-8 w-8 items-center justify-center rounded text-gray-600 hover:bg-gray-50"
              onClick={() => setSettingsOpen(false)}
              title="Cerrar configuración"
              aria-label="Cerrar configuración"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </div>

        <div className="max-h-[76vh] overflow-auto">
          {settingsError && (
            <div className="m-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {settingsError}
            </div>
          )}

          {settingsLoading && !settings ? (
            <div className="px-4 py-8 text-center text-sm text-gray-500">Cargando configuración...</div>
          ) : (
            <div className="space-y-4 p-3">
              <section className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-sm font-medium text-gray-900">Teléfono</div>
                    <div className="text-xs text-gray-500">Este dispositivo recibe push si el canal Teléfono está activo.</div>
                  </div>
                </div>
                {renderPushControl()}
              </section>

              <section className="space-y-2">
                <div className="text-sm font-medium text-gray-900">Emails</div>
                <div className="rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
                  {settings?.primary_email || "Tu usuario no tiene email principal cargado."}
                </div>
                {canManageExtraEmails && (
                  <form className="flex gap-2" onSubmit={handleAddEmail}>
                    <input
                      type="email"
                      value={emailDraft}
                      onChange={(event) => setEmailDraft(event.target.value)}
                      className="min-w-0 flex-1 rounded border border-gray-300 px-3 py-2 text-sm"
                      placeholder="email extra"
                      disabled={emailSaving}
                    />
                    <button
                      type="submit"
                      className="inline-flex h-9 w-9 items-center justify-center rounded border border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 disabled:opacity-50"
                      disabled={!emailDraft.trim() || emailSaving}
                      title="Agregar email"
                      aria-label="Agregar email"
                    >
                      <Plus className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </form>
                )}
                {settings?.extra_emails?.length ? (
                  <div className="space-y-1">
                    {settings.extra_emails.map((row) => (
                      <div key={row.id} className="flex items-center justify-between gap-2 rounded border border-gray-200 px-3 py-2 text-sm">
                        <span className="min-w-0 truncate text-gray-700">{row.email}</span>
                        {canManageExtraEmails && (
                          <button
                            type="button"
                            className="inline-flex h-7 w-7 items-center justify-center rounded text-gray-500 hover:bg-red-50 hover:text-red-700 disabled:opacity-50"
                            onClick={() => handleDeleteEmail(row.id)}
                            disabled={emailSaving}
                            title="Quitar email"
                            aria-label={`Quitar ${row.email}`}
                          >
                            <Trash2 className="h-4 w-4" aria-hidden="true" />
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                ) : null}
              </section>

              <section className="space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-sm font-medium text-gray-900">Tipos de notificación</div>
                  <div className="hidden gap-1 text-[11px] text-gray-500 sm:flex">
                    {CHANNELS.map(({ key, label }) => (
                      <span key={key} className="w-10 text-center">{label}</span>
                    ))}
                  </div>
                </div>
                {settingsGroups.map((group) => (
                  <div key={group.group} className="space-y-1">
                    <div className="px-1 text-xs font-semibold uppercase tracking-wide text-gray-500">{group.group}</div>
                    <div className="divide-y divide-gray-100 rounded border border-gray-200">
                      {group.items.map((item) => (
                        <div key={item.key} className="flex flex-col gap-2 px-3 py-2 sm:flex-row sm:items-center">
                          <div className="min-w-0 flex-1">
                            <div className="truncate text-sm font-medium text-gray-900">{item.label}</div>
                            <div className="line-clamp-2 text-xs text-gray-500">{item.description}</div>
                          </div>
                          <div className="flex items-center gap-1">
                            {channelsForItem(item).map(({ key, label, Icon }) => (
                              <ToggleButton
                                key={key}
                                Icon={Icon}
                                label={`${label}: ${item.label}`}
                                title={`${label}: ${item.label}`}
                                checked={Boolean(item.effective_channels?.[key])}
                                disabled={settingsSaving === `${item.key}:${key}`}
                                onClick={() => handleToggleChannel(item, key)}
                              />
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </section>
            </div>
          )}
        </div>
      </>
    );
  }

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        className="relative inline-flex h-9 w-9 items-center justify-center rounded border border-gray-200 text-gray-700 hover:bg-gray-50"
        aria-label="Abrir notificaciones"
        aria-haspopup="dialog"
        aria-expanded={open}
        title="Notificaciones"
        onClick={() => {
          setOpen((value) => !value);
          if (!open) refresh({ quiet: true });
        }}
      >
        <Bell className="h-4 w-4" aria-hidden="true" />
        {badgeText && (
          <span className="absolute -right-1 -top-1 min-w-5 rounded-full bg-red-600 px-1.5 py-0.5 text-center text-[10px] font-semibold leading-none text-white">
            {badgeText}
          </span>
        )}
      </button>

      {open && (
        <div
          className="fixed left-3 right-3 top-14 z-50 rounded-lg border border-gray-200 bg-white shadow-xl md:absolute md:left-auto md:right-0 md:top-10 md:w-[42rem] md:max-w-[calc(100vw-2rem)]"
          role="dialog"
          aria-label={settingsOpen ? "Configuración de notificaciones" : "Notificaciones"}
        >
          {settingsOpen ? (
            renderSettings()
          ) : (
            <>
              <div className="flex items-center justify-between border-b px-3 py-2">
                <div>
                  <div className="text-sm font-semibold text-gray-900">Notificaciones</div>
                  <div className="text-xs text-gray-500">
                    {unreadCount === 1 ? "1 pendiente" : `${unreadCount} pendientes`}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    className="inline-flex h-8 w-8 items-center justify-center rounded text-gray-600 hover:bg-gray-50 disabled:opacity-40"
                    onClick={handleMarkAllRead}
                    disabled={!unreadCount || markingAll}
                    title="Marcar todas como leídas"
                    aria-label="Marcar todas como leídas"
                  >
                    <CheckCheck className="h-4 w-4" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="inline-flex h-8 w-8 items-center justify-center rounded text-gray-600 hover:bg-gray-50 disabled:opacity-40"
                    onClick={openSettings}
                    title="Configurar notificaciones"
                    aria-label="Configurar notificaciones"
                  >
                    <Settings className="h-4 w-4" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="inline-flex h-8 w-8 items-center justify-center rounded text-gray-600 hover:bg-gray-50 disabled:opacity-40"
                    onClick={() => refresh()}
                    disabled={loading}
                    title="Actualizar"
                    aria-label="Actualizar notificaciones"
                  >
                    <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
                  </button>
                </div>
              </div>

              {renderPushControl({ compact: true })}

              {error && (
                <div className="m-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                  {error}
                </div>
              )}

              <div className="max-h-[70vh] overflow-auto py-1">
                {loading && !items.length ? (
                  <div className="px-4 py-6 text-center text-sm text-gray-500">Cargando...</div>
                ) : items.length ? (
                  items.map((item) => {
                    const unread = !item.read_at;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        className={`flex w-full gap-3 px-3 py-3 text-left hover:bg-gray-50 disabled:opacity-60 ${
                          unread ? "" : "opacity-75"
                        }`}
                        onClick={() => handleClickNotification(item)}
                        disabled={clickingId === item.id}
                      >
                        <span
                          className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
                            unread ? severityClass(item.severity) : "bg-gray-300"
                          }`}
                          aria-hidden="true"
                        />
                        <span className="min-w-0 flex-1">
                          <span className={`block truncate text-sm ${unread ? "font-medium text-gray-900" : "text-gray-700"}`}>
                            {item.title || "Notificación"}
                          </span>
                          {item.body && (
                            <span className="mt-1 line-clamp-3 whitespace-pre-line text-xs text-gray-600">
                              {item.body}
                            </span>
                          )}
                          <span className="mt-1 block text-[11px] text-gray-400">
                            {formatDate(item.created_at)}
                          </span>
                        </span>
                      </button>
                    );
                  })
                ) : (
                  <div className="px-4 py-8 text-center text-sm text-gray-500">
                    No hay notificaciones recientes.
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
