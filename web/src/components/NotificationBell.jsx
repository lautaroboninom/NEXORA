import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bell, CheckCheck, RefreshCw } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { getNotificaciones, postNotificacionClick, postNotificacionesReadAll } from "@/lib/api";

const REFRESH_MS = 60000;

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

export default function NotificationBell() {
  const navigate = useNavigate();
  const rootRef = useRef(null);
  const mountedRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [clickingId, setClickingId] = useState(null);
  const [markingAll, setMarkingAll] = useState(false);

  const badgeText = useMemo(() => {
    if (!unreadCount) return "";
    return unreadCount > 99 ? "99+" : String(unreadCount);
  }, [unreadCount]);

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

  useEffect(() => {
    mountedRef.current = true;
    refresh({ quiet: true });
    const timer = window.setInterval(() => refresh({ quiet: true }), REFRESH_MS);
    const onFocus = () => refresh({ quiet: true });
    window.addEventListener("focus", onFocus);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  useEffect(() => {
    if (!open) return undefined;
    const onMouseDown = (event) => {
      if (rootRef.current?.contains(event.target)) return;
      setOpen(false);
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") setOpen(false);
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
          className="fixed left-3 right-3 top-14 z-50 rounded-lg border border-gray-200 bg-white shadow-xl md:absolute md:left-auto md:right-0 md:top-10 md:w-96"
          role="dialog"
          aria-label="Notificaciones"
        >
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
                onClick={() => refresh()}
                disabled={loading}
                title="Actualizar"
                aria-label="Actualizar notificaciones"
              >
                <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
              </button>
            </div>
          </div>

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
        </div>
      )}
    </div>
  );
}
