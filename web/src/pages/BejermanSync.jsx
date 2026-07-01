import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileText,
  Loader2,
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
  getBejermanPdfOutputSettings,
  getBejermanRemitoProcesses,
  getDeviceEditable,
  patchDeviceEditable,
  postBejermanArticleMapping,
  postBejermanJobRetry,
  postBejermanRemitoProcessRetry,
  putBejermanPdfOutputSettings,
} from "../lib/api";
import { reservePdfWindow, waitForPdfBlob } from "../lib/pdf";
import { remitoDocumentLabel, remitoPdfUnavailableReason, remitoPrintableLabel } from "../lib/remitos";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { formatDateTime, formatOS } from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DesktopTableWrap, MobileDataCard, MobileDataField, MobileDataList } from "../components/Responsive.jsx";

const STATUS_LABELS = {
  pending: "En cola",
  running: "Procesando",
  succeeded: "Correcta",
  generated: "Generado",
  failed: "Fallida",
  blocked: "Bloqueada",
};

const STATUS_CLASS = {
  pending: "bg-slate-100 text-slate-700 border-slate-200",
  running: "bg-blue-50 text-blue-700 border-blue-200",
  succeeded: "bg-emerald-50 text-emerald-700 border-emerald-200",
  generated: "bg-emerald-50 text-emerald-700 border-emerald-200",
  failed: "bg-rose-50 text-rose-700 border-rose-200",
  blocked: "bg-amber-50 text-amber-800 border-amber-200",
};

const TYPE_LABELS = {
  stock_entry_str: "Ingreso STR bloqueado",
  stock_str_to_stl: "STR a STL",
  stock_str_to_stc: "STR a STC",
  stock_str_to_stcl: "STR a STCL",
  stock_to_desguace: "Baja a DES",
  stock_from_desguace: "Alta a STR",
};

const REMITO_SOURCE_LABELS = {
  ingreso: "Ingreso de servicio",
  orden_entrega: "Orden de entrega",
};

const PROCESS_TYPE_LABELS = {
  all: "Todos",
  remito: "Remitos",
  stock: "Stock",
};

const PROCESS_KIND_LABELS = {
  remito: "Remito",
  stock: "Stock",
};

const PROCESS_STATUSES = ["failed", "blocked", "running", "pending", "generated", "succeeded"];

const AUTO_REFRESH_MS = 5000;

function normalizeCompanyOption(item) {
  const key = String(item?.key || item?.companyKey || "").trim().toUpperCase();
  if (!key) return null;
  return {
    key,
    label: item?.label || item?.companyLabel || key,
    bejermanCompany: item?.bejermanCompany || "",
    isTest: !!item?.isTest,
  };
}

function pdfSettingsCompanies(data) {
  const fromCompanies = Array.isArray(data?.companies) ? data.companies : [];
  const fromItems = Array.isArray(data?.items) ? data.items : [];
  const byKey = new Map();
  [...fromCompanies, ...fromItems].forEach((item) => {
    const normalized = normalizeCompanyOption(item);
    if (normalized && !byKey.has(normalized.key)) byKey.set(normalized.key, normalized);
  });
  return Array.from(byKey.values());
}

function normalizePdfSettingsForm(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  return items.map((item) => ({
    companyKey: item.companyKey || "",
    companyLabel: item.companyLabel || item.companyKey || "",
    bejermanCompany: item.bejermanCompany || "",
    remitosDir: item.remitos?.outputDir || "",
    facturasDir: item.facturas?.outputDir || "",
  })).filter((item) => item.companyKey);
}

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

function processTypeValue(filters) {
  return filters?.process_type || "all";
}

