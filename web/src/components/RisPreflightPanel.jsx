import React, { useState } from "react";

import { getBejermanArticles } from "@/lib/api";

const severityClass = (severity) => {
  if (severity === "warning") return "border-amber-200 bg-amber-50 text-amber-900";
  return "border-red-200 bg-red-50 text-red-900";
};

const issueScopeLabel = (issue) => {
  const parts = [];
  if (Number.isInteger(issue?.item_index)) parts.push(`Equipo #${issue.item_index + 1}`);
  if (issue?.scope) parts.push(issue.scope);
  if (issue?.field) parts.push(issue.field);
  return parts.join(" · ");
};

const candidateLabel = (candidate) =>
  candidate?.name ||
  candidate?.customer_name ||
  candidate?.article_description ||
  candidate?.description ||
  candidate?.article_code ||
  candidate?.code ||
  "Candidato";

function CustomerCandidate({ issue, candidate, onApplyCustomer }) {
  const customerId = issue?.fix?.customer_id || issue?.fix?.customerId;
  const code = candidate?.code || candidate?.customer_code || "";
  const canApply = issue?.fix?.type === "customer" && customerId && code && onApplyCustomer;
  return (
    <div className="rounded border border-gray-200 bg-white p-2">
      <div className="text-sm font-semibold text-gray-900">
        {candidateLabel(candidate)}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-600">
        {code && <span>Código: {code}</span>}
        {candidate?.cuit && <span>CUIT: {candidate.cuit}</span>}
        {candidate?.iva && <span>IVA: {candidate.iva}</span>}
        {candidate?.province && <span>Provincia: {candidate.province}</span>}
      </div>
      {canApply && (
        <button
          type="button"
          className="mt-2 rounded border border-blue-600 px-3 py-1.5 text-xs font-semibold text-blue-700 hover:bg-blue-50"
          onClick={() =>
            onApplyCustomer({
              customer_id: customerId,
              customer_code: code,
              company_key: issue?.fix?.company_key || issue?.fix?.companyKey || "",
            })
          }
        >
          Usar cliente de Bejerman
        </button>
      )}
    </div>
  );
}

function ArticleCandidate({ issue, candidate, onApplyArticle }) {
  const fix = issue?.fix || {};
  const code = candidate?.article_code || candidate?.code || fix.article_code || "";
  const canApply =
    fix.type === "article_mapping" &&
    fix.model_id &&
    code &&
    (!fix.article_code || fix.article_code === code) &&
    onApplyArticle;
  return (
    <div className="rounded border border-gray-200 bg-white p-2">
      <div className="text-sm font-semibold text-gray-900">
        {candidateLabel(candidate)}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-600">
        {code && <span>Artículo: {code}</span>}
        {candidate?.deposit && <span>Depósito: {candidate.deposit}</span>}
        {candidate?.partida && <span>Partida: {candidate.partida}</span>}
      </div>
      {canApply && (
        <button
          type="button"
          className="mt-2 rounded border border-blue-600 px-3 py-1.5 text-xs font-semibold text-blue-700 hover:bg-blue-50"
          onClick={() =>
            onApplyArticle({
              model_id: fix.model_id,
              variante: fix.variante || "",
              article_code: code,
              article_description: candidate?.article_description || fix.article_description || "",
            })
          }
        >
          Aplicar artículo
        </button>
      )}
    </div>
  );
}

function ArticleSearch({ issue, onApplyArticle }) {
  const fix = issue?.fix || {};
  const modelId = fix.model_id || fix.modelId;
  const [query, setQuery] = useState("");
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  if (!modelId || !onApplyArticle) return null;

  const search = async () => {
    setLoading(true);
    setError("");
    try {
      const result = await getBejermanArticles({
        model_id: modelId,
        variante: fix.variante || fix.variant || "",
        q: query,
        limit: 20,
      });
      setItems(Array.isArray(result?.items) ? result.items : []);
    } catch (err) {
      setItems([]);
      setError(err?.data?.detail || err?.message || "No se pudieron buscar artículos Bejerman.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mt-3 rounded border border-gray-200 bg-white p-2">
      <div className="grid gap-2 md:grid-cols-[1fr_auto]">
        <input
          className="h-9 rounded border border-gray-300 px-2 text-sm"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              search();
            }
          }}
          placeholder="Código o descripción Bejerman"
        />
        <button
          type="button"
          className="h-9 rounded border border-gray-300 px-3 text-sm font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-60"
          disabled={loading}
          onClick={search}
        >
          {loading ? "Buscando..." : "Buscar"}
        </button>
      </div>
      {error && <div className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-800">{error}</div>}
      {items.length > 0 && (
        <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
          {items.map((candidate, index) => (
            <ArticleCandidate
              key={`${candidate?.article_code || candidate?.code || index}`}
              issue={issue}
              candidate={candidate}
              onApplyArticle={onApplyArticle}
            />
          ))}
        </div>
      )}
      {!loading && !error && items.length === 0 && query.trim() && (
        <div className="mt-2 text-xs text-gray-600">Sin resultados para esa búsqueda.</div>
      )}
    </div>
  );
}

