import { Fragment, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  RotateCcw,
  Search,
} from "lucide-react";
import {
  getBejermanJobs,
  postBejermanArticleMapping,
  postBejermanJobRetry,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { formatDateTime, formatOS } from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";

const STATUS_LABELS = {
  pending: "En cola",
  running: "Procesando",
  succeeded: "Correcta",
  failed: "Fallida",
  blocked: "Bloqueada",
};

const STATUS_CLASS = {
  pending: "bg-slate-100 text-slate-700 border-slate-200",
  running: "bg-blue-50 text-blue-700 border-blue-200",
  succeeded: "bg-emerald-50 text-emerald-700 border-emerald-200",
  failed: "bg-rose-50 text-rose-700 border-rose-200",
  blocked: "bg-amber-50 text-amber-800 border-amber-200",
};

const TYPE_LABELS = {
  stock_entry_str: "RIS ingreso STR",
  stock_str_to_stl: "STR a STL",
  stock_str_to_stc: "STR a STC",
  stock_str_to_stcl: "STR a STCL",
  stock_exit_rts: "RSS salida",
};

function payloadText(value) {
  if (!value) return "{}";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function StatusBadge({ status }) {
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs ${STATUS_CLASS[status] || STATUS_CLASS.pending}`}>
      {STATUS_LABELS[status] || status || "-"}
    </span>
  );
}

export default function BejermanSync() {
  const { user } = useAuth();
  const canManage = can(user, PERMISSION_CODES.ACTION_BEJERMAN_SYNC_MANAGE);
  const [filters, setFilters] = useState({
    status: "",
    sync_type: "",
    q: "",
    cliente: "",
    articulo: "",
  });
  const [rows, setRows] = useState([]);
  const [counters, setCounters] = useState({});
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [expanded, setExpanded] = useState({});
  const [mappingForms, setMappingForms] = useState({});

  const load = async () => {
    setLoading(true);
    setErr("");
    try {
      const data = await getBejermanJobs(filters);
      setRows(Array.isArray(data?.items) ? data.items : []);
      setCounters(data?.counters || {});
    } catch (e) {
      setErr(e?.message || "No se pudo cargar la cola Bejerman.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const total = useMemo(
    () => Object.values(counters || {}).reduce((acc, value) => acc + Number(value || 0), 0),
    [counters],
  );

  const updateFilter = (key, value) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
  };

  const retry = async (jobId) => {
    setErr("");
    try {
      await postBejermanJobRetry(jobId);
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo reintentar la operación.");
    }
  };

  const confirmMapping = async (job) => {
    const form = mappingForms[job.id] || {};
    const articleCode = (form.article_code || "").trim();
    if (!articleCode) {
      setErr("Ingrese un código de artículo Bejerman.");
      return;
    }
    setErr("");
    try {
      await postBejermanArticleMapping({
        job_id: job.id,
        article_code: articleCode,
        article_description: form.article_description || "",
      });
      setMappingForms((prev) => ({ ...prev, [job.id]: {} }));
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo confirmar el artículo.");
    }
  };

  return (
    <div className="w-full max-w-screen-2xl mx-auto p-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="text-2xl font-bold">Bejerman</h1>
          <p className="text-sm text-gray-600">Operaciones de stock y cola de sincronización.</p>
        </div>
        <button
          type="button"
          className="inline-flex h-10 items-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
          onClick={load}
          disabled={loading}
        >
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          Actualizar
        </button>
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {["blocked", "failed", "pending", "running", "succeeded"].map((status) => (
          <button
            key={status}
            type="button"
            onClick={() => updateFilter("status", filters.status === status ? "" : status)}
            className={`rounded border px-3 py-2 text-left text-sm ${filters.status === status ? "border-gray-900 bg-gray-50" : "border-gray-200 bg-white hover:bg-gray-50"}`}
          >
            <div className="text-xs text-gray-500">{STATUS_LABELS[status]}</div>
            <div className="text-xl font-semibold">{Number(counters?.[status] || 0)}</div>
          </button>
        ))}
        <div className="rounded border border-gray-200 px-3 py-2 text-sm">
          <div className="text-xs text-gray-500">Total filtrado</div>
          <div className="text-xl font-semibold">{total}</div>
        </div>
      </div>

      <div className="mt-4 grid gap-2 md:grid-cols-[160px_180px_1fr_180px_180px_auto]">
        <select
          className="h-10 rounded border border-gray-300 px-2 text-sm"
          value={filters.status}
          onChange={(e) => updateFilter("status", e.target.value)}
        >
          <option value="">Estados</option>
          {Object.entries(STATUS_LABELS).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <select
          className="h-10 rounded border border-gray-300 px-2 text-sm"
          value={filters.sync_type}
          onChange={(e) => updateFilter("sync_type", e.target.value)}
        >
          <option value="">Operaciones</option>
          {Object.entries(TYPE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <input
          className="h-10 rounded border border-gray-300 px-3 text-sm"
          value={filters.q}
          onChange={(e) => updateFilter("q", e.target.value)}
          placeholder="Buscar por OS, serie, cliente, artículo o error"
        />
        <input
          className="h-10 rounded border border-gray-300 px-3 text-sm"
          value={filters.cliente}
          onChange={(e) => updateFilter("cliente", e.target.value)}
          placeholder="Cliente"
        />
        <input
          className="h-10 rounded border border-gray-300 px-3 text-sm"
          value={filters.articulo}
          onChange={(e) => updateFilter("articulo", e.target.value)}
          placeholder="Artículo"
        />
        <button
          type="button"
          className="inline-flex h-10 items-center justify-center gap-2 rounded bg-gray-900 px-4 text-sm text-white hover:bg-gray-800"
          onClick={load}
        >
          <Search className="h-4 w-4" />
          Filtrar
        </button>
      </div>

      {err && <div className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{err}</div>}

      <div className="mt-4 overflow-auto border border-gray-200">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
            <tr>
              <th className="w-8 p-2"></th>
              <th className="p-2">Estado</th>
              <th className="p-2">Operación</th>
              <th className="p-2">Depósitos</th>
              <th className="p-2">OS</th>
              <th className="p-2">Equipo</th>
              <th className="p-2">Serie</th>
              <th className="p-2">Artículo</th>
              <th className="p-2">Intentos</th>
              <th className="p-2">Último mensaje</th>
              <th className="p-2">Actualizado</th>
              <th className="p-2"></th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={12} className="p-4 text-center text-gray-500">Cargando...</td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={12} className="p-4 text-center text-gray-500">Sin operaciones.</td>
              </tr>
            )}
            {!loading && rows.map((row) => {
              const isOpen = !!expanded[row.id];
              const form = mappingForms[row.id] || {};
              return (
                <Fragment key={row.id}>
                  <tr className="border-t border-gray-100 align-top hover:bg-gray-50">
                    <td className="p-2">
                      <button
                        type="button"
                        className="inline-flex h-7 w-7 items-center justify-center rounded border border-gray-200 hover:bg-white"
                        onClick={() => setExpanded((prev) => ({ ...prev, [row.id]: !isOpen }))}
                        aria-label={isOpen ? "Ocultar detalle" : "Ver detalle"}
                      >
                        {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                      </button>
                    </td>
                    <td className="p-2"><StatusBadge status={row.status} /></td>
                    <td className="p-2">{TYPE_LABELS[row.sync_type] || row.sync_type}</td>
                    <td className="p-2 whitespace-nowrap">{row.source_deposit} → {row.target_deposit}</td>
                    <td className="p-2">
                      <Link className="text-blue-700 hover:underline" to={`/ingresos/${row.ingreso_id}`}>
                        {formatOS(row.ingreso_id)}
                      </Link>
                    </td>
                    <td className="p-2 min-w-[220px]">
                      <div className="font-medium">{[row.marca, row.modelo, row.variante].filter(Boolean).join(" ") || "-"}</div>
                      <div className="text-xs text-gray-500">{row.cliente || "-"}</div>
                    </td>
                    <td className="p-2 whitespace-nowrap">
                      <DeviceIdentifier row={row} />
                    </td>
                    <td className="p-2">{row.article_code || "-"}</td>
                    <td className="p-2">{row.attempts ?? 0}</td>
                    <td className="p-2 min-w-[240px] max-w-md">
                      <div className="line-clamp-3 text-xs text-gray-700">{row.last_error || "Sin errores"}</div>
                    </td>
                    <td className="p-2 whitespace-nowrap">{formatDateTime(row.updated_at)}</td>
                    <td className="p-2">
                      {canManage && (row.status === "failed" || row.status === "blocked") && (
                        <button
                          type="button"
                          className="inline-flex h-8 items-center gap-1 rounded border border-gray-300 px-2 text-xs hover:bg-white"
                          onClick={() => retry(row.id)}
                        >
                          <RotateCcw className="h-3.5 w-3.5" />
                          Reintentar
                        </button>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="border-t border-gray-100 bg-gray-50/60">
                      <td></td>
                      <td colSpan={11} className="p-3">
                        {canManage && row.status === "blocked" && !row.article_code && (
                          <div className="mb-3 grid gap-2 md:grid-cols-[200px_1fr_auto]">
                            <input
                              className="h-9 rounded border border-gray-300 px-2 text-sm"
                              value={form.article_code || ""}
                              onChange={(e) => setMappingForms((prev) => ({
                                ...prev,
                                [row.id]: { ...(prev[row.id] || {}), article_code: e.target.value },
                              }))}
                              placeholder="Código Bejerman"
                            />
                            <input
                              className="h-9 rounded border border-gray-300 px-2 text-sm"
                              value={form.article_description || ""}
                              onChange={(e) => setMappingForms((prev) => ({
                                ...prev,
                                [row.id]: { ...(prev[row.id] || {}), article_description: e.target.value },
                              }))}
                              placeholder="Descripción"
                            />
                            <button
                              type="button"
                              className="inline-flex h-9 items-center justify-center gap-2 rounded bg-emerald-700 px-3 text-sm text-white hover:bg-emerald-800"
                              onClick={() => confirmMapping(row)}
                            >
                              <CheckCircle2 className="h-4 w-4" />
                              Confirmar
                            </button>
                          </div>
                        )}
                        <div className="grid gap-3 lg:grid-cols-2">
                          <div>
                            <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Request</div>
                            <pre className="max-h-80 overflow-auto rounded border border-gray-200 bg-white p-3 text-xs">
                              {payloadText(row.request_payload)}
                            </pre>
                          </div>
                          <div>
                            <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Response</div>
                            <pre className="max-h-80 overflow-auto rounded border border-gray-200 bg-white p-3 text-xs">
                              {payloadText(row.response_payload)}
                            </pre>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