function processDateMs(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function processAttemptedAt(row) {
  return row?.attempted_at || row?.updated_at || row?.generated_at || row?.created_at || "";
}

function processCreatedAt(row) {
  return row?.created_at || "";
}

function formatProcessDateTime(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" });
}

function stockApiFilters(filters) {
  return {
    status: filters.status,
    sync_type: filters.process_type === "stock" ? filters.sync_type : "",
    company_key: filters.company_key,
    q: filters.q,
    cliente: filters.cliente,
    articulo: filters.articulo,
  };
}

function remitoApiFilters(filters) {
  return {
    status: filters.status,
    source: filters.process_type === "remito" ? filters.source : "",
    company_key: filters.company_key,
    q: filters.q,
    cliente: filters.cliente,
  };
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

function operationLabel(row) {
  return TYPE_LABELS[row?.sync_type] || row?.sync_type || "-";
}

function companyLabel(row) {
  return row?.company_label || row?.company_key || "-";
}

function depositLabel(row) {
  const source = row?.source_deposit || "-";
  const target = row?.target_deposit || "-";
  return `${source} -> ${target}`;
}

function equipmentLabel(row) {
  return [row?.marca, row?.modelo, row?.variante].filter(Boolean).join(" ") || "-";
}

function ArticleSummary({ row, className = "" }) {
  const mapping = articleMappingOf(row);
  const mappingCode = trimValue(mapping?.article_code);
  const jobCode = trimValue(row?.article_code);
  const articleCode = effectiveArticleCode(row);
  const articleDescription = effectiveArticleDescription(row);

  return (
    <div className={`min-w-0 leading-tight ${className}`.trim()}>
      <div className="font-mono text-sm text-gray-900">{articleCode || "-"}</div>
      {articleDescription && (
        <div className="truncate text-xs text-gray-500" title={articleDescription}>
          {articleDescription}
        </div>
      )}
      {mappingCode && jobCode && mappingCode !== jobCode && (
        <div className="text-[11px] text-amber-700">
          Job: <span className="font-mono">{jobCode}</span>
        </div>
      )}
    </div>
  );
}

function JobActions({ row, canManage, onEdit, onRetry, className = "" }) {
  const canEdit = canManage && row?.device_id;
  const canRetry = canManage && (row?.status === "failed" || row?.status === "blocked");

  if (!canEdit && !canRetry) return null;

  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`.trim()}>
      {canEdit && (
        <button
          type="button"
          className="inline-flex h-7 shrink-0 items-center justify-center gap-1 rounded border border-gray-300 bg-white px-2 text-xs hover:bg-gray-50"
          onClick={() => onEdit(row)}
          title="Editar equipo y artículo Bejerman"
        >
          <Pencil className="h-3.5 w-3.5" />
          Editar
        </button>
      )}
      {canRetry && (
        <button
          type="button"
          className="inline-flex h-7 shrink-0 items-center justify-center gap-1 rounded border border-gray-300 bg-white px-2 text-xs hover:bg-gray-50"
          onClick={() => onRetry(row.id)}
        >
          <RotateCcw className="h-3.5 w-3.5" />
          Reintentar
        </button>
      )}
    </div>
  );
}

function remitoMessage(row) {
  if (row?.last_error) return row.last_error;
  if (row?.status === "generated") {
    return row?.document_mode === "register"
      ? "Remito registrado correctamente."
      : "Remito emitido correctamente.";
  }
  if (row?.status === "running") return "Proceso en ejecución.";
  if (row?.status === "pending") return "Proceso pendiente.";
  return "Sin errores.";
}

function remitoSourceLabel(row) {
  return row?.source_label || REMITO_SOURCE_LABELS[row?.source] || row?.source || "-";
}

function RemitoPdfAction({ row }) {
  const printUrl = row?.print_url || row?.printUrl;
  const pdfUrl = row?.pdf_url || row?.pdfUrl;
  const unavailableReason = remitoPdfUnavailableReason(row);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  if (!printUrl && !pdfUrl && !unavailableReason) return null;

  const documentLabel = remitoDocumentLabel(row);
  const printableLabel = remitoPrintableLabel(row);
  const fallbackUrl = printUrl || pdfUrl;

  const openPdf = async () => {
    if (loading || (!printUrl && !pdfUrl)) return;
    const reservedWindow = reservePdfWindow({
      title: "Remito Bejerman",
      message: `Preparando PDF del ${printableLabel}...`,
      fallbackMessage:
        "NEXORA sigue esperando el PDF de Bejerman. Si esta pestaña no cambia, puede cerrarla y reintentar desde Procesos.",
    });
    setLoading(true);
    setStatus("Preparando PDF...");
    setError("");
    try {
      if (pdfUrl) {
        const blob = await waitForPdfBlob(pdfUrl, {
          label: printableLabel,
          onProgress: (progress) => setStatus(progress?.status || "Preparando PDF..."),
        });
        const opened = reservedWindow.openPrintable(blob, {
          title: "Remito Bejerman",
          documentLabel: printableLabel,
        }).opened;
        if (!opened) {
          reservedWindow.close();
          setError("El PDF está listo, pero el navegador bloqueó la ventana automática. Use Abrir PDF.");
          return;
        }
        setStatus("");
        return;
      }
      const opened = reservedWindow.openUrl(printUrl);
      if (!opened) {
        reservedWindow.close();
        setError("El navegador bloqueó la ventana automática. Use Abrir PDF.");
        return;
      }
      setStatus("");
    } catch (err) {
      reservedWindow.close();
      setError(err?.message || "No se pudo abrir el PDF del remito.");
    } finally {
      setLoading(false);
    }
  };

  if (unavailableReason && !printUrl && !pdfUrl) {
    return (
      <span className="inline-flex h-7 shrink-0 items-center justify-center rounded border border-gray-200 bg-gray-50 px-2 text-xs text-gray-500" title={unavailableReason}>
        Sin PDF
      </span>
    );
  }

  return (
    <div className="flex min-w-0 flex-col items-start gap-1">
      <button
        type="button"
        className="inline-flex h-7 shrink-0 items-center justify-center gap-1 rounded border border-gray-300 bg-white px-2 text-xs hover:bg-gray-50 disabled:opacity-50"
        onClick={openPdf}
        disabled={loading}
      >
        {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileText className="h-3.5 w-3.5" />}
        {loading ? "Preparando" : "PDF"}
      </button>
      {status && <div className="max-w-[220px] text-[11px] leading-snug text-gray-500">{status}</div>}
      {error && (
        <div className="max-w-[240px] text-[11px] leading-snug text-red-700">
          {error}{" "}
          {fallbackUrl && (
            <a className="font-semibold underline" href={fallbackUrl} target="_blank" rel="noreferrer">
              Abrir PDF
            </a>
          )}
        </div>
      )}
    </div>
  );
}

function RemitoActions({ row, canManage, onRetry, className = "" }) {
  const canRetry = canManage && row?.retryable;
  const printUrl = row?.print_url || row?.printUrl;
  const pdfUrl = row?.pdf_url || row?.pdfUrl;
  const hasPdfState = Boolean(printUrl || pdfUrl || remitoPdfUnavailableReason(row));
  if (!canRetry && !hasPdfState) return null;
  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`.trim()}>
      {hasPdfState && <RemitoPdfAction row={row} />}
      {canRetry && (
        <button
          type="button"
          className="inline-flex h-7 shrink-0 items-center justify-center gap-1 rounded border border-gray-300 bg-white px-2 text-xs hover:bg-gray-50"
          onClick={() => onRetry(row)}
        >
          <RotateCcw className="h-3.5 w-3.5" />
          Reintentar
        </button>
      )}
    </div>
  );
}