function IssueRow({ issue, onApplyCustomer, onApplyArticle, onEditItem }) {
  const candidates = Array.isArray(issue?.candidates) ? issue.candidates : [];
  const isCustomer = issue?.fix?.type === "customer" || issue?.scope === "cliente";
  const isArticle = issue?.fix?.type === "article_mapping" || issue?.scope === "articulo";
  return (
    <div className={`rounded border p-3 ${severityClass(issue?.severity)}`}>
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="text-sm font-semibold">{issue?.message || "Problema de validación RIS"}</div>
          <div className="mt-1 text-xs opacity-80">
            {issueScopeLabel(issue) || issue?.code || "RIS"}
          </div>
        </div>
        {Number.isInteger(issue?.item_index) && onEditItem && (
          <button
            type="button"
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-700 hover:bg-gray-50"
            onClick={() => onEditItem(issue.item_index)}
          >
            Editar equipo
          </button>
        )}
      </div>
      {candidates.length > 0 && (
        <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">
          {candidates.map((candidate, index) =>
            isCustomer ? (
              <CustomerCandidate
                key={`${candidate?.code || candidate?.customer_code || index}`}
                issue={issue}
                candidate={candidate}
                onApplyCustomer={onApplyCustomer}
              />
            ) : isArticle ? (
              <ArticleCandidate
                key={`${candidate?.article_code || candidate?.code || index}`}
                issue={issue}
                candidate={candidate}
                onApplyArticle={onApplyArticle}
              />
            ) : (
              <div key={index} className="rounded border border-gray-200 bg-white p-2 text-sm text-gray-700">
                {candidateLabel(candidate)}
              </div>
            )
          )}
        </div>
      )}
      {isArticle && !issue?.fix?.article_code && candidates.length !== 1 && (
        <div className="mt-2 text-xs font-semibold text-red-800">
          No hay un artículo único para aplicar. Resuelva el mapeo del modelo/variante.
        </div>
      )}
      {isArticle && <ArticleSearch issue={issue} onApplyArticle={onApplyArticle} />}
    </div>
  );
}

export default function RisPreflightPanel({
  result,
  loading = false,
  error = "",
  onValidate,
  onApplyCustomer,
  onApplyArticle,
  onEditItem,
  disabled = false,
  documentLabel = "RIS",
  actionLabel = "Emitir RIS",
}) {
  const issues = Array.isArray(result?.issues) ? result.issues : [];
  const preview = result?.preview || {};
  const hasResult = !!result;
  const normalizedDocumentLabel = documentLabel || "RIS";
  const titleDocumentLabel = normalizedDocumentLabel === "remito" ? "del remito" : normalizedDocumentLabel;
  const actionText = actionLabel === "Emitir RIS" ? "emitir RIS" : actionLabel.toLowerCase();
  return (
    <section className="rounded border border-gray-200 bg-white p-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">Validación {titleDocumentLabel}</h2>
          <div className="mt-1 text-xs text-gray-600">
            {hasResult
              ? result?.can_emit
                ? `${normalizedDocumentLabel} listo para ${actionText} · ${preview.lineCount || 0} ${Number(preview.lineCount) === 1 ? "línea" : "líneas"}`
                : result?.detail || "La validación encontró problemas."
              : `Valide los datos antes de crear o ${actionText}.`}
          </div>
        </div>
        {onValidate && (
          <button
            type="button"
            disabled={loading || disabled}
            className={`rounded px-3 py-2 text-sm font-semibold text-white ${
              loading || disabled ? "bg-gray-400 cursor-not-allowed" : "bg-sky-700 hover:bg-sky-800"
            }`}
            onClick={onValidate}
          >
            {loading ? "Validando..." : `Validar ${normalizedDocumentLabel}`}
          </button>
        )}
      </div>

      {error && <div className="mt-3 rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">{error}</div>}

      {hasResult && result?.can_emit && (
        <div className="mt-3 rounded border border-emerald-200 bg-emerald-50 p-2 text-sm font-semibold text-emerald-800">
          La validación del {normalizedDocumentLabel} es válida.
        </div>
      )}

      {issues.length > 0 && (
        <div className="mt-3 space-y-2">
          {issues.map((issue, index) => (
            <IssueRow
              key={`${issue?.code || "issue"}-${issue?.item_index ?? "single"}-${index}`}
              issue={issue}
              onApplyCustomer={onApplyCustomer}
              onApplyArticle={onApplyArticle}
              onEditItem={onEditItem}
            />
          ))}
        </div>
      )}
    </section>
  );
}
