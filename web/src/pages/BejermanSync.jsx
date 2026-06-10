import { Fragment, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Pencil,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  X,
} from "lucide-react";
import {
  getBejermanArticles,
  getBejermanJobs,
  getDeviceEditable,
  patchDeviceEditable,
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
  stock_entry_str: "Ingreso STR bloqueado",
  stock_str_to_stl: "STR a STL",
  stock_str_to_stc: "STR a STC",
  stock_str_to_stcl: "STR a STCL",
};

function payloadText(value) {
  if (!value) return "{}";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function jobMessage(row) {
  const raw = row?.last_error || "";
  let message = raw;
  const comparable = String(message)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\?/g, "")
    .toLowerCase();
  if (comparable.includes("articulo bejerman para restaurar stock en destino")) {
    message = "No se pudo restaurar stock en destino: falta definir el artículo Bejerman.";
  }
  const payload = row?.response_payload || {};
  const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
  const diagnostic = payload.stock_restore_diagnostic || null;
  const details = [];
  if (candidates.length > 1 && !message.toLowerCase().includes("candidato")) {
    details.push(`${candidates.length} candidatos posibles`);
  } else if (candidates.length === 0 && message.includes("artículo Bejerman")) {
    details.push("sin candidatos por marca/modelo/variante");
  }
  if (diagnostic?.source_deposit || diagnostic?.target_deposit) {
    details.push(`consultado ${diagnostic.source_deposit || "-"} → ${diagnostic.target_deposit || "-"}`);
  }
  return details.length ? `${message} (${details.join("; ")})` : (message || "Sin errores");
}

function resolutionCandidates(row) {
  const fromResolution = row?.article_resolution?.candidates;
  if (Array.isArray(fromResolution) && fromResolution.length) return fromResolution;
  const payloadCandidates = row?.response_payload?.candidates;
  return Array.isArray(payloadCandidates) ? payloadCandidates : [];
}

function scopeText(row) {
  const ctx = row?.article_resolution?.context || row || {};
  return [ctx.marca, ctx.modelo, ctx.variante].filter(Boolean).join(" ") || "-";
}

function articleMappingOf(row) {
  return row?.article_mapping || row?.article_resolution?.mapping || null;
}

function effectiveArticleCode(row) {
  const mappingCode = (articleMappingOf(row)?.article_code || "").trim();
  return mappingCode || (row?.article_code || "").trim();
}

function effectiveArticleDescription(row) {
  return (articleMappingOf(row)?.article_description || "").trim();
}

function trimValue(value) {
  return String(value || "").trim();
}

function candidateBadges(candidate) {
  const flags = candidate?.flags || {};
  const badges = [];
  if (candidate?.score !== undefined) badges.push(`Score ${candidate.score}`);
  if (flags.stock_by_partida === true) badges.push("Stock por partida");
  if (flags.participates_stock === true) badges.push("Participa en stock");
  if (flags.deposit) badges.push(`Depósito ${flags.deposit}`);
  return badges;
}

function ArticleCandidateCard({ candidate, onSelect, selected }) {
  const warnings = Array.isArray(candidate?.warnings) ? candidate.warnings : [];
  const reasons = Array.isArray(candidate?.reasons) ? candidate.reasons : [];
  return (
    <div className={`rounded border bg-white p-3 ${selected ? "border-emerald-500 ring-1 ring-emerald-500" : "border-gray-200"}`}>
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="font-mono text-sm font-semibold text-gray-900">{candidate?.article_code || "-"}</div>
          <div className="mt-1 text-sm text-gray-800">{candidate?.article_description || "Sin descripción"}</div>
        </div>
        <button
          type="button"
          className="inline-flex h-8 shrink-0 items-center justify-center gap-1 rounded bg-emerald-700 px-3 text-xs font-medium text-white hover:bg-emerald-800"
          onClick={() => onSelect(candidate)}
        >
          <CheckCircle2 className="h-3.5 w-3.5" />
          Usar este artículo
        </button>
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {candidateBadges(candidate).map((badge) => (
          <span key={badge} className="rounded border border-gray-200 bg-gray-50 px-2 py-0.5 text-[11px] text-gray-700">
            {badge}
          </span>
        ))}
      </div>
      {reasons.length > 0 && (
        <div className="mt-2 text-xs text-emerald-800">{reasons.join(" - ")}</div>
      )}
      {warnings.length > 0 && (
        <div className="mt-2 text-xs text-amber-800">{warnings.join(" - ")}</div>
      )}
    </div>
  );
}