function RemitoReference({ row }) {
  if (row?.source === "ingreso" && row?.ingreso_id) {
    return (
      <Link className="font-semibold text-blue-700 hover:underline" to={`/ingresos/${row.ingreso_id}`}>
        {formatOS(row.ingreso_id)}
      </Link>
    );
  }
  const orders = Array.isArray(row?.orders) ? row.orders : [];
  const orderLabel = orders.map((order) => order.orderNumber || order.id).filter(Boolean).slice(0, 3).join(", ");
  return (
    <Link className="font-semibold text-blue-700 hover:underline" to="/ordenes-entrega">
      {orderLabel || row?.display_id || row?.group_id || row?.id || "-"}
    </Link>
  );
}

function ExpandedRemitoDetail({ row }) {
  const orders = Array.isArray(row?.orders) ? row.orders : [];
  return (
    <>
      {orders.length > 0 && (
        <div className="mb-3">
          <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Órdenes</div>
          <div className="flex flex-wrap gap-2">
            {orders.map((order) => (
              <span key={order.id || order.orderNumber} className="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700">
                {order.orderNumber || order.id || "-"}
                {order.sourceReference ? ` · ${order.sourceReference}` : ""}
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="min-w-0">
          <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Request</div>
          <pre className="max-h-80 overflow-auto rounded border border-gray-200 bg-white p-3 text-xs">
            {payloadText(row.request_payload)}
          </pre>
        </div>
        <div className="min-w-0">
          <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Response</div>
          <pre className="max-h-80 overflow-auto rounded border border-gray-200 bg-white p-3 text-xs">
            {payloadText(row.response_payload)}
          </pre>
        </div>
      </div>
    </>
  );
}

function ExpandedJobDetail({
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
  return (
    <>
      <ArticleResolutionPanel
        row={row}
        canManage={canManage}
        form={form}
        searchForm={searchForm}
        searchState={searchState}
        onSelectCandidate={onSelectCandidate}
        onUpdateManual={onUpdateManual}
        onUpdateSearch={onUpdateSearch}
        onSearch={onSearch}
        onConfirm={onConfirm}
      />
      <JobDiagnostic row={row} />
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="min-w-0">
          <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Request</div>
          <pre className="max-h-80 overflow-auto rounded border border-gray-200 bg-white p-3 text-xs">
            {payloadText(row.request_payload)}
          </pre>
        </div>
        <div className="min-w-0">
          <div className="mb-1 text-xs font-semibold uppercase text-gray-500">Response</div>
          <pre className="max-h-80 overflow-auto rounded border border-gray-200 bg-white p-3 text-xs">
            {payloadText(row.response_payload)}
          </pre>
        </div>
      </div>
    </>
  );
}

function pdfSourceLabel(source) {
  if (source === "db") return "Configurado";
  if (source === "env") return "Env";
  return "Sin configurar";
}

function pdfFieldMeta(settings, companyKey, field) {
  const item = (settings?.items || []).find((row) => row.companyKey === companyKey);
  return item?.[field] || {};
}

function PdfOutputSettingsPanel({
  settings,
  form,
  selectedCompanyKey,
  canManage,
  loading,
  saving,
  error,
  message,
  onSelectCompany,
  onChange,
  onReload,
  onSave,
}) {
  const mounts = Array.isArray(settings?.windowsPathMounts) ? settings.windowsPathMounts : [];
  const companies = form.length ? form : pdfSettingsCompanies(settings);
  const selectedRow = form.find((row) => row.companyKey === selectedCompanyKey) || form[0] || null;
  const remitos = selectedRow ? pdfFieldMeta(settings, selectedRow.companyKey, "remitos") : {};
  const facturas = selectedRow ? pdfFieldMeta(settings, selectedRow.companyKey, "facturas") : {};

  return (
    <section className="mt-4 rounded border border-gray-200 bg-white p-3">
      <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-900">Ubicación de PDFs</h2>
          <div className="mt-1 flex flex-wrap gap-2 text-xs text-gray-500">
            {mounts.map((mount) => (
              <span
                key={mount.drive}
                className={`rounded border px-2 py-0.5 ${mount.mounted ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-amber-200 bg-amber-50 text-amber-800"}`}
              >
                {mount.drive}: {mount.mounted ? "montado" : "sin montar"}
              </span>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
            onClick={onReload}
            disabled={loading || saving}
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Actualizar
          </button>
          {canManage && (
            <button
              type="button"
              className="inline-flex h-9 items-center justify-center gap-2 rounded bg-gray-900 px-3 text-sm text-white hover:bg-gray-800 disabled:opacity-60"
              onClick={onSave}
              disabled={loading || saving || !selectedRow}
            >
              <Save className="h-4 w-4" />
              {saving ? "Guardando..." : "Guardar"}
            </button>
          )}
        </div>
      </div>

      {error && <div className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {message && <div className="mt-3 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{message}</div>}

      <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(220px,320px)_1fr]">
        <label className="block">
          <span className="text-xs font-medium text-gray-600">Empresa</span>
          <select
            className="mt-1 h-10 w-full rounded border border-gray-300 px-2 text-sm disabled:bg-gray-50"
            value={selectedRow?.companyKey || ""}
            onChange={(event) => onSelectCompany(event.target.value)}
            disabled={loading || saving || companies.length === 0}
          >
            {companies.length === 0 && <option value="">Sin empresas disponibles</option>}
            {companies.map((company) => (
              <option key={company.companyKey || company.key} value={company.companyKey || company.key}>
                {company.companyLabel || company.label || company.companyKey || company.key}
              </option>
            ))}
          </select>
          {selectedRow?.bejermanCompany && (
            <div className="mt-1 text-xs text-gray-500">Código Bejerman: {selectedRow.bejermanCompany}</div>
          )}
        </label>

        {selectedRow ? (
          <div className="grid gap-3 lg:grid-cols-2">
            <label className="block">
              <span className="text-xs font-medium text-gray-600">Remitos</span>
              <input
                className="mt-1 h-10 w-full rounded border border-gray-300 px-3 text-sm disabled:bg-gray-50"
                value={selectedRow.remitosDir}
                onChange={(event) => onChange(selectedRow.companyKey, "remitosDir", event.target.value)}
                placeholder="Z:\\..."
                disabled={!canManage || saving}
              />
              <PdfOutputStatus meta={remitos} />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-gray-600">Facturas</span>
              <input
                className="mt-1 h-10 w-full rounded border border-gray-300 px-3 text-sm disabled:bg-gray-50"
                value={selectedRow.facturasDir}
                onChange={(event) => onChange(selectedRow.companyKey, "facturasDir", event.target.value)}
                placeholder="Z:\\..."
                disabled={!canManage || saving}
              />
              <PdfOutputStatus meta={facturas} />
            </label>
          </div>
        ) : (
          <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            No hay empresas Bejerman disponibles para configurar.
          </div>
        )}
      </div>
    </section>
  );
}

function PdfOutputStatus({ meta }) {
  const validationError = meta?.validationError || "";
  if (validationError) {
    return <div className="mt-1 break-words text-xs text-red-700">{validationError}</div>;
  }
  const source = meta?.source || "none";
  const effectiveDir = meta?.effectiveDir || "";
  return (
    <div className="mt-1 break-words text-xs text-gray-500">
      {pdfSourceLabel(source)}
      {effectiveDir ? `: ${effectiveDir}` : ""}
    </div>
  );
}

export default function BejermanSync() {
  const { user } = useAuth();
  const canManage = can(user, PERMISSION_CODES.ACTION_BEJERMAN_SYNC_MANAGE);
  const [filters, setFilters] = useState({
    process_type: "all",
    status: "",
    sync_type: "",
    source: "",
    company_key: "",
    q: "",
    cliente: "",
    articulo: "",
  });
  const [rows, setRows] = useState([]);
  const [counters, setCounters] = useState({});
  const [remitoRows, setRemitoRows] = useState([]);
  const [remitoCounters, setRemitoCounters] = useState({});
  const [loading, setLoading] = useState(false);
  const [autoRefreshing, setAutoRefreshing] = useState(false);
  const [remitoLoading, setRemitoLoading] = useState(false);
  const [remitoAutoRefreshing, setRemitoAutoRefreshing] = useState(false);
  const [err, setErr] = useState("");
  const [remitoErr, setRemitoErr] = useState("");
  const [msg, setMsg] = useState("");
  const [expanded, setExpanded] = useState({});
  const [remitoExpanded, setRemitoExpanded] = useState({});
  const [mappingForms, setMappingForms] = useState({});
  const [articleSearchForms, setArticleSearchForms] = useState({});
  const [articleSearchState, setArticleSearchState] = useState({});
  const [editModal, setEditModal] = useState(null);
  const [pdfSettings, setPdfSettings] = useState(null);
  const [pdfSettingsForm, setPdfSettingsForm] = useState([]);
  const [selectedPdfCompanyKey, setSelectedPdfCompanyKey] = useState("");
  const [pdfSettingsLoading, setPdfSettingsLoading] = useState(false);
  const [pdfSettingsSaving, setPdfSettingsSaving] = useState(false);
  const [pdfSettingsError, setPdfSettingsError] = useState("");
  const [pdfSettingsMessage, setPdfSettingsMessage] = useState("");
  const filtersRef = useRef(filters);
  const rowsRef = useRef(rows);
  const remitoRowsRef = useRef(remitoRows);
  const editModalRef = useRef(editModal);
  const loadingRef = useRef(false);
  const remitoLoadingRef = useRef(false);
  const autoRefreshPausedRef = useRef(false);

  useEffect(() => {
    filtersRef.current = filters;
  }, [filters]);

  useEffect(() => {
    rowsRef.current = rows;
  }, [rows]);

  useEffect(() => {
    remitoRowsRef.current = remitoRows;
  }, [remitoRows]);

  useEffect(() => {
    editModalRef.current = editModal;
  }, [editModal]);

  const pauseAutoRefresh = useCallback(() => {
    autoRefreshPausedRef.current = true;
  }, []);

  const resumeAutoRefresh = useCallback(() => {
    autoRefreshPausedRef.current = false;
  }, []);

  const handleQueuePointerEnter = useCallback((event) => {
    if (event.pointerType !== "touch") pauseAutoRefresh();
  }, [pauseAutoRefresh]);

  const handleQueuePointerLeave = useCallback((event) => {
    if (event.pointerType !== "touch") resumeAutoRefresh();
  }, [resumeAutoRefresh]);

  const handleQueueBlur = useCallback((event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) {
      resumeAutoRefresh();
    }
  }, [resumeAutoRefresh]);

  const shouldSkipAutoRefresh = useCallback(() => (
    document.visibilityState === "hidden" ||
    autoRefreshPausedRef.current ||
    !!editModalRef.current
  ), []);

  const load = useCallback(async ({ silent = false } = {}) => {
    if (processTypeValue(filtersRef.current) === "remito") {
      setRows([]);
      setCounters({});
      setErr("");
      return;
    }
    if (loadingRef.current) return;
    loadingRef.current = true;
    if (silent) {
      setAutoRefreshing(true);
    } else {
      setLoading(true);
      setErr("");
    }
    try {
      const data = await getBejermanJobs(stockApiFilters(filtersRef.current));
      const nextRows = Array.isArray(data?.items) ? data.items : [];
      setRows((currentRows) => {
        if (!silent || currentRows.length === 0) return nextRows;
        const nextById = new Map(nextRows.map((row) => [row.id, row]));
        const keptRows = currentRows.map((row) => nextById.get(row.id)).filter(Boolean);
        const keptIds = new Set(keptRows.map((row) => row.id));
        const newRows = nextRows.filter((row) => !keptIds.has(row.id));
        return [...keptRows, ...newRows];
      });
      setCounters(data?.counters || {});
      setErr("");
    } catch (e) {
      if (!silent || rowsRef.current.length === 0) {
        setErr(e?.message || "No se pudo cargar la cola Bejerman.");
      }
    } finally {
      loadingRef.current = false;
      if (silent) {
        setAutoRefreshing(false);
      } else {
        setLoading(false);
      }
    }
  }, []);

  const loadRemitos = useCallback(async ({ silent = false } = {}) => {
    if (processTypeValue(filtersRef.current) === "stock") {
      setRemitoRows([]);
      setRemitoCounters({});
      setRemitoErr("");
      return;
    }
    if (remitoLoadingRef.current) return;
    remitoLoadingRef.current = true;
    if (silent) {
      setRemitoAutoRefreshing(true);
    } else {
      setRemitoLoading(true);
      setRemitoErr("");
    }
    try {
      const data = await getBejermanRemitoProcesses(remitoApiFilters(filtersRef.current));
      const nextRows = Array.isArray(data?.items) ? data.items : [];
      setRemitoRows((currentRows) => {
        if (!silent || currentRows.length === 0) return nextRows;
        const nextById = new Map(nextRows.map((row) => [row.process_id || `${row.source}:${row.id}`, row]));
        const keptRows = currentRows.map((row) => nextById.get(row.process_id || `${row.source}:${row.id}`)).filter(Boolean);
        const keptIds = new Set(keptRows.map((row) => row.process_id || `${row.source}:${row.id}`));
        const newRows = nextRows.filter((row) => !keptIds.has(row.process_id || `${row.source}:${row.id}`));
        return [...keptRows, ...newRows];
      });
      setRemitoCounters(data?.counters || {});
      setRemitoErr("");
    } catch (e) {
      if (!silent || remitoRowsRef.current.length === 0) {
        setRemitoErr(e?.message || "No se pudieron cargar los remitos Bejerman.");
      }
    } finally {
      remitoLoadingRef.current = false;
      if (silent) {
        setRemitoAutoRefreshing(false);
      } else {
        setRemitoLoading(false);
      }
    }
  }, []);

  const loadPdfSettings = useCallback(async () => {
    setPdfSettingsLoading(true);
    setPdfSettingsError("");
    try {
      const data = await getBejermanPdfOutputSettings();
      const nextForm = normalizePdfSettingsForm(data);
      setPdfSettings(data || null);
      setPdfSettingsForm(nextForm);
      setSelectedPdfCompanyKey((current) => (
        nextForm.some((row) => row.companyKey === current)
          ? current
          : (nextForm[0]?.companyKey || "")
      ));
      setPdfSettingsMessage("");
    } catch (e) {
      setPdfSettingsError(e?.message || "No se pudo cargar la ubicación de PDFs.");
    } finally {
      setPdfSettingsLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadRemitos();
  }, [loadRemitos]);

  useEffect(() => {
    loadPdfSettings();
  }, [loadPdfSettings]);

  useEffect(() => {
    const refresh = () => {
      if (shouldSkipAutoRefresh()) return;
      load({ silent: true });
      loadRemitos({ silent: true });
    };
    const intervalId = window.setInterval(refresh, AUTO_REFRESH_MS);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [load, loadRemitos, shouldSkipAutoRefresh]);

  const total = useMemo(
    () => Object.values(counters || {}).reduce((acc, value) => acc + Number(value || 0), 0),
    [counters],
  );
  const remitoTotal = useMemo(
    () => Object.values(remitoCounters || {}).reduce((acc, value) => acc + Number(value || 0), 0),
    [remitoCounters],
  );
  const processCounters = useMemo(() => {
    const type = processTypeValue(filters);
    const next = {};
    PROCESS_STATUSES.forEach((status) => {
      const stockCount = type === "remito" ? 0 : Number(counters?.[status] || 0);
      const remitoCount = type === "stock" ? 0 : Number(remitoCounters?.[status] || 0);
      next[status] = stockCount + remitoCount;
    });
    return next;
  }, [counters, filters, remitoCounters]);
  const processTotal = (
    (processTypeValue(filters) === "remito" ? 0 : total) +
    (processTypeValue(filters) === "stock" ? 0 : remitoTotal)
  );
  const processRows = useMemo(() => {
    const type = processTypeValue(filters);
    const stockRows = type === "remito"
      ? []
      : rows.map((row) => ({
          kind: "stock",
          key: `stock:${row.id}`,
          row,
          sortAt: processAttemptedAt(row),
        }));
    const remitoProcessRows = type === "stock"
      ? []
      : remitoRows.map((row) => ({
          kind: "remito",
          key: `remito:${row.process_id || `${row.source}:${row.id}`}`,
          row,
          sortAt: processAttemptedAt(row),
        }));
    return [...stockRows, ...remitoProcessRows].sort((a, b) => {
      const byDate = processDateMs(b.sortAt) - processDateMs(a.sortAt);
      if (byDate) return byDate;
      return String(b.key).localeCompare(String(a.key));
    });
  }, [filters, rows, remitoRows]);
  const processLoading = (
    (processTypeValue(filters) !== "remito" && loading) ||
    (processTypeValue(filters) !== "stock" && remitoLoading)
  );
  const refreshing = loading || autoRefreshing || remitoLoading || remitoAutoRefreshing;
  const companyOptions = useMemo(() => {
    const byKey = new Map();
    pdfSettingsCompanies(pdfSettings).forEach((company) => {
      byKey.set(company.key, company);
    });
    [...rows, ...remitoRows].forEach((row) => {
      const key = String(row?.company_key || "").trim().toUpperCase();
      if (key && !byKey.has(key)) {
        byKey.set(key, {
          key,
          label: row?.company_label || key,
          bejermanCompany: "",
          isTest: false,
        });
      }
    });
    return Array.from(byKey.values());
  }, [pdfSettings, rows, remitoRows]);

  const updateFilter = (key, value) => {
    setFilters((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "process_type") {
        if (value !== "stock") next.sync_type = "";
        if (value !== "remito") next.source = "";
        if (value === "remito") next.articulo = "";
      }
      return next;
    });
  };

  const updatePdfSetting = (companyKey, key, value) => {
    setPdfSettingsForm((prev) => prev.map((row) => (
      row.companyKey === companyKey ? { ...row, [key]: value } : row
    )));
    setPdfSettingsMessage("");
  };

  const savePdfSettings = async () => {
    if (!canManage || pdfSettingsSaving) return;
    setPdfSettingsSaving(true);
    setPdfSettingsError("");
    setPdfSettingsMessage("");
    try {
      const data = await putBejermanPdfOutputSettings({
        items: pdfSettingsForm.map((row) => ({
          companyKey: row.companyKey,
          remitosDir: row.remitosDir,
          facturasDir: row.facturasDir,
        })),
      });
      setPdfSettings(data || null);
      setPdfSettingsForm(normalizePdfSettingsForm(data));
      setPdfSettingsMessage("Ubicación de PDFs guardada.");
    } catch (e) {
      setPdfSettingsError(e?.message || "No se pudo guardar la ubicación de PDFs.");
    } finally {
      setPdfSettingsSaving(false);
    }
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

  const retryRemito = async (row) => {
    if (!row?.source || !row?.id) return;
    setRemitoErr("");
    setMsg("");
    try {
      await postBejermanRemitoProcessRetry(row.source, row.id);
      setMsg(`Proceso de remito ${remitoDocumentLabel(row)} reintentado.`);
      await loadRemitos();
    } catch (e) {
      setRemitoErr(e?.message || "No se pudo reintentar el remito.");
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

  const renderExpandedDetail = (row) => {
    const form = mappingForms[row.id] || {};
    const searchForm = articleSearchForms[row.id] || {};
    const searchState = articleSearchState[row.id] || {};

    return (
      <ExpandedJobDetail
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
    );
  };

  const toggleProcessExpanded = (kind, key, isOpen) => {
    if (kind === "remito") {
      setRemitoExpanded((prev) => ({ ...prev, [key]: !isOpen }));
      return;
    }
    setExpanded((prev) => ({ ...prev, [key]: !isOpen }));
  };

  const renderProcessMobileCard = (process) => {
    const { kind, key, row } = process;
    const isRemito = kind === "remito";
    const openKey = isRemito ? key : row.id;
    const isOpen = isRemito ? !!remitoExpanded[openKey] : !!expanded[openKey];
    const message = isRemito ? remitoMessage(row) : jobMessage(row);
    const attemptedAt = processAttemptedAt(row);
    const createdAt = processCreatedAt(row);

    return (
      <MobileDataCard key={key} className="space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={row.status} />
              <span className="text-xs font-medium text-gray-600">{PROCESS_KIND_LABELS[kind]}</span>
              <span className="text-xs text-gray-500">{isRemito ? remitoSourceLabel(row) : operationLabel(row)}</span>
            </div>
            {isRemito ? (
              <>
                <div className="mt-2 font-semibold text-gray-900">{remitoDocumentLabel(row)}</div>
                <div className="mt-1"><RemitoReference row={row} /></div>
                <div className="text-xs text-gray-500">{row.customer_name || row.cliente || "-"}</div>
              </>
            ) : (
              <>
                <Link className="mt-2 inline-flex font-semibold text-blue-700 hover:underline" to={`/ingresos/${row.ingreso_id}`}>
                  {formatOS(row.ingreso_id)}
                </Link>
                <div className="mt-1 font-medium text-gray-900">{equipmentLabel(row)}</div>
                <div className="text-xs text-gray-500">{row.cliente || "-"}</div>
              </>
            )}
          </div>
          <button
            type="button"
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded border border-gray-200 bg-white hover:bg-gray-50"
            onClick={() => toggleProcessExpanded(kind, openKey, isOpen)}
            aria-label={isOpen ? "Ocultar detalle" : "Ver detalle"}
          >
            {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </button>
        </div>

        <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
          <MobileDataField label="Empresa" value={companyLabel(row)} />
          <MobileDataField label={isRemito ? "Origen" : "Operación"} value={isRemito ? remitoSourceLabel(row) : operationLabel(row)} />
          <MobileDataField label="Intentos" value={row.attempts ?? 0} />
          <MobileDataField label="Creado" value={formatProcessDateTime(createdAt)} />
          <MobileDataField label="Último intento" value={formatProcessDateTime(attemptedAt)} />
          {isRemito ? (
            <>
              <MobileDataField label="Equipo" value={row.equipment_label || "-"} />
              <MobileDataField label="Serie" value={row.numero_serie || "-"} />
            </>
          ) : (
            <>
              <MobileDataField label="Depósitos" value={depositLabel(row)} />
              <MobileDataField label="Serie">
                <DeviceIdentifier row={row} />
              </MobileDataField>
              <MobileDataField label="Artículo">
                <ArticleSummary row={row} />
              </MobileDataField>
            </>
          )}
          <MobileDataField label="Último mensaje" className="min-[420px]:col-span-2" valueClassName="text-xs leading-relaxed text-gray-700">
            {message}
          </MobileDataField>
        </div>

        {isRemito ? (
          <RemitoActions row={row} canManage={canManage} onRetry={retryRemito} />
        ) : (
          <JobActions row={row} canManage={canManage} onEdit={openEditModal} onRetry={retry} />
        )}

        {isOpen && (
          <div className="rounded border border-gray-200 bg-gray-50 p-3">
            {isRemito ? <ExpandedRemitoDetail row={row} /> : renderExpandedDetail(row)}
          </div>
        )}
      </MobileDataCard>
    );
  };

  const renderProcessDesktopRow = (process) => {
    const { kind, key, row } = process;
    const isRemito = kind === "remito";
    const openKey = isRemito ? key : row.id;
    const isOpen = isRemito ? !!remitoExpanded[openKey] : !!expanded[openKey];
    const message = isRemito ? remitoMessage(row) : jobMessage(row);
    const attemptedAt = processAttemptedAt(row);
    const createdAt = processCreatedAt(row);

    return (
      <Fragment key={key}>
        <tr className="border-t border-gray-100 align-top hover:bg-gray-50">
          <td className="px-2 py-2">
            <button
              type="button"
              className="inline-flex h-7 w-7 items-center justify-center rounded border border-gray-200 bg-white hover:bg-gray-50"
              onClick={() => toggleProcessExpanded(kind, openKey, isOpen)}
              aria-label={isOpen ? "Ocultar detalle" : "Ver detalle"}
            >
              {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            </button>
          </td>
          <td className="px-3 py-2">
            <StatusBadge status={row.status} />
            <div className="mt-1.5 font-medium leading-tight text-gray-900">{PROCESS_KIND_LABELS[kind]}</div>
            <div className="mt-1 text-xs text-gray-500">{companyLabel(row)}</div>
            <div className="text-xs leading-tight text-gray-500">
              {isRemito ? remitoSourceLabel(row) : `${operationLabel(row)} · ${depositLabel(row)}`}
            </div>
          </td>
          <td className="px-3 py-2">
            {isRemito ? (
              <>
                <div className="break-words font-semibold leading-tight text-gray-900">{remitoDocumentLabel(row)}</div>
                <div className="mt-1 text-xs text-gray-500">{row.operation_label || "-"}</div>
                <div className="text-xs leading-tight text-gray-500">
                  {[row.comprobante_tipo, row.comprobante_pto_venta, row.comprobante_numero].map(trimValue).filter(Boolean).join(" · ")}
                </div>
              </>
            ) : (
              <>
                <Link className="font-semibold text-blue-700 hover:underline" to={`/ingresos/${row.ingreso_id}`}>
                  {formatOS(row.ingreso_id)}
                </Link>
                <div className="mt-1 break-words font-medium leading-tight text-gray-900">{equipmentLabel(row)}</div>
                <div className="text-xs leading-tight text-gray-500">{row.cliente || "-"}</div>
              </>
            )}
          </td>
          <td className="px-3 py-2">
            {isRemito ? (
              <>
                <RemitoReference row={row} />
                <div className="mt-1 break-words font-medium leading-tight text-gray-900">{row.equipment_label || "-"}</div>
                <div className="text-xs leading-tight text-gray-500">{row.customer_name || row.cliente || "-"}</div>
              </>
            ) : (
              <div className="min-w-0">
                <div className="text-[10px] font-semibold uppercase leading-tight text-gray-500">Serie</div>
                <div className="break-all leading-tight">
                  <DeviceIdentifier row={row} />
                </div>
                <div className="mt-2 text-[10px] font-semibold uppercase leading-tight text-gray-500">Artículo</div>
                <ArticleSummary row={row} />
              </div>
            )}
          </td>
          <td className="px-3 py-2">
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-500">
              <span>Intentos: {row.attempts ?? 0}</span>
              <span>Creado: {formatProcessDateTime(createdAt)}</span>
              <span>Último intento: {formatProcessDateTime(attemptedAt)}</span>
            </div>
            <div className="mt-1.5 line-clamp-3 text-xs leading-snug text-gray-700" title={message}>
              {message}
            </div>
          </td>
          <td className="px-3 py-2">
            {isRemito ? (
              <RemitoActions row={row} canManage={canManage} onRetry={retryRemito} className="justify-end" />
            ) : (
              <JobActions row={row} canManage={canManage} onEdit={openEditModal} onRetry={retry} className="justify-end" />
            )}
          </td>
        </tr>
        {isOpen && (
          <tr className="border-t border-gray-100 bg-gray-50/60">
            <td colSpan={6} className="p-3">
              {isRemito ? <ExpandedRemitoDetail row={row} /> : renderExpandedDetail(row)}
            </td>
          </tr>
        )}
      </Fragment>
    );
  };

  return (
    <div className="mx-auto w-full max-w-screen-2xl p-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="text-2xl font-bold">Bejerman</h1>
          <p className="text-sm text-gray-600">Remitos, operaciones de stock y cola de sincronización.</p>
        </div>
        <button
          type="button"
          className="inline-flex h-10 items-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
          onClick={() => {
            load();
            loadRemitos();
          }}
          disabled={loading || remitoLoading}
        >
          <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          Actualizar
        </button>
      </div>

      <PdfOutputSettingsPanel
        settings={pdfSettings}
        form={pdfSettingsForm}
        selectedCompanyKey={selectedPdfCompanyKey}
        canManage={canManage}
        loading={pdfSettingsLoading}
        saving={pdfSettingsSaving}
        error={pdfSettingsError}
        message={pdfSettingsMessage}
        onSelectCompany={setSelectedPdfCompanyKey}
        onChange={updatePdfSetting}
        onReload={loadPdfSettings}
        onSave={savePdfSettings}
      />

      <section className="mt-4">
        <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Procesos en Bejerman</h2>
            <p className="text-sm text-gray-600">Remitos y movimientos de stock enviados a Bejerman.</p>
          </div>
          <button
            type="button"
            className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
            onClick={() => {
              load();
              loadRemitos();
            }}
            disabled={processLoading}
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            Actualizar procesos
          </button>
        </div>

        <div className="mt-3 grid gap-2 sm:grid-cols-3 lg:grid-cols-7">
          {PROCESS_STATUSES.map((status) => (
            <button
              key={status}
              type="button"
              onClick={() => updateFilter("status", filters.status === status ? "" : status)}
              className={`rounded border px-3 py-2 text-left text-sm ${filters.status === status ? "border-gray-900 bg-gray-50" : "border-gray-200 bg-white hover:bg-gray-50"}`}
            >
              <div className="text-xs text-gray-500">{STATUS_LABELS[status]}</div>
              <div className="text-xl font-semibold">{Number(processCounters?.[status] || 0)}</div>
            </button>
          ))}
          <div className="rounded border border-gray-200 px-3 py-2 text-sm">
            <div className="text-xs text-gray-500">Total filtrado</div>
            <div className="text-xl font-semibold">{processTotal}</div>
          </div>
        </div>

        <div className="mt-3 rounded border border-gray-200 bg-white p-3">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <select
              className="h-10 min-w-0 rounded border border-gray-300 px-2 text-sm"
              value={filters.process_type}
              onChange={(e) => updateFilter("process_type", e.target.value)}
            >
              {Object.entries(PROCESS_TYPE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <select
              className="h-10 min-w-0 rounded border border-gray-300 px-2 text-sm"
              value={filters.status}
              onChange={(e) => updateFilter("status", e.target.value)}
            >
              <option value="">Estados</option>
              {PROCESS_STATUSES.map((status) => (
                <option key={status} value={status}>{STATUS_LABELS[status]}</option>
              ))}
            </select>
            {filters.process_type === "remito" ? (
              <select
                className="h-10 min-w-0 rounded border border-gray-300 px-2 text-sm"
                value={filters.source}
                onChange={(e) => updateFilter("source", e.target.value)}
              >
                <option value="">Orígenes de remito</option>
                {Object.entries(REMITO_SOURCE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            ) : filters.process_type === "stock" ? (
              <select
                className="h-10 min-w-0 rounded border border-gray-300 px-2 text-sm"
                value={filters.sync_type}
                onChange={(e) => updateFilter("sync_type", e.target.value)}
              >
                <option value="">Operaciones de stock</option>
                {Object.entries(TYPE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            ) : (
              <select className="h-10 min-w-0 rounded border border-gray-300 px-2 text-sm text-gray-500" value="" disabled>
                <option value="">Subtipo</option>
              </select>
            )}
            <select
              className="h-10 min-w-0 rounded border border-gray-300 px-2 text-sm"
              value={filters.company_key}
              onChange={(e) => updateFilter("company_key", e.target.value)}
            >
              <option value="">Empresas</option>
              {companyOptions.map((company) => (
                <option key={company.key} value={company.key}>{company.label}</option>
              ))}
            </select>
          </div>
          <div className="mt-2 grid gap-2 lg:grid-cols-[minmax(260px,1fr)_minmax(150px,200px)_minmax(150px,180px)_auto]">
            <input
              className="h-10 min-w-0 rounded border border-gray-300 px-3 text-sm"
              value={filters.q}
              onChange={(e) => updateFilter("q", e.target.value)}
              placeholder="Buscar por OS, remito, orden, serie, cliente o error"
            />
            <input
              className="h-10 min-w-0 rounded border border-gray-300 px-3 text-sm"
              value={filters.cliente}
              onChange={(e) => updateFilter("cliente", e.target.value)}
              placeholder="Cliente"
            />
            <input
              className="h-10 min-w-0 rounded border border-gray-300 px-3 text-sm disabled:bg-gray-50"
              value={filters.articulo}
              onChange={(e) => updateFilter("articulo", e.target.value)}
              placeholder="Artículo"
              disabled={filters.process_type === "remito"}
            />
            <button
              type="button"
              className="inline-flex h-10 w-full items-center justify-center gap-2 rounded bg-gray-900 px-4 text-sm text-white hover:bg-gray-800 disabled:opacity-60 lg:w-auto"
              onClick={() => {
                load();
                loadRemitos();
              }}
              disabled={processLoading}
            >
              <Search className="h-4 w-4" />
              Filtrar
            </button>
          </div>
        </div>

        {filters.process_type !== "remito" && err && <div className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{err}</div>}
        {filters.process_type !== "stock" && remitoErr && <div className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{remitoErr}</div>}
        {msg && <div className="mt-3 rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{msg}</div>}

        <div
          onPointerEnter={handleQueuePointerEnter}
          onPointerLeave={handleQueuePointerLeave}
          onFocusCapture={pauseAutoRefresh}
          onBlurCapture={handleQueueBlur}
        >
          <MobileDataList className="mt-4">
            {processLoading && processRows.length === 0 && (
              <MobileDataCard className="text-center text-gray-500">Cargando procesos...</MobileDataCard>
            )}
            {!processLoading && processRows.length === 0 && (
              <MobileDataCard className="text-center text-gray-500">Sin procesos.</MobileDataCard>
            )}
            {processRows.map(renderProcessMobileCard)}
          </MobileDataList>

          <DesktopTableWrap className="mt-4 rounded border border-gray-200 bg-white">
            <table className="w-full table-fixed text-sm">
              <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
                <tr>
                  <th className="w-10 px-2 py-2"></th>
                  <th className="w-[170px] px-3 py-2">Estado / Tipo</th>
                  <th className="w-[24%] px-3 py-2">Documento / OS</th>
                  <th className="w-[26%] px-3 py-2">Referencia</th>
                  <th className="px-3 py-2">Seguimiento</th>
                  <th className="w-[130px] px-3 py-2 text-right">Acciones</th>
                </tr>
              </thead>
              <tbody>
                {processLoading && processRows.length === 0 && (
                  <tr>
                    <td colSpan={6} className="p-4 text-center text-gray-500">Cargando procesos...</td>
                  </tr>
                )}
                {!processLoading && processRows.length === 0 && (
                  <tr>
                    <td colSpan={6} className="p-4 text-center text-gray-500">Sin procesos.</td>
                  </tr>
                )}
                {processRows.map(renderProcessDesktopRow)}
              </tbody>
            </table>
          </DesktopTableWrap>
        </div>
      </section>


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
