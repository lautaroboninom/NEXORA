import React, { useState } from "react";

import {
  getBejermanArticles,
  postIngresoConflictoEvidencia,
  postIngresoConflictoResolver,
} from "@/lib/api";

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
              company_key: fix.company_key || fix.companyKey || "",
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
        company_key: fix.company_key || fix.companyKey || "",
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

/**
 * El equipo ya tiene una orden de servicio abierta.
 *
 * Casi siempre es que no se tildó la entrega anterior, no que el equipo esté dos veces en el
 * taller. Por eso esto no frena el remito: informa, y ofrece el camino corto para arreglarlo
 * —preguntarle a Bejerman si el equipo salió— sin obligar a nadie a salir de la pantalla con
 * el cliente esperando.
 */
function OpenIngresoConflict({ issue, onResolved }) {
  const fix = issue?.fix || {};
  const [evidence, setEvidence] = useState(null);
  const [loading, setLoading] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState("");
  const [resolved, setResolved] = useState("");

  const payload = {
    conflicting_ingreso_id: fix.conflicting_ingreso_id,
    numero_serie: fix.numero_serie || "",
    company_key: fix.company_key || "",
  };

  const buscar = async () => {
    setLoading(true);
    setError("");
    try {
      setEvidence(await postIngresoConflictoEvidencia(payload));
    } catch (err) {
      setEvidence(null);
      setError(err?.data?.detail || err?.message || "No se pudo consultar Bejerman.");
    } finally {
      setLoading(false);
    }
  };

  const cerrar = async () => {
    setResolving(true);
    setError("");
    try {
      const result = await postIngresoConflictoResolver(payload);
      setResolved(result?.detail || "La orden anterior quedó cerrada.");
      onResolved?.();
    } catch (err) {
      setError(err?.data?.detail || err?.message || "No se pudo cerrar la orden anterior.");
    } finally {
      setResolving(false);
    }
  };

  if (!fix.conflicting_ingreso_id) return null;

  const documents = Array.isArray(evidence?.documents) ? evidence.documents : [];
  const found = evidence?.status === "found" && documents.length > 0;

  return (
    <div className="mt-3 rounded border border-amber-300 bg-white p-2">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-700">
        <span>
          Orden anterior: <b>OS {fix.conflicting_os || fix.conflicting_ingreso_id}</b>
        </span>
        {fix.conflicting_fecha_ingreso && <span>Ingresada el {fix.conflicting_fecha_ingreso}</span>}
        {fix.conflicting_estado && <span>Estado: {fix.conflicting_estado}</span>}
        {fix.conflicting_cliente && <span>Cliente: {fix.conflicting_cliente}</span>}
      </div>

      {resolved ? (
        <div className="mt-2 rounded border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs font-semibold text-emerald-800">
          {resolved} Ya puede emitir el remito con el número de OS nuevo.
        </div>
      ) : (
        <>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-60"
              disabled={loading || resolving}
              onClick={buscar}
            >
              {loading ? "Consultando Bejerman..." : "Buscar la salida en Bejerman"}
            </button>
            {found && (
              <button
                type="button"
                className="rounded border border-emerald-600 bg-white px-3 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 disabled:opacity-60"
                disabled={resolving}
                onClick={cerrar}
              >
                {resolving ? "Cerrando..." : `Cerrar OS ${fix.conflicting_os || ""} con ese remito`}
              </button>
            )}
          </div>

          {found && (
            <div className="mt-2 space-y-1">
              <div className="text-xs font-semibold text-emerald-800">
                Bejerman registra la salida del equipo después de ese ingreso:
              </div>
              {documents.slice(0, 3).map((document, index) => (
                <div key={`${document.remitoNumber || index}`} className="text-xs text-gray-700">
                  {document.remitoNumber} · {document.issueDate}
                  {document.description ? ` · ${document.description}` : ""}
                </div>
              ))}
            </div>
          )}

          {evidence && !found && (
            <div className="mt-2 text-xs text-gray-700">
              {evidence.reason ||
                "Bejerman no muestra ningún remito de salida posterior. El aviso le llega a jefatura para que lo resuelva."}
            </div>
          )}

          {!evidence && !loading && (
            <div className="mt-2 text-xs text-gray-600">
              Puede emitir el remito igual: ese renglón sale sin número de OS y jefatura recibe el aviso.
            </div>
          )}
        </>
      )}

      {error && <div className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-800">{error}</div>}
    </div>
  );
}

function IssueRow({ issue, onApplyCustomer, onApplyArticle, onEditItem, onConflictResolved }) {
  const candidates = Array.isArray(issue?.candidates) ? issue.candidates : [];
  const isConflict = issue?.fix?.type === "open_ingreso_conflict";
  const isCustomer = !isConflict && (issue?.fix?.type === "customer" || issue?.scope === "cliente");
  const isArticle = !isConflict && (issue?.fix?.type === "article_mapping" || issue?.scope === "articulo");
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
      {isConflict && <OpenIngresoConflict issue={issue} onResolved={onConflictResolved} />}
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
  onConflictResolved,
  disabled = false,
  documentLabel = "RIS",
  actionLabel = "Emitir RIS",
}) {
  const issues = Array.isArray(result?.issues) ? result.issues : [];
  const preview = result?.preview || {};
  const profile = preview?.documentProfile || preview?.document_profile || result?.documentProfile || result?.document_profile || {};
  const profileType = String(profile?.type || "").trim().toUpperCase();
  const profileDeposit = String(profile?.deposit || "").trim();
  const updatesStock = Boolean(profile?.updateStock);
  const documentCount = Number(preview?.documentCount || preview?.document_count || 1);
  const documentLineCounts = Array.isArray(preview?.documentLineCounts)
    ? preview.documentLineCounts.map(Number).filter((value) => Number.isFinite(value) && value > 0)
    : Array.isArray(preview?.document_line_counts)
      ? preview.document_line_counts.map(Number).filter((value) => Number.isFinite(value) && value > 0)
      : [];
  const printCapacityLabel = documentCount > 1
    ? `se emitirán ${documentCount} remitos${documentLineCounts.length ? ` · ${documentLineCounts.join(" + ")} líneas` : ""}`
    : `${preview.lineCount || 0} ${Number(preview.lineCount) === 1 ? "línea" : "líneas"}`;
  const partidaLabel = updatesStock && profileType === "RDA" ? "N° serie del equipo" : "identificador del equipo";
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
                ? `${normalizedDocumentLabel} listo para ${actionText} · ${printCapacityLabel}`
                : result?.detail || "La validación encontró problemas."
              : `Valide los datos antes de crear o ${actionText}.`}
          </div>
          {profileType && (
            <div className="mt-1 text-xs font-medium text-gray-700">
              Comprobante: {profileType}
              {profileDeposit ? ` · Depósito: ${profileDeposit}` : ""}
              {updatesStock ? ` · Partida: ${partidaLabel}` : " · Sin movimiento de stock"}
            </div>
          )}
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
              onConflictResolved={onConflictResolved}
            />
          ))}
        </div>
      )}
    </section>
  );
}