function ArticleResolutionPanel({
  row,
  canManage,
  form,
  searchForm,
  searchState,
  onSelectCandidate,
  onUpdateManual,
  onUpdateSearch,
  onSearch,
  onConfirm,
}) {
  if (!canManage || row.status !== "blocked" || row.article_code) return null;
  const suggested = resolutionCandidates(row);
  const searchItems = Array.isArray(searchState?.items) ? searchState.items : [];
  const related = row?.article_resolution?.related_blocked_jobs || searchState?.related_blocked_jobs || 0;
  const selectedCode = (form.article_code || "").trim();
  const selectedDescription = form.article_description || form.selectedCandidate?.article_description || "";

  return (
    <div className="mb-3 rounded border border-gray-200 bg-white p-3">
      <div className="flex flex-col gap-1 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="text-sm font-semibold text-gray-900">Resolver artículo Bejerman</div>
          <div className="text-xs text-gray-600">Aplica a marca + modelo + variante: {scopeText(row)}</div>
        </div>
        <div className="text-xs text-gray-600">{related} jobs relacionados para reabrir</div>
      </div>

      {suggested.length > 0 && (
        <div className="mt-3">
          <div className="mb-2 text-xs font-semibold uppercase text-gray-500">Candidatos sugeridos</div>
          <div className="grid gap-2 xl:grid-cols-2">
            {suggested.slice(0, 6).map((candidate, index) => (
              <ArticleCandidateCard
                key={`${candidate?.article_code || "candidate"}-${index}`}
                candidate={candidate}
                selected={selectedCode && selectedCode === candidate?.article_code}
                onSelect={onSelectCandidate}
              />
            ))}
          </div>
        </div>
      )}

      <div className="mt-3 grid gap-2 md:grid-cols-[1fr_auto]">
        <input
          className="h-9 rounded border border-gray-300 px-2 text-sm"
          value={searchForm.query || ""}
          onChange={(e) => onUpdateSearch(e.target.value)}
          placeholder="Buscar otro artículo por código o descripción"
        />
        <button
          type="button"
          className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
          onClick={onSearch}
          disabled={searchState?.loading}
        >
          <Search className="h-4 w-4" />
          {searchState?.loading ? "Buscando..." : "Buscar"}
        </button>
      </div>

      {searchState?.error && (
        <div className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{searchState.error}</div>
      )}

      {searchItems.length > 0 && (
        <div className="mt-3">
          <div className="mb-2 text-xs font-semibold uppercase text-gray-500">Resultados de búsqueda</div>
          <div className="grid gap-2 xl:grid-cols-2">
            {searchItems.map((candidate, index) => (
              <ArticleCandidateCard
                key={`search-${candidate?.article_code || "candidate"}-${index}`}
                candidate={candidate}
                selected={selectedCode && selectedCode === candidate?.article_code}
                onSelect={onSelectCandidate}
              />
            ))}
          </div>
        </div>
      )}

      <div className="mt-3 grid gap-2 md:grid-cols-[180px_1fr]">
        <input
          className="h-9 rounded border border-gray-300 px-2 text-sm"
          value={form.article_code || ""}
          onChange={(e) => onUpdateManual("article_code", e.target.value)}
          placeholder="Código manual"
        />
        <input
          className="h-9 rounded border border-gray-300 px-2 text-sm"
          value={form.article_description || ""}
          onChange={(e) => onUpdateManual("article_description", e.target.value)}
          placeholder="Descripción opcional"
        />
      </div>

      {selectedCode && (
        <div className="mt-3 rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-950">
          <div className="font-semibold">Confirmar mapeo</div>
          <div className="mt-1 text-xs">
            Equipo: {scopeText(row)}. Artículo: <span className="font-mono">{selectedCode}</span>
            {selectedDescription ? ` - ${selectedDescription}` : ""}. Se reabrirán {related} jobs relacionados.
          </div>
          <button
            type="button"
            className="mt-2 inline-flex h-9 items-center justify-center gap-2 rounded bg-emerald-700 px-3 text-sm text-white hover:bg-emerald-800"
            onClick={onConfirm}
          >
            <CheckCircle2 className="h-4 w-4" />
            Confirmar y reabrir
          </button>
        </div>
      )}
    </div>
  );
}

function BejermanEditModal({
  modal,
  onClose,
  onChangeDevice,
  onChangeArticle,
  onChangeSearch,
  onSearch,
  onSelectCandidate,
  onSave,
}) {
  if (!modal) return null;
  const row = modal.row || {};
  const device = modal.device || {};
  const deviceForm = modal.deviceForm || {};
  const articleForm = modal.articleForm || {};
  const searchItems = Array.isArray(modal.searchItems) ? modal.searchItems : [];
  const selectedCode = trimValue(articleForm.article_code);
  const title = scopeText(row);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      onClick={() => { if (!modal.saving) onClose(); }}
    >
      <div className="w-full max-w-4xl max-h-[92vh] overflow-y-auto rounded bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 p-4">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-gray-900">Editar equipo Bejerman</h2>
            <div className="mt-1 truncate text-sm text-gray-600">{title}</div>
          </div>
          <button
            type="button"
            className="inline-flex h-8 w-8 items-center justify-center rounded border border-gray-300 hover:bg-gray-50 disabled:opacity-60"
            onClick={onClose}
            disabled={modal.saving}
            aria-label="Cerrar"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="p-4">
          {modal.error && (
            <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{modal.error}</div>
          )}

          {modal.loading ? (
            <div className="py-8 text-center text-sm text-gray-500">Cargando equipo...</div>
          ) : (
            <div className="space-y-4">
              <div className="grid gap-3 rounded border border-gray-200 bg-gray-50 p-3 text-sm md:grid-cols-4">
                <div>
                  <div className="text-xs font-medium text-gray-500">Equipo</div>
                  <div className="font-medium text-gray-900">#{row.device_id || device.id || "-"}</div>
                </div>
                <div>
                  <div className="text-xs font-medium text-gray-500">Cliente</div>
                  <div className="truncate text-gray-900">{device.customer_nombre || row.cliente || "-"}</div>
                </div>
                <div>
                  <div className="text-xs font-medium text-gray-500">Marca</div>
                  <div className="truncate text-gray-900">{device.marca || row.marca || "-"}</div>
                </div>
                <div>
                  <div className="text-xs font-medium text-gray-500">Modelo</div>
                  <div className="truncate text-gray-900">{device.modelo || row.modelo || "-"}</div>
                </div>
              </div>

              <div className="grid gap-4 lg:grid-cols-2">
                <div className="rounded border border-gray-200 p-3">
                  <div className="mb-3 text-sm font-semibold text-gray-900">Identificadores</div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="block">
                      <span className="text-xs font-medium text-gray-600">N° serie</span>
                      <input
                        className="mt-1 h-10 w-full rounded border border-gray-300 px-3 text-sm"
                        value={deviceForm.numero_serie || ""}
                        onChange={(e) => onChangeDevice("numero_serie", e.target.value)}
                        disabled={modal.saving}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-gray-600">N° interno (MG)</span>
                      <input
                        className="mt-1 h-10 w-full rounded border border-gray-300 px-3 text-sm"
                        value={deviceForm.numero_interno || ""}
                        onChange={(e) => onChangeDevice("numero_interno", e.target.value)}
                        disabled={modal.saving}
                      />
                    </label>
                  </div>
                </div>

                <div className="rounded border border-gray-200 p-3">
                  <div className="mb-3 text-sm font-semibold text-gray-900">Artículo Bejerman</div>
                  <div className="grid gap-3 sm:grid-cols-[160px_1fr]">
                    <label className="block">
                      <span className="text-xs font-medium text-gray-600">Código</span>
                      <input
                        className="mt-1 h-10 w-full rounded border border-gray-300 px-3 font-mono text-sm"
                        value={articleForm.article_code || ""}
                        onChange={(e) => onChangeArticle("article_code", e.target.value)}
                        disabled={modal.saving}
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-medium text-gray-600">Descripción</span>
                      <input
                        className="mt-1 h-10 w-full rounded border border-gray-300 px-3 text-sm"
                        value={articleForm.article_description || ""}
                        onChange={(e) => onChangeArticle("article_description", e.target.value)}
                        disabled={modal.saving}
                      />
                    </label>
                  </div>
                </div>
              </div>

              <div className="rounded border border-gray-200 p-3">
                <div className="grid gap-2 md:grid-cols-[1fr_auto]">
                  <input
                    className="h-9 rounded border border-gray-300 px-2 text-sm"
                    value={modal.searchQuery || ""}
                    onChange={(e) => onChangeSearch(e.target.value)}
                    placeholder="Buscar artículo por código o descripción"
                    disabled={modal.saving}
                  />
                  <button
                    type="button"
                    className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
                    onClick={onSearch}
                    disabled={modal.saving || modal.searchLoading}
                  >
                    <Search className="h-4 w-4" />
                    {modal.searchLoading ? "Buscando..." : "Buscar"}
                  </button>
                </div>
                {modal.searchError && (
                  <div className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{modal.searchError}</div>
                )}
                {searchItems.length > 0 && (
                  <div className="mt-3 grid gap-2 xl:grid-cols-2">
                    {searchItems.map((candidate, index) => (
                      <ArticleCandidateCard
                        key={`modal-search-${candidate?.article_code || "candidate"}-${index}`}
                        candidate={candidate}
                        selected={selectedCode && selectedCode === candidate?.article_code}
                        onSelect={onSelectCandidate}
                      />
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-gray-200 p-4">
          <button
            type="button"
            className="h-9 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
            onClick={onClose}
            disabled={modal.saving}
          >
            Cancelar
          </button>
          <button
            type="button"
            className="inline-flex h-9 items-center justify-center gap-2 rounded bg-gray-900 px-3 text-sm text-white hover:bg-gray-800 disabled:opacity-60"
            onClick={onSave}
            disabled={modal.loading || modal.saving}
          >
            <Save className="h-4 w-4" />
            {modal.saving ? "Guardando..." : "Guardar"}
          </button>
        </div>
      </div>
    </div>
  );
}

function JobDiagnostic({ row }) {
  const payload = row?.response_payload || {};
  const diagnostic = payload.stock_restore_diagnostic || null;
  if (!diagnostic) return null;
  const sourceCount = Number(diagnostic?.source_record_count || 0);
  const targetCount = Number(diagnostic?.target_record_count || 0);
  return (
    <div className="mb-3 rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-950">
      <div className="grid gap-2 md:grid-cols-3">
        <div>
          <div className="font-semibold uppercase tracking-wide text-amber-700">Partida</div>
          <div className="mt-0.5 font-mono">{diagnostic?.serial || row.numero_serie || "-"}</div>
        </div>
        <div>
          <div className="font-semibold uppercase tracking-wide text-amber-700">Depósitos</div>
          <div className="mt-0.5">{diagnostic ? `${diagnostic.source_deposit || "-"} → ${diagnostic.target_deposit || "-"}` : `${row.source_deposit || "-"} → ${row.target_deposit || "-"}`}</div>
        </div>
        <div>
          <div className="font-semibold uppercase tracking-wide text-amber-700">Stock consultado</div>
          <div className="mt-0.5">Origen: {sourceCount} · Destino: {targetCount}</div>
        </div>
      </div>
      {diagnostic?.article_resolution_error && (
        <div className="mt-2 text-amber-900">{diagnostic.article_resolution_error}</div>
      )}
    </div>
  );
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
  const [msg, setMsg] = useState("");
  const [expanded, setExpanded] = useState({});
  const [mappingForms, setMappingForms] = useState({});
  const [articleSearchForms, setArticleSearchForms] = useState({});
  const [articleSearchState, setArticleSearchState] = useState({});
  const [editModal, setEditModal] = useState(null);

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
    setMsg("");
    try {
      await postBejermanJobRetry(jobId);
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo reintentar la operación.");
    }
  };

  const openEditModal = async (row) => {
    if (!row?.id || !row?.device_id) return;
    setErr("");
    setMsg("");
    const initialArticle = {
      article_code: effectiveArticleCode(row),
      article_description: effectiveArticleDescription(row),
    };
    const fallbackDevice = {
      id: row.device_id,
      numero_serie: row.numero_serie || "",
      numero_interno: row.numero_interno || "",
      customer_nombre: row.cliente || "",
      marca: row.marca || "",
      modelo: row.modelo || "",
      variante: row.variante || "",
    };
    setEditModal({
      row,
      loading: true,
      saving: false,
      error: "",
      device: fallbackDevice,
      initialDevice: {
        numero_serie: fallbackDevice.numero_serie,
        numero_interno: fallbackDevice.numero_interno,
      },
      deviceForm: {
        numero_serie: fallbackDevice.numero_serie,
        numero_interno: fallbackDevice.numero_interno,
      },
      initialArticle,
      articleForm: initialArticle,
      searchQuery: "",
      searchLoading: false,
      searchError: "",
      searchItems: [],
    });
    try {
      const data = await getDeviceEditable(row.device_id);
      const device = data?.device || fallbackDevice;
      const initialDevice = {
        numero_serie: device.numero_serie || "",
        numero_interno: device.numero_interno || "",
      };
      setEditModal((prev) => (
        prev?.row?.id === row.id
          ? {
              ...prev,
              loading: false,
              device,
              initialDevice,
              deviceForm: initialDevice,
            }
          : prev
      ));
    } catch (e) {
      setEditModal((prev) => (
        prev?.row?.id === row.id
          ? {
              ...prev,
              loading: false,
              error: e?.message || "No se pudo cargar el equipo.",
            }
          : prev
      ));
    }
  };

  const updateEditDevice = (key, value) => {
    setEditModal((prev) => prev ? ({
      ...prev,
      deviceForm: { ...(prev.deviceForm || {}), [key]: value },
    }) : prev);
  };

  const updateEditArticle = (key, value) => {
    setEditModal((prev) => prev ? ({
      ...prev,
      articleForm: { ...(prev.articleForm || {}), [key]: value },
    }) : prev);
  };

  const updateModalSearch = (query) => {
    setEditModal((prev) => prev ? ({ ...prev, searchQuery: query }) : prev);
  };

  const searchModalArticles = async () => {
    const modal = editModal;
    if (!modal?.row?.id) return;
    const query = trimValue(modal.searchQuery);
    setEditModal((prev) => prev ? ({ ...prev, searchLoading: true, searchError: "" }) : prev);
    try {
      const data = await getBejermanArticles({ job_id: modal.row.id, q: query });
      setEditModal((prev) => (
        prev?.row?.id === modal.row.id
          ? {
              ...prev,
              searchLoading: false,
              searchItems: Array.isArray(data?.items) ? data.items : [],
              searchError: "",
            }
          : prev
      ));
    } catch (e) {
      setEditModal((prev) => (
        prev?.row?.id === modal.row.id
          ? {
              ...prev,
              searchLoading: false,
              searchError: e?.message || "No se pudo buscar artículos Bejerman.",
            }
          : prev
      ));
    }
  };

  const selectModalCandidate = (candidate) => {
    setEditModal((prev) => prev ? ({
      ...prev,
      articleForm: {
        ...(prev.articleForm || {}),
        article_code: candidate?.article_code || "",
        article_description: candidate?.article_description || "",
      },
    }) : prev);
  };

  const saveEditModal = async () => {
    const modal = editModal;
    if (!modal?.row) return;
    const deviceId = modal.row.device_id;
    const deviceForm = modal.deviceForm || {};
    const initialDevice = modal.initialDevice || {};
    const articleForm = modal.articleForm || {};
    const initialArticle = modal.initialArticle || {};
    const nextArticleCode = trimValue(articleForm.article_code);
    const nextArticleDescription = trimValue(articleForm.article_description);
    const articleChanged =
      nextArticleCode !== trimValue(initialArticle.article_code) ||
      nextArticleDescription !== trimValue(initialArticle.article_description);
    const devicePayload = {};
    if (trimValue(deviceForm.numero_serie) !== trimValue(initialDevice.numero_serie)) {
      devicePayload.numero_serie = trimValue(deviceForm.numero_serie);
    }
    if (trimValue(deviceForm.numero_interno) !== trimValue(initialDevice.numero_interno)) {
      devicePayload.numero_interno = trimValue(deviceForm.numero_interno);
    }

    if (!Object.keys(devicePayload).length && !articleChanged) {
      setEditModal((prev) => prev ? ({ ...prev, error: "No hay cambios para guardar." }) : prev);
      return;
    }
    if (articleChanged && !nextArticleCode) {
      setEditModal((prev) => prev ? ({ ...prev, error: "El código de artículo Bejerman no puede quedar vacío." }) : prev);
      return;
    }

    setEditModal((prev) => prev ? ({ ...prev, saving: true, error: "" }) : prev);
    try {
      let articleResult = null;
      if (Object.keys(devicePayload).length) {
        await patchDeviceEditable(deviceId, devicePayload);
      }
      if (articleChanged) {
        articleResult = await postBejermanArticleMapping({
          job_id: modal.row.id,
          article_code: nextArticleCode,
          article_description: nextArticleDescription,
        });
      }
      setEditModal(null);
      const reopened = Number(articleResult?.reopened_jobs || 0);
      const details = reopened > 0 ? ` Jobs reabiertos: ${reopened}.` : "";
      setMsg(`Cambios guardados.${details}`);
      await load();
    } catch (e) {
      setEditModal((prev) => prev ? ({
        ...prev,
        saving: false,
        error: e?.message || "No se pudieron guardar los cambios.",
      }) : prev);
    }
  };

  const updateMappingForm = (jobId, key, value) => {
    setMappingForms((prev) => ({
      ...prev,
      [jobId]: {
        ...(prev[jobId] || {}),
        [key]: value,
        ...(key === "article_code" ? { selectedCandidate: null } : {}),
      },
    }));
  };

  const selectCandidate = (jobId, candidate) => {
    setErr("");
    setMsg("");
    setMappingForms((prev) => ({
      ...prev,
      [jobId]: {
        ...(prev[jobId] || {}),
        article_code: candidate?.article_code || "",
        article_description: candidate?.article_description || "",
        selectedCandidate: candidate,
      },
    }));
  };

  const updateArticleSearch = (jobId, query) => {
    setArticleSearchForms((prev) => ({
      ...prev,
      [jobId]: { ...(prev[jobId] || {}), query },
    }));
  };

  const searchArticles = async (job) => {
    const query = (articleSearchForms[job.id]?.query || "").trim();
    setErr("");
    setMsg("");
    setArticleSearchState((prev) => ({
      ...prev,
      [job.id]: {
        ...(prev[job.id] || {}),
        loading: true,
        error: "",
      },
    }));
    try {
      const data = await getBejermanArticles({ job_id: job.id, q: query });
      setArticleSearchState((prev) => ({
        ...prev,
        [job.id]: {
          loading: false,
          error: "",
          items: Array.isArray(data?.items) ? data.items : [],
          related_blocked_jobs: data?.related_blocked_jobs || 0,
        },
      }));
    } catch (e) {
      setArticleSearchState((prev) => ({
        ...prev,
        [job.id]: {
          ...(prev[job.id] || {}),
          loading: false,
          error: e?.message || "No se pudo buscar artículos Bejerman.",
        },
      }));
    }
  };

  const confirmMapping = async (job) => {
    const form = mappingForms[job.id] || {};
    const articleCode = (form.article_code || "").trim();
    if (!articleCode) {
      setErr("Ingrese o seleccione un código de artículo Bejerman.");
      return;
    }
    setErr("");
    setMsg("");
    const articleDescription = (
      form.article_description
      || form.selectedCandidate?.article_description
      || ""
    ).trim();
    try {
      const result = await postBejermanArticleMapping({
        job_id: job.id,
        article_code: articleCode,
        article_description: articleDescription,
      });
      setMappingForms((prev) => ({ ...prev, [job.id]: {} }));
      setArticleSearchState((prev) => ({ ...prev, [job.id]: {} }));
      setMsg(`Artículo Bejerman ${articleCode} aplicado a ${scopeText(job)}. Jobs reabiertos: ${Number(result?.reopened_jobs || 0)}.`);
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
      {msg && <div className="mt-3 rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{msg}</div>}

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
              const searchForm = articleSearchForms[row.id] || {};
              const searchState = articleSearchState[row.id] || {};
              const mapping = articleMappingOf(row);
              const mappingCode = trimValue(mapping?.article_code);
              const jobCode = trimValue(row.article_code);
              const articleCode = effectiveArticleCode(row);
              const articleDescription = effectiveArticleDescription(row);
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
                    <td className="p-2 min-w-[150px]">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="font-mono text-sm">{articleCode || "-"}</div>
                          {articleDescription && (
                            <div className="truncate text-xs text-gray-500" title={articleDescription}>{articleDescription}</div>
                          )}
                          {mappingCode && jobCode && mappingCode !== jobCode && (
                            <div className="text-[11px] text-amber-700">Job: <span className="font-mono">{jobCode}</span></div>
                          )}
                        </div>
                        {canManage && row.device_id && (
                          <button
                            type="button"
                            className="inline-flex h-7 shrink-0 items-center gap-1 rounded border border-gray-300 px-2 text-xs hover:bg-white"
                            onClick={() => openEditModal(row)}
                            title="Editar equipo y artículo Bejerman"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                            Editar
                          </button>
                        )}
                      </div>
                    </td>
                    <td className="p-2">{row.attempts ?? 0}</td>
                    <td className="p-2 min-w-[240px] max-w-md">
                      <div className="line-clamp-3 text-xs text-gray-700" title={jobMessage(row)}>{jobMessage(row)}</div>
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
                        <ArticleResolutionPanel
                          row={row}
                          canManage={canManage}
                          form={form}
                          searchForm={searchForm}
                          searchState={searchState}
                          onSelectCandidate={(candidate) => selectCandidate(row.id, candidate)}
                          onUpdateManual={(key, value) => updateMappingForm(row.id, key, value)}
                          onUpdateSearch={(query) => updateArticleSearch(row.id, query)}
                          onSearch={() => searchArticles(row)}
                          onConfirm={() => confirmMapping(row)}
                        />
                        <JobDiagnostic row={row} />
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

      <BejermanEditModal
        modal={editModal}
        onClose={() => setEditModal((prev) => (prev?.saving ? prev : null))}
        onChangeDevice={updateEditDevice}
        onChangeArticle={updateEditArticle}
        onChangeSearch={updateModalSearch}
        onSearch={searchModalArticles}
        onSelectCandidate={selectModalCandidate}
        onSave={saveEditModal}
      />
    </div>
  );
}
