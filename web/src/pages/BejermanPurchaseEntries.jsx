import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardList,
  PackagePlus,
  RefreshCw,
  Save,
  Search,
  Send,
  Trash2,
  XCircle,
} from "lucide-react";
import {
  deleteBejermanPurchaseEntry,
  deleteBejermanPurchaseEntryLine,
  deleteBejermanPurchaseEntryScan,
  getBejermanPurchaseArticles,
  getBejermanPurchaseEntries,
  getBejermanPurchaseEntry,
  getBejermanPurchaseOrders,
  getBejermanPurchaseHistory,
  getBejermanPurchaseProviders,
  patchBejermanPurchaseEntry,
  patchBejermanPurchaseEntryLine,
  patchBejermanPurchaseEntryScan,
  postBejermanPurchaseEntry,
  postBejermanPurchaseEntryEmit,
  postBejermanPurchaseEntryLine,
  postBejermanPurchaseEntryPopulateFromOrder,
  postBejermanPurchaseEntryReopen,
  postBejermanPurchaseEntryScan,
  postBejermanPurchaseEntryValidate,
  postBejermanPurchaseEntryVerify,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import {
  DesktopTableWrap,
  MobileDataCard,
  MobileDataList,
  ResponsiveActionBar,
  fullWidthButtonClass,
} from "../components/Responsive.jsx";
import { MobileSortControl } from "../components/TableSortControls.jsx";
import { sortRows } from "../lib/table-sort.js";

const STATUS_LABELS = {
  draft: "Borrador",
  validated: "Validado",
  running: "Registrando",
  verifying: "Verificando",
  generated: "Registrado",
  failed: "Fallido",
  failed_review: "Revisión necesaria",
  cancelled: "Cancelado",
};

const STATUS_CLASS = {
  draft: "border-slate-200 bg-slate-50 text-slate-700",
  validated: "border-emerald-200 bg-emerald-50 text-emerald-700",
  running: "border-blue-200 bg-blue-50 text-blue-700",
  verifying: "border-amber-300 bg-amber-50 text-amber-800",
  generated: "border-emerald-300 bg-emerald-100 text-emerald-800",
  failed: "border-rose-200 bg-rose-50 text-rose-700",
  failed_review: "border-amber-300 bg-amber-50 text-amber-800",
  cancelled: "border-gray-200 bg-gray-50 text-gray-600",
};

const MIN_LOOKUP_QUERY_LENGTH = 2;
const MAX_BULK_SCAN_CODES = 5000;
const SCAN_PAGE_SIZE = 200;

const PURCHASE_SORT_OPTIONS = [
  { value: "documento", label: "Documento" },
  { value: "proveedor", label: "Proveedor" },
  { value: "fecha", label: "Fecha" },
  { value: "estado", label: "Estado" },
  { value: "total", label: "Total" },
];

function todayIso() {
  const now = new Date();
  const localDate = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return localDate.toISOString().slice(0, 10);
}

function money(value) {
  const number = Number(value || 0);
  return number.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function quantity(value) {
  const number = Number(value || 0);
  return number.toLocaleString("es-AR", { maximumFractionDigits: 4 });
}

function numericInput(value) {
  let text = String(value ?? "").trim();
  if (text.includes(",") && text.includes(".")) {
    text = text.lastIndexOf(",") > text.lastIndexOf(".")
      ? text.replaceAll(".", "").replace(",", ".")
      : text.replaceAll(",", "");
  } else if (text.includes(",")) {
    text = text.replace(",", ".");
  }
  return Number(text);
}

function errText(error) {
  return error?.data?.detail || error?.message || "Error inesperado";
}

function documentNumber(value) {
  if (!value) return "RT sin numerar";
  if (value.remitoNumber) return value.remitoNumber;
  const type = value.comprobanteTipo || "RT";
  const letter = value.comprobanteLetra || "R";
  const point = value.comprobantePtoVenta || "-";
  const number = value.comprobanteNumero || "-";
  return `${type} ${letter} ${point}-${number}`;
}

function bulkBarcodeSummary(value) {
  const items = String(value || "")
    .split(/[\r\n\t,;]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const seen = new Set();
  const duplicates = [];
  items.forEach((item) => {
    const normalized = item.replace(/\s+/g, " ").trim().toUpperCase();
    if (seen.has(normalized)) duplicates.push(item);
    else seen.add(normalized);
  });
  return {
    items,
    total: items.length,
    unique: seen.size,
    duplicateCount: duplicates.length,
    duplicates,
  };
}

function purchaseAvailability(item) {
  if (!item || typeof item !== "object") return null;
  if (
    item.purchaseEnabled === false ||
    item.enabled === false ||
    item.controlsStock === false ||
    item.stockEnabled === false
  ) return false;
  if (
    item.purchaseEnabled === true &&
    item.controlsStock === true &&
    item.stockEnabled === true
  ) {
    return true;
  }
  return null;
}

function requiresPartida(item) {
  if (!item || typeof item !== "object") return null;
  if (typeof item.requiresPartida === "boolean") return item.requiresPartida;
  if (typeof item.stockByPartida === "boolean") return item.stockByPartida;
  return null;
}

function eventLabel(eventType) {
  const labels = {
    created: "Lote creado",
    updated: "Encabezado actualizado",
    line_added: "Artículo agregado",
    line_updated: "Artículo actualizado",
    line_deleted: "Artículo eliminado",
    scan_added: "Bultos cargados",
    scan_updated: "Bulto actualizado",
    scan_deleted: "Bulto eliminado",
    validated: "Lote validado",
    emission_started: "Registración iniciada",
    emission_generated: "RT registrado",
    emission_failed: "Falló la registración",
    emission_review_required: "Registración pendiente de revisión",
    emission_accepted: "RT aceptado; verificando postcondiciones",
    emission_reconciled: "Registración verificada",
    emission_not_found: "RT no encontrado en Bejerman",
    emission_ambiguous: "Verificación ambigua",
    postconditions_verified: "Postcondiciones confirmadas",
    postconditions_pending: "Postcondiciones pendientes",
    postconditions_ambiguous: "Postcondiciones requieren revisión",
    postconditions_unavailable: "Verificación posterior no disponible",
    verification_started: "Verificación posterior iniciada",
    verification_pending: "Postcondiciones pendientes",
    verification_confirmed: "Postcondiciones confirmadas",
    verification_ambiguous: "Postcondiciones requieren revisión",
    verification_unavailable: "Verificación posterior no disponible",
    postcondition_verification_started: "Verificación posterior iniciada",
    postcondition_verification_pending: "Postcondiciones pendientes",
    postcondition_verification_confirmed: "Postcondiciones confirmadas",
    postcondition_verification_ambiguous: "Postcondiciones requieren revisión",
    postcondition_verification_unavailable: "Verificación posterior no disponible",
    reopened: "Lote reabierto",
    discarded: "Lote descartado",
    lines_populated_from_order: "Artículos autocompletados desde la OC",
    order_detail_unavailable: "Bejerman no devolvió el detalle de la OC citada",
  };
  return labels[eventType] || eventType || "Actividad";
}

function formatEventDate(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString("es-AR");
}

function StatusBadge({ status }) {
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs ${STATUS_CLASS[status] || STATUS_CLASS.draft}`}>
      {STATUS_LABELS[status] || status || "Borrador"}
    </span>
  );
}

function flattenScans(entry) {
  return (entry?.lines || []).flatMap((line) =>
    (line.scans || []).map((scan) => ({
      ...scan,
      line,
    }))
  );
}

function articleSummaryLines(entry, limit = 20) {
  const lines = (entry?.lines || []).map((line) => {
    const scans = line.scans || [];
    const totalQuantity = scans.reduce(
      (sum, scan) => sum + Number(scan.conversionFactor || 0),
      0,
    );
    const totalValue = scans.reduce(
      (sum, scan) => sum + Number(scan.totalValue || 0),
      0,
    );
    return `${line.articleCode || "-"} · ${scans.length} bultos · ${quantity(totalQuantity)} unidades · neto ${money(totalValue)}`;
  });
  if (lines.length <= limit) return lines;
  return [
    ...lines.slice(0, limit),
    `… y ${lines.length - limit} artículos más`,
  ];
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function purchaseVerificationFrom(value) {
  const source = objectValue(value);
  if (!source) return null;
  const sourceEntry = objectValue(source.entry);
  const entryValue = sourceEntry || source;
  const responsePayload = objectValue(entryValue.responsePayload) || {};
  const candidate =
    objectValue(source.postconditions) ||
    objectValue(source.postconditionVerification) ||
    objectValue(source.verification) ||
    objectValue(responsePayload.postconditionVerification) ||
    objectValue(responsePayload.postconditions) ||
    objectValue(responsePayload.verification);
  if (!candidate && !source.outcome) return null;
  const result = {
    ...(candidate || {}),
    outcome: source.outcome || candidate?.outcome || "",
  };
  if (typeof source.canReopen === "boolean") result.canReopen = source.canReopen;
  if (typeof source.reopenAuthorized === "boolean") {
    result.reopenAuthorized = source.reopenAuthorized;
  }
  return result;
}

function normalizedEvidenceStatus(value) {
  const status = String(value || "").trim().toLowerCase();
  if (["confirmed", "verified", "ok", "matched"].includes(status)) return "confirmed";
  if (["pending", "verifying", "not_found", "missing"].includes(status)) return "pending";
  if (["ambiguous", "mismatch", "conflict", "failed_review"].includes(status)) return "ambiguous";
  if (["unavailable", "baseline_unavailable", "error", "failed"].includes(status)) return "unavailable";
  if (["not_applicable", "not-applicable", "na"].includes(status)) return "not_applicable";
  return "";
}

function sectionEvidenceStatus(section, fallback = "pending") {
  const value = objectValue(section) || {};
  const explicit = normalizedEvidenceStatus(value.status || value.overallStatus);
  if (explicit) return explicit;
  if (value.confirmed === true) return "confirmed";
  if (value.confirmed === false) return fallback;
  return fallback;
}

function purchaseVerificationStatus(verification) {
  if (!verification) return "pending";
  const explicit = normalizedEvidenceStatus(
    verification.overallStatus || verification.status,
  );
  if (explicit) return explicit;
  const sections = [
    objectValue(verification.header) || objectValue(verification.document),
    objectValue(verification.stock),
    objectValue(verification.partidas),
  ].filter(Boolean);
  const sectionStatuses = sections
    .map((section) => normalizedEvidenceStatus(section.status || section.overallStatus))
    .filter(Boolean);
  if (sectionStatuses.includes("ambiguous")) return "ambiguous";
  if (sectionStatuses.includes("unavailable")) return "unavailable";
  if (
    sectionStatuses.length === sections.length &&
    sectionStatuses.every((status) => ["confirmed", "not_applicable"].includes(status))
  ) {
    return "confirmed";
  }
  const outcome = normalizedEvidenceStatus(verification.outcome);
  if (
    outcome === "confirmed" &&
    (sections.length > 0 || verification.confirmed === true)
  ) {
    return "confirmed";
  }
  if (["ambiguous", "unavailable"].includes(outcome)) return outcome;
  return "pending";
}

function evidenceStatusLabel(status) {
  return {
    confirmed: "Confirmado",
    pending: "Pendiente",
    ambiguous: "Revisión necesaria",
    unavailable: "No disponible",
    not_applicable: "No aplica",
  }[status] || "Pendiente";
}

function evidenceStatusClass(status) {
  return {
    confirmed: "border-emerald-200 bg-emerald-50 text-emerald-800",
    pending: "border-amber-300 bg-amber-50 text-amber-900",
    ambiguous: "border-rose-300 bg-rose-50 text-rose-900",
    unavailable: "border-slate-300 bg-slate-50 text-slate-800",
    not_applicable: "border-gray-200 bg-gray-50 text-gray-700",
  }[status] || "border-amber-300 bg-amber-50 text-amber-900";
}

function verificationDocumentSummary(header) {
  const evidence = objectValue(header) || {};
  const observed = objectValue(evidence.observed) || objectValue(evidence.actual) || evidence;
  const expected = objectValue(evidence.expected) || {};
  const remito =
    observed.remitoNumber ||
    observed.documentNumber ||
    evidence.remitoNumber ||
    evidence.documentNumber ||
    "";
  const type =
    observed.comprobanteTipo ||
    observed.type ||
    expected.comprobanteTipo ||
    expected.type ||
    "RT";
  const letter =
    observed.comprobanteLetra ||
    observed.letter ||
    expected.comprobanteLetra ||
    expected.letter ||
    "R";
  const point =
    observed.comprobantePtoVenta ||
    observed.pointOfSale ||
    expected.comprobantePtoVenta ||
    expected.pointOfSale ||
    "";
  const number =
    observed.comprobanteNumero ||
    observed.number ||
    expected.comprobanteNumero ||
    expected.number ||
    "";
  const operation =
    observed.operationCode ||
    observed.operation ||
    evidence.operationCode ||
    evidence.operation ||
    expected.operationCode ||
    expected.operation ||
    "MC";
  const identity = remito || (point || number ? `${type} ${letter} ${point || "-"}-${number || "-"}` : "");
  return identity
    ? `${identity} · operación ${operation}`
    : `Documento RT · operación ${operation}`;
}

function verificationQuantity(value) {
  return value === null || value === undefined || value === "" ? "-" : quantity(value);
}

function verificationIssueText(issue) {
  const item = issue.item || {};
  const reason = item.reason || item.message || item.detail || "";
  if (issue.kind === "document") {
    const expected = objectValue(item.expected) || {};
    const observed = objectValue(item.observed) || {};
    const article = expected.articleCode || observed.articleCode || "-";
    const expectedQuantity = verificationQuantity(expected.quantity);
    const observedQuantity = verificationQuantity(observed.quantity);
    return `${article} · cabecera/ítem ${reason || "diferente"} · esperado ${expectedQuantity} · observado ${observedQuantity}`;
  }
  if (issue.kind === "partida") {
    const article = item.articleCode || "-";
    const partida = item.partida || item.barcode || "-";
    const expected = verificationQuantity(
      item.expectedQuantity ?? item.expectedDelta ?? item.expectedIncrement,
    );
    const observed = verificationQuantity(
      item.realQuantity ??
        item.observedIncrement ??
        item.currentRealQuantity ??
        item.afterReal ??
        item.observedQuantity,
    );
    return `${article} · partida ${partida} · esperado ${expected} · observado ${observed}${reason ? ` · ${reason}` : ""}`;
  }
  const article = item.articleCode || "-";
  const expected = verificationQuantity(
    item.expectedDelta ?? item.expectedQuantity ?? item.expectedIncrement,
  );
  const observed = verificationQuantity(
    item.observedDelta ?? item.observedIncrement ?? item.observedQuantity,
  );
  return `${article} · incremento esperado ${expected} · observado ${observed}${reason ? ` · ${reason}` : ""}`;
}

function PurchaseVerificationPanel({ verification, entryStatus, error = "" }) {
  const overallStatus = purchaseVerificationStatus(verification);
  const header =
    objectValue(verification?.header) ||
    objectValue(verification?.document) ||
    {};
  const stock = objectValue(verification?.stock) || {};
  const partidas = objectValue(verification?.partidas) || {};
  const outcome = String(verification?.outcome || "").toLowerCase();
  const headerStatus = sectionEvidenceStatus(
    header,
    outcome === "matched"
      ? "confirmed"
      : overallStatus,
  );
  const stockStatus = sectionEvidenceStatus(stock, overallStatus);
  const expectedPartidas = Number(
    partidas.expectedCount ?? partidas.totalCount ?? partidas.required ?? 0,
  );
  const partidasStatus = sectionEvidenceStatus(
    partidas,
    Object.keys(partidas).length > 0 && expectedPartidas === 0
      ? "not_applicable"
      : overallStatus,
  );
  const stockArticles = Array.isArray(stock.articles) ? stock.articles : [];
  const partidasItems = Array.isArray(partidas.items)
    ? partidas.items
    : Array.isArray(partidas.missing)
      ? partidas.missing
      : [];
  const confirmedArticles = stockArticles.filter(
    (item) => sectionEvidenceStatus(item) === "confirmed",
  ).length;
  const verifiedPartidas = Number(
    partidas.verifiedCount ??
      partidas.confirmedCount ??
      (typeof partidas.confirmed === "number" ? partidas.confirmed : undefined) ??
      partidasItems.filter(
        (item) => sectionEvidenceStatus(item) === "confirmed",
      ).length,
  );
  const documentItems = objectValue(header.items) || {};
  const documentIssues = (
    Array.isArray(documentItems.mismatches) ? documentItems.mismatches : []
  ).map((item) => ({ kind: "document", item }));
  const stockIssues = stockArticles
    .filter(
      (item) =>
        !["confirmed", "not_applicable"].includes(
          sectionEvidenceStatus(item),
        ),
    )
    .map((item) => ({ kind: "stock", item }));
  const partidaIssues = partidasItems
    .filter(
      (item) =>
        !["confirmed", "not_applicable"].includes(
          sectionEvidenceStatus(
            {
              ...item,
              status:
                item?.status ||
                (item?.catalogConfirmed === false
                  ? item?.quantityStatus === "ambiguous"
                    ? "ambiguous"
                    : "pending"
                  : item?.quantityStatus),
            },
          ),
        ),
    )
    .map((item) => ({ kind: "partida", item }));
  const issues = [...documentIssues, ...stockIssues, ...partidaIssues];
  const visibleIssues = issues.slice(0, 20);
  const overallCopy = {
    confirmed:
      "Bejerman confirmó la cabecera RT/MC, el ingreso de stock en VAL y las partidas requeridas.",
    pending:
      "La registración no debe repetirse: todavía falta confirmar la cabecera RT/MC, el stock en VAL o alguna partida.",
    ambiguous:
      "La cabecera, el stock o las partidas devolvieron evidencia contradictoria. No reabras ni vuelvas a registrar el lote.",
    unavailable:
      "No se pudo completar la consulta posterior. El lote continúa bloqueado: no vuelvas a registrarlo y reintentá la verificación.",
  };
  const panelStatus = error && !verification ? "unavailable" : overallStatus;
  const stockDeposit = stock.deposit || stock.depositCode || "VAL";
  const checkedAt = verification?.checkedAt;
  const nextCheckAt = verification?.nextCheckAt;

  return (
    <div className={`mt-3 rounded border px-3 py-3 text-sm ${evidenceStatusClass(panelStatus)}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-semibold">Verificación posterior en Bejerman</div>
        <span className="rounded-full border border-current/20 px-2 py-0.5 text-xs font-semibold">
          {evidenceStatusLabel(panelStatus)}
        </span>
      </div>
      <div className="mt-1">
        {overallCopy[panelStatus] || overallCopy.pending}
      </div>
      {error && (
        <div className="mt-2 rounded border border-current/20 bg-white/60 px-2 py-1.5 text-xs">
          Último intento: {error}
        </div>
      )}
      <div className="mt-3 grid gap-2 lg:grid-cols-3">
        <div className="rounded border border-current/20 bg-white/60 px-2 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">Cabecera RT/MC</span>
            <span className="text-xs font-medium">{evidenceStatusLabel(headerStatus)}</span>
          </div>
          <div className="mt-1 text-xs">{verificationDocumentSummary(header)}</div>
        </div>
        <div className="rounded border border-current/20 bg-white/60 px-2 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">Stock {stockDeposit}</span>
            <span className="text-xs font-medium">{evidenceStatusLabel(stockStatus)}</span>
          </div>
          <div className="mt-1 text-xs">
            {stockArticles.length > 0
              ? `${confirmedArticles}/${stockArticles.length} artículos confirmados en el depósito ${stockDeposit}.`
              : stockStatus === "confirmed"
                ? `Ingreso de stock confirmado en el depósito ${stockDeposit}.`
                : `Falta confirmar el ingreso de stock en el depósito ${stockDeposit}.`}
          </div>
        </div>
        <div className="rounded border border-current/20 bg-white/60 px-2 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">Partidas</span>
            <span className="text-xs font-medium">{evidenceStatusLabel(partidasStatus)}</span>
          </div>
          <div className="mt-1 text-xs">
            {partidasStatus === "not_applicable"
              ? "No aplica para los artículos de este lote."
              : Object.keys(partidas).length === 0
                ? "Falta confirmar las partidas requeridas en VAL."
                : `${verifiedPartidas}/${expectedPartidas || partidasItems.length} partidas confirmadas en VAL.`}
          </div>
        </div>
      </div>
      {issues.length > 0 && (
        <details className="mt-3 rounded border border-current/20 bg-white/60 px-2 py-2">
          <summary className="cursor-pointer text-xs font-semibold">
            Ver pendientes o diferencias ({issues.length})
          </summary>
          <div className="mt-2 space-y-1 text-xs">
            {visibleIssues.map((issue, index) => (
              <div
                key={`${issue.kind}-${issue.item?.articleCode || ""}-${issue.item?.partida || issue.item?.barcode || ""}-${index}`}
                className="break-words"
              >
                {verificationIssueText(issue)}
              </div>
            ))}
            {issues.length > visibleIssues.length && (
              <div className="font-medium">
                … y {issues.length - visibleIssues.length} diferencias más.
              </div>
            )}
          </div>
        </details>
      )}
      {(checkedAt || nextCheckAt || entryStatus === "verifying") && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {checkedAt && <span>Consultado: {formatEventDate(checkedAt)}</span>}
          {nextCheckAt && (
            <span>Próximo reintento sugerido: {formatEventDate(nextCheckAt)}</span>
          )}
          {!checkedAt && entryStatus === "verifying" && (
            <span>Usá “Verificar en Bejerman” para actualizar esta evidencia.</span>
          )}
        </div>
      )}
    </div>
  );
}

const emptyHeader = {
  supplierCode: "",
  supplierCodeRaw: "",
  supplierName: "",
  supplierTaxId: "",
  paymentTermCode: "",
  comprobanteLetra: "R",
  comprobantePtoVenta: "",
  comprobanteNumero: "",
  comprobanteTipoOrigen: "",
  comprobanteLetraOrigen: "",
  comprobantePtoVentaOrigen: "",
  comprobanteNumeroOrigen: "",
  issueDate: todayIso(),
  accountingDate: todayIso(),
  ddjjDate: todayIso(),
  notes: "",
  supplierSnapshot: {},
  provider: null,
};

function headerFromEntry(entry) {
  if (!entry) return emptyHeader;
  const supplierSnapshot =
    entry.supplierSnapshot && typeof entry.supplierSnapshot === "object"
      ? entry.supplierSnapshot
      : {};
  return {
    supplierCode: entry.supplierCode || "",
    supplierCodeRaw: entry.supplierCodeRaw || entry.supplierCode || "",
    supplierName: entry.supplierName || "",
    supplierTaxId: entry.supplierTaxId || "",
    paymentTermCode: entry.paymentTermCode || "",
    comprobanteLetra: entry.comprobanteLetra || "R",
    comprobantePtoVenta: entry.comprobantePtoVenta || "",
    comprobanteNumero: entry.comprobanteNumero || "",
    comprobanteTipoOrigen: entry.comprobanteTipoOrigen || "",
    comprobanteLetraOrigen: entry.comprobanteLetraOrigen || "",
    comprobantePtoVentaOrigen: entry.comprobantePtoVentaOrigen || "",
    comprobanteNumeroOrigen: entry.comprobanteNumeroOrigen || "",
    comprobanteOrigenLabel: entry.comprobanteOrigenLabel || "",
    issueDate: entry.issueDate || todayIso(),
    accountingDate: entry.accountingDate || entry.issueDate || todayIso(),
    ddjjDate: entry.ddjjDate || entry.issueDate || todayIso(),
    notes: entry.notes || "",
    supplierSnapshot,
    provider: entry.supplierCode || entry.supplierName
      ? {
          code: entry.supplierCode || "",
          rawCode: entry.supplierCodeRaw || entry.supplierCode || "",
          name: entry.supplierName || "",
          cuit: entry.supplierTaxId || "",
          paymentTermCode: entry.paymentTermCode || "",
          purchaseEnabled: entry.purchaseEnabled ?? supplierSnapshot.purchaseEnabled ?? null,
          enabled: entry.enabled ?? supplierSnapshot.enabled ?? null,
          raw: supplierSnapshot,
        }
      : null,
  };
}

export default function BejermanPurchaseEntries({ embedded = false, companyKey = "SEPID" } = {}) {
  const { user } = useAuth();
  const canManage = can(user, PERMISSION_CODES.ACTION_BEJERMAN_PURCHASE_ENTRIES_MANAGE);
  const canEmit = can(user, PERMISSION_CODES.ACTION_BEJERMAN_PURCHASE_ENTRIES_EMIT);
  const scannerRef = useRef(null);
  const articleLookupSeqRef = useRef(0);
  const activeArticleLookupSeqRef = useRef(0);
  const busyRef = useRef(false);

  const [entries, setEntries] = useState([]);
  const [runtimeInfo, setRuntimeInfo] = useState({
    bejermanEnvironment: "",
    emissionEnabled: false,
  });
  const [entry, setEntry] = useState(null);
  const [header, setHeader] = useState(emptyHeader);
  const [headerDirty, setHeaderDirty] = useState(false);
  const [activeLineId, setActiveLineId] = useState("");
  const [providerQuery, setProviderQuery] = useState("");
  const [providers, setProviders] = useState([]);
  const [orderQuery, setOrderQuery] = useState("");
  const [orders, setOrders] = useState([]);
  const [orderLoading, setOrderLoading] = useState(false);
  const [orderError, setOrderError] = useState("");
  const [articleQuery, setArticleQuery] = useState("");
  const [articles, setArticles] = useState([]);
  const [articleLoading, setArticleLoading] = useState(false);
  const [articleError, setArticleError] = useState("");
  const [activeArticleMetadataError, setActiveArticleMetadataError] = useState("");
  const [activeArticleMetadataRetry, setActiveArticleMetadataRetry] = useState(0);
  const [lineForm, setLineForm] = useState({
    articleCode: "",
    articleDescription: "",
    defaultConversionFactor: "1",
    defaultUnitValue: "",
    purchaseEnabled: null,
    enabled: null,
    controlsStock: null,
    stockEnabled: null,
    stockByPartida: null,
    requiresPartida: null,
    partidaMask: "",
  });
  const [articleMetadataByCode, setArticleMetadataByCode] = useState({});
  const [activeDefaults, setActiveDefaults] = useState({ defaultConversionFactor: "1", defaultUnitValue: "" });
  const [scanText, setScanText] = useState("");
  const [bulkScanText, setBulkScanText] = useState("");
  const [manualQuantity, setManualQuantity] = useState("");
  const [scanEdits, setScanEdits] = useState({});
  const [history, setHistory] = useState([]);
  const [verification, setVerification] = useState(null);
  const [verificationLoading, setVerificationLoading] = useState(false);
  const [verificationError, setVerificationError] = useState("");
  const [entriesSort, setEntriesSort] = useState("");
  const [historySort, setHistorySort] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [validation, setValidation] = useState(null);
  const [scanPage, setScanPage] = useState(1);

  const activeLine = useMemo(
    () => (entry?.lines || []).find((line) => line.id === activeLineId) || (entry?.lines || [])[0] || null,
    [activeLineId, entry]
  );
  const scans = useMemo(() => flattenScans(entry), [entry]);
  const bulkScanSummary = useMemo(() => bulkBarcodeSummary(bulkScanText), [bulkScanText]);
  const bulkLimitExceeded = bulkScanSummary.total > MAX_BULK_SCAN_CODES;
  const scanPageCount = Math.max(1, Math.ceil(scans.length / SCAN_PAGE_SIZE));
  const visibleScans = useMemo(() => {
    const start = (scanPage - 1) * SCAN_PAGE_SIZE;
    return scans.slice(start, start + SCAN_PAGE_SIZE);
  }, [scanPage, scans]);
  const hasPendingCapture =
    Boolean(scanText.trim()) ||
    Boolean(bulkScanText.trim()) ||
    Boolean(manualQuantity.trim()) ||
    Object.keys(scanEdits).length > 0;
  const activeArticleMetadata = useMemo(() => {
    if (!activeLine?.articleCode) return {};
    return articleMetadataByCode[String(activeLine.articleCode).trim().toUpperCase()] || {};
  }, [activeLine?.articleCode, articleMetadataByCode]);
  const activeRequiresPartida =
    requiresPartida(activeLine) ??
    requiresPartida(activeArticleMetadata);
  const activeHasPositiveValue = numericInput(activeLine?.defaultUnitValue) > 0;
  const activeCompanyKey = entry?.companyKey || companyKey || "SEPID";
  const emissionEnabled = entry?.id
    ? entry.emissionEnabled === true
    : runtimeInfo.emissionEnabled === true;
  const purchaseSortColumns = useMemo(() => ({
    documento: { getValue: (item) => item.remitoNumber || `${item.comprobanteTipo || "RT"} ${item.comprobantePtoVenta || ""}-${item.comprobanteNumero || ""}` },
    proveedor: { getValue: (item) => item.supplierName || item.supplierCode },
    fecha: { getValue: (item) => item.issueDate, type: "date" },
    estado: { getValue: (item) => item.status },
    total: { getValue: (item) => item.totalValue, type: "number" },
  }), []);
  const displayEntries = useMemo(
    () => sortRows(entries, entriesSort, purchaseSortColumns),
    [entries, entriesSort, purchaseSortColumns],
  );
  const displayHistory = useMemo(
    () => sortRows(history, historySort, purchaseSortColumns),
    [history, historySort, purchaseSortColumns],
  );
  const displayVerification = verification || purchaseVerificationFrom(entry);
  const isLocked = ["running", "verifying", "generated", "failed_review", "cancelled"].includes(entry?.status);
  const canVerifyEntry =
    canEmit &&
    ["running", "verifying", "failed_review", "generated"].includes(entry?.status);
  const safeReopenAuthorized =
    displayVerification?.reopenAuthorized === true ||
    (
      displayVerification?.canReopen === true &&
      displayVerification?.exhausted === true &&
      Number(displayVerification?.attempt || 0) >= 2
    );
  const canReopenEntry =
    canEmit &&
    entry?.status === "failed_review" &&
    safeReopenAuthorized;
  const showVerificationPanel =
    Boolean(displayVerification) ||
    Boolean(verificationError) ||
    ["verifying", "generated"].includes(entry?.status);

  const focusScanner = useCallback(() => {
    window.setTimeout(() => scannerRef.current?.focus(), 30);
  }, []);

  const loadEntries = useCallback(async () => {
    const data = await getBejermanPurchaseEntries({ limit: 80, companyKey });
    setEntries(data.items || []);
    setRuntimeInfo({
      bejermanEnvironment: data.bejermanEnvironment || "",
      emissionEnabled: data.emissionEnabled === true,
    });
    return data.items || [];
  }, [companyKey]);

  const loadEntry = useCallback(
    async (entryId) => {
      const data = await getBejermanPurchaseEntry(entryId);
      setEntry(data);
      setHeader(headerFromEntry(data));
      setHeaderDirty(false);
      setActiveLineId((current) => (data.lines || []).some((line) => line.id === current) ? current : (data.lines || [])[0]?.id || "");
      setValidation(null);
      setVerification(purchaseVerificationFrom(data));
      setVerificationError("");
      focusScanner();
      return data;
    },
    [focusScanner]
  );

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        setLoading(true);
        const items = await loadEntries();
        if (alive && items[0]?.id) await loadEntry(items[0].id);
      } catch (exc) {
        if (alive) setError(errText(exc));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [loadEntries, loadEntry]);

  useEffect(() => {
    focusScanner();
  }, [activeLineId, entry?.id, focusScanner]);

  useEffect(() => {
    setScanText("");
    setBulkScanText("");
    setManualQuantity("");
    setScanEdits({});
    setScanPage(1);
  }, [activeLineId, entry?.id]);

  useEffect(() => {
    setScanPage((current) => Math.min(Math.max(current, 1), scanPageCount));
  }, [scanPageCount]);

  useEffect(() => {
    if (!activeLine) {
      setActiveArticleMetadataError("");
      return;
    }
    setActiveDefaults({
      defaultConversionFactor: String(activeLine.defaultConversionFactor ?? "1"),
      defaultUnitValue: String(activeLine.defaultUnitValue ?? ""),
    });
  }, [activeLine?.id, activeLine?.defaultConversionFactor, activeLine?.defaultUnitValue]);

  useEffect(() => {
    const articleCode = String(activeLine?.articleCode || "").trim();
    const articleKey = articleCode.toUpperCase();
    if (
      !articleCode ||
      articleMetadataByCode[articleKey]?.metadataChecked === true
    ) return undefined;
    setActiveArticleMetadataError("");
    const seq = activeArticleLookupSeqRef.current + 1;
    activeArticleLookupSeqRef.current = seq;
    let alive = true;
    (async () => {
      try {
        const data = await getBejermanPurchaseArticles({
          q: articleCode,
          search: articleCode,
          limit: 50,
          companyKey: activeCompanyKey,
        });
        if (!alive || activeArticleLookupSeqRef.current !== seq) return;
        const exact = (data.items || []).find(
          (item) => String(item.code || item.id || "").trim().toUpperCase() === articleKey,
        );
        if (!exact) {
          setActiveArticleMetadataError(
            `No se pudo verificar el artículo ${articleCode} en Bejerman.`,
          );
          return;
        }
        setArticleMetadataByCode((current) => ({
          ...current,
          [articleKey]: {
            metadataChecked: true,
            purchaseEnabled: exact.purchaseEnabled ?? null,
            enabled: exact.enabled ?? null,
            controlsStock: exact.controlsStock ?? null,
            stockEnabled: exact.stockEnabled ?? null,
            stockByPartida: exact.stockByPartida ?? null,
            requiresPartida: exact.requiresPartida ?? null,
            partidaMask: exact.partidaMask || "",
          },
        }));
      } catch (exc) {
        if (!alive || activeArticleLookupSeqRef.current !== seq) return;
        setActiveArticleMetadataError(
          `No se pudo verificar el artículo en Bejerman: ${errText(exc)}`,
        );
      }
    })();
    return () => {
      alive = false;
    };
  }, [activeLine?.articleCode, activeArticleMetadataRetry, articleMetadataByCode, activeCompanyKey]);

  const loadArticleSuggestions = useCallback(async (query, { limit = 12, showError = false } = {}) => {
    const q = String(query || "").trim();
    if (q.length < MIN_LOOKUP_QUERY_LENGTH) {
      articleLookupSeqRef.current += 1;
      setArticles([]);
      setArticleError(
        showError
          ? `Ingresá al menos ${MIN_LOOKUP_QUERY_LENGTH} caracteres para buscar artículos.`
          : "",
      );
      setArticleLoading(false);
      return null;
    }
    const seq = articleLookupSeqRef.current + 1;
    articleLookupSeqRef.current = seq;
    setArticleLoading(true);
    setArticleError("");
    try {
      const data = await getBejermanPurchaseArticles({ q, search: q, limit, companyKey: activeCompanyKey });
      if (articleLookupSeqRef.current !== seq) return data;
      if (data?.unavailable) {
        setArticles([]);
        setArticleError("El catálogo de artículos Bejerman no está disponible. No continúes con un código sin verificar.");
        return data;
      }
      setArticles(data.items || []);
      if (!(data.items || []).length) {
        setArticleError(`No se encontraron artículos para “${q}”.`);
      }
      return data;
    } catch (exc) {
      if (articleLookupSeqRef.current !== seq) return null;
      setArticles([]);
      const message = errText(exc);
      if (showError) setError(message);
      else setArticleError(message);
      return null;
    } finally {
      if (articleLookupSeqRef.current === seq) setArticleLoading(false);
    }
  }, [activeCompanyKey]);

  async function run(
    action,
    okMessage = "",
    { focusAfter = true, refreshOnError = false, refreshEntryId = entry?.id } = {},
  ) {
    if (busyRef.current) return null;
    busyRef.current = true;
    setError("");
    setMessage("");
    setLoading(true);
    try {
      const result = await action();
      if (okMessage) setMessage(okMessage);
      return result;
    } catch (exc) {
      const detail = errText(exc);
      setError(detail);
      if (refreshOnError && refreshEntryId) {
        try {
          await loadEntries();
          await loadEntry(refreshEntryId);
        } catch (refreshError) {
          setError(`${detail}. Además, no se pudo refrescar el lote: ${errText(refreshError)}`);
        }
      }
      return null;
    } finally {
      busyRef.current = false;
      setLoading(false);
      if (focusAfter) focusScanner();
    }
  }

  function applyEntryState(nextEntry, { selectLastLine = false } = {}) {
    if (!nextEntry?.id) return;
    setEntry(nextEntry);
    setHeader(headerFromEntry(nextEntry));
    setHeaderDirty(false);
    setVerification(purchaseVerificationFrom(nextEntry));
    setActiveLineId((current) => {
      if (selectLastLine) return (nextEntry.lines || []).at(-1)?.id || current;
      return (nextEntry.lines || []).some((line) => line.id === current)
        ? current
        : (nextEntry.lines || [])[0]?.id || "";
    });
  }

  async function refreshCurrent(nextEntry = null, { selectLastLine = false } = {}) {
    await loadEntries();
    if (nextEntry?.id) {
      applyEntryState(nextEntry, { selectLastLine });
      setValidation(null);
      return nextEntry;
    }
    if (entry?.id) return loadEntry(entry.id);
    return null;
  }

  function updateHeader(nextHeader) {
    setHeader(nextHeader);
    setHeaderDirty(true);
    setValidation(null);
    setVerification(null);
  }

  function updateHeaderField(field, value) {
    updateHeader({ ...header, [field]: value });
  }

  async function persistHeaderDirect() {
    if (!entry?.id || !headerDirty) return entry;
    const data = await patchBejermanPurchaseEntry(entry.id, header);
    await loadEntries();
    applyEntryState(data);
    setValidation(null);
    setVerification(null);
    return data;
  }

  function confirmDiscardPendingCapture(actionLabel) {
    if (!hasPendingCapture) return true;
    return window.confirm(
      `Hay códigos, cantidades o ediciones todavía sin guardar. ¿Descartarlos y ${actionLabel}?`,
    );
  }

  function selectActiveLine(nextLineId) {
    if (!nextLineId || nextLineId === activeLine?.id) return;
    if (!confirmDiscardPendingCapture("cambiar de artículo")) return;
    setActiveLineId(nextLineId);
  }

  function showLastScanPage(nextEntry) {
    const total = flattenScans(nextEntry).length;
    setScanPage(Math.max(1, Math.ceil(total / SCAN_PAGE_SIZE)));
  }

  async function createDraft() {
    if (!confirmDiscardPendingCapture("crear otro lote")) return;
    await run(
      async () => {
        const data = await postBejermanPurchaseEntry({
          companyKey,
          comprobanteLetra: "R",
          issueDate: todayIso(),
          accountingDate: todayIso(),
          ddjjDate: todayIso(),
        });
        await loadEntries();
        await loadEntry(data.id);
        return data;
      },
      "Borrador creado",
      { focusAfter: false },
    );
  }

  async function saveHeader() {
    if (!entry?.id) return;
    await run(() => persistHeaderDirect(), "Encabezado guardado");
  }

  async function searchProviders() {
    const q = providerQuery.trim();
    if (q.length < MIN_LOOKUP_QUERY_LENGTH) {
      setError(`Ingresá al menos ${MIN_LOOKUP_QUERY_LENGTH} caracteres para buscar proveedores.`);
      setProviders([]);
      return;
    }
    const data = await run(
      () => getBejermanPurchaseProviders({ q, limit: 30, companyKey: activeCompanyKey }),
      "",
      { focusAfter: false },
    );
    if (data) setProviders(data.items || []);
  }

  async function pickProvider(provider) {
    if (purchaseAvailability(provider) === false) {
      setError("Ese proveedor está deshabilitado para el circuito de compras.");
      return;
    }
    const next = {
      ...header,
      supplierCode: provider.code || "",
      supplierCodeRaw: provider.rawCode || provider.code || "",
      supplierName: provider.name || "",
      supplierTaxId: provider.cuit || "",
      paymentTermCode: provider.paymentTermCode || "",
      supplierSnapshot: provider.raw || {},
      provider,
    };
    updateHeader(next);
    if (entry?.id) {
      await run(
        async () => {
          const data = await patchBejermanPurchaseEntry(entry.id, next);
          await refreshCurrent(data);
          setProviders([]);
          return data;
        },
        "Proveedor asignado",
        { focusAfter: false },
      );
    }
  }

  async function searchOrders() {
    if (!header.supplierCode) {
      setOrderError("Elegí un proveedor antes de buscar sus órdenes de compra.");
      setOrders([]);
      return;
    }
    setOrderLoading(true);
    setOrderError("");
    try {
      const data = await getBejermanPurchaseOrders({
        supplierCode: header.supplierCodeRaw || header.supplierCode,
        q: orderQuery,
        limit: 30,
        companyKey: activeCompanyKey,
      });
      setOrders(data.items || []);
      if (!(data.items || []).length) {
        setOrderError("No se encontraron órdenes de compra para este proveedor.");
      }
    } catch (exc) {
      setOrders([]);
      setOrderError(errText(exc));
    } finally {
      setOrderLoading(false);
    }
  }

  async function pickOrder(order) {
    const next = {
      ...header,
      comprobanteTipoOrigen: order.comprobanteTipo || "OC",
      comprobanteLetraOrigen: order.comprobanteLetra || "",
      comprobantePtoVentaOrigen: order.comprobantePtoVenta || "",
      comprobanteNumeroOrigen: order.comprobanteNumero || "",
      comprobanteOrigenLabel: order.label || "",
    };
    updateHeader(next);
    setOrders([]);
    setOrderQuery("");
    if (!entry?.id) return;
    const result = await run(
      async () => {
        await patchBejermanPurchaseEntry(entry.id, next);
        try {
          const populated = await postBejermanPurchaseEntryPopulateFromOrder(entry.id);
          await refreshCurrent(populated);
          return { populatedCount: populated.populatedLineCount || 0 };
        } catch (exc) {
          await refreshCurrent();
          return { populateWarning: errText(exc) };
        }
      },
      "",
      { focusAfter: false },
    );
    if (!result) return;
    if (result.populateWarning) {
      setError(`La OC quedó citada, pero no se pudieron autocompletar los artículos: ${result.populateWarning}`);
    } else if (result.populatedCount > 0) {
      setMessage(
        `Orden de compra citada. Se cargaron ${result.populatedCount} artículo${result.populatedCount === 1 ? "" : "s"} de la OC.`
      );
    } else {
      setMessage("Orden de compra citada, pero no se encontraron artículos para autocompletar.");
    }
  }

  async function clearOrder() {
    const next = {
      ...header,
      comprobanteTipoOrigen: "",
      comprobanteLetraOrigen: "",
      comprobantePtoVentaOrigen: "",
      comprobanteNumeroOrigen: "",
      comprobanteOrigenLabel: "",
    };
    updateHeader(next);
    if (entry?.id) {
      await run(
        async () => {
          const data = await patchBejermanPurchaseEntry(entry.id, next);
          await refreshCurrent(data);
          return data;
        },
        "Orden de compra quitada",
        { focusAfter: false },
      );
    }
  }

  async function searchArticles() {
    if (articleLoading || loading) return;
    await run(
      () => loadArticleSuggestions(articleQuery, { limit: 30, showError: true }),
      "",
      { focusAfter: false },
    );
  }

  function pickArticle(article) {
    if (purchaseAvailability(article) === false) {
      setArticleError("Ese artículo está deshabilitado para el circuito de compras.");
      return;
    }
    const articleCode = article.code || article.id || "";
    setLineForm((current) => ({
      ...current,
      articleCode,
      articleDescription: article.description || article.name || "",
      purchaseEnabled: article.purchaseEnabled ?? null,
      enabled: article.enabled ?? null,
      controlsStock: article.controlsStock ?? null,
      stockEnabled: article.stockEnabled ?? null,
      stockByPartida: article.stockByPartida ?? null,
      requiresPartida: article.requiresPartida ?? null,
      partidaMask: article.partidaMask || "",
    }));
    if (articleCode) {
      setArticleMetadataByCode((current) => ({
        ...current,
        [String(articleCode).trim().toUpperCase()]: {
          purchaseEnabled: article.purchaseEnabled ?? null,
          enabled: article.enabled ?? null,
          controlsStock: article.controlsStock ?? null,
          stockEnabled: article.stockEnabled ?? null,
          stockByPartida: article.stockByPartida ?? null,
          requiresPartida: article.requiresPartida ?? null,
          partidaMask: article.partidaMask || "",
        },
      }));
    }
    setArticles([]);
    setArticleError("");
  }

  async function addLine() {
    if (!entry?.id || !lineForm.articleCode.trim()) return;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await postBejermanPurchaseEntryLine(entry.id, lineForm);
        await refreshCurrent(data, { selectLastLine: true });
        return data;
      },
      "Artículo agregado",
    );
  }

  async function updateActiveLine() {
    if (!entry?.id || !activeLine?.id) return;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await patchBejermanPurchaseEntryLine(entry.id, activeLine.id, {
          defaultConversionFactor: activeDefaults.defaultConversionFactor || activeLine.defaultConversionFactor,
          defaultUnitValue: activeDefaults.defaultUnitValue || activeLine.defaultUnitValue,
        });
        await refreshCurrent(data);
        return data;
      },
      "Valores por defecto actualizados",
    );
  }

  async function deleteLine(lineId) {
    if (!entry?.id || !lineId) return;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await deleteBejermanPurchaseEntryLine(entry.id, lineId);
        await refreshCurrent(data);
        return data;
      },
      "Línea eliminada",
    );
  }

  async function discardEntry() {
    if (!entry?.id) return;
    const confirmed = window.confirm("¿Descartar este lote borrador? Se ocultará de la lista activa.");
    if (!confirmed) return;
    const discardedId = entry.id;
    await run(
      async () => {
        const data = await deleteBejermanPurchaseEntry(discardedId);
        const items = await loadEntries();
        const next = (items || []).find((item) => item.id !== discardedId) || (items || [])[0] || null;
        if (next?.id) {
          await loadEntry(next.id);
        } else {
          setEntry(null);
          setHeader(emptyHeader);
          setHeaderDirty(false);
          setActiveLineId("");
          setValidation(null);
          setVerification(null);
        }
        return data;
      },
      "Lote descartado",
      { focusAfter: false },
    );
  }

  async function submitScan(text) {
    const value = String(text || "").trim();
    if (!entry?.id || !activeLine?.id || !value || loading) return;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await postBejermanPurchaseEntryScan(entry.id, activeLine.id, { barcode: value });
        setScanText("");
        await refreshCurrent(data);
        showLastScanPage(data);
        return data;
      },
      "Escaneo cargado",
    );
  }

  async function submitBulkScans() {
    if (
      !entry?.id ||
      !activeLine?.id ||
      !bulkScanSummary.total ||
      bulkScanSummary.duplicateCount ||
      bulkLimitExceeded ||
      loading
    ) {
      if (bulkLimitExceeded) {
        setError(
          `Cada bloque admite como máximo ${MAX_BULK_SCAN_CODES.toLocaleString("es-AR")} códigos. Dividí la carga antes de continuar.`,
        );
      }
      return;
    }
    const count = bulkScanSummary.total;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await postBejermanPurchaseEntryScan(entry.id, activeLine.id, {
          barcodes: bulkScanSummary.items,
        });
        setBulkScanText("");
        await refreshCurrent(data);
        showLastScanPage(data);
        return data;
      },
      `${count} ${count === 1 ? "código cargado" : "códigos cargados"}`,
    );
  }

  async function addManualQuantity() {
    if (
      !entry?.id ||
      !activeLine?.id ||
      !manualQuantity ||
      loading ||
      activeRequiresPartida !== false ||
      !activeHasPositiveValue
    ) return;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await postBejermanPurchaseEntryScan(entry.id, activeLine.id, { quantity: manualQuantity });
        setManualQuantity("");
        await refreshCurrent(data);
        showLastScanPage(data);
        return data;
      },
      "Cantidad manual cargada",
    );
  }

  async function saveScan(scan) {
    if (!entry?.id || !scan?.id) return;
    const edit = scanEdits[scan.id] || {};
    if (!Object.keys(edit).length) return;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await patchBejermanPurchaseEntryScan(entry.id, scan.id, edit);
        setScanEdits((current) => {
          const next = { ...current };
          delete next[scan.id];
          return next;
        });
        await refreshCurrent(data);
        return data;
      },
      "Bulto actualizado",
    );
  }

  async function deleteScan(scanId) {
    if (!entry?.id || !scanId) return;
    setValidation(null);
    setVerification(null);
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await deleteBejermanPurchaseEntryScan(entry.id, scanId);
        await refreshCurrent(data);
        return data;
      },
      "Bulto eliminado",
    );
  }

  async function validateEntry() {
    if (!entry?.id) return;
    await run(
      async () => {
        await persistHeaderDirect();
        const data = await postBejermanPurchaseEntryValidate(entry.id, { checkRemote: true });
        await loadEntries();
        if (data?.entry) applyEntryState(data.entry);
        setValidation(data);
        setVerification(null);
        if (data?.ok) {
          setMessage("Prevalidación local y catálogos Bejerman completada");
        } else {
          setError("Hay errores para corregir antes de registrar el RT de compra.");
        }
        return data;
      },
      "",
      { refreshOnError: true },
    );
  }

  async function emitEntry() {
    if (!entry?.id) return;
    await run(
      async () => {
        await persistHeaderDirect();
        const preflight = await postBejermanPurchaseEntryValidate(entry.id, { checkRemote: true });
        await loadEntries();
        if (preflight?.entry) applyEntryState(preflight.entry);
        setValidation(preflight);
        setVerification(null);
        if (!preflight?.ok) {
          setError("Hay errores para corregir antes de registrar el RT de compra.");
          return preflight;
        }
        const validatedEntry = preflight.entry || entry;
        const documentTotals = preflight.documentTotals || {};
        const articleLines = articleSummaryLines(validatedEntry);
        const confirmed = window.confirm(
          [
            "¿Registrar RT de compra en Bejerman?",
            "",
            `Proveedor: ${validatedEntry.supplierName || "-"} (${validatedEntry.supplierCode || "-"})`,
            `Documento: ${documentNumber(validatedEntry)}`,
            `Bultos: ${flattenScans(validatedEntry).length}`,
            `Cantidad total: ${quantity(validatedEntry.totalQuantity)}`,
            `Importe neto: ${money(documentTotals.netValue ?? validatedEntry.totalValue)}`,
            `IVA: ${money(documentTotals.taxValue)}`,
            `Total a registrar: ${money(documentTotals.totalValue)} (moneda configurada en Bejerman)`,
            "",
            "Detalle por artículo:",
            ...articleLines,
            "",
            "Perfil: SEPID · COMPRAS · RT/MC · depósito VAL · numeración N · registración R",
            "",
            "La registración actualiza stock en Bejerman.",
          ].join("\n"),
        );
        if (!confirmed) {
          setMessage("Registración cancelada. El lote permanece validado.");
          return preflight;
        }
        const quantityMismatches = (preflight.warnings || []).filter(
          (item) => item.code === "PURCHASE_ORDER_QUANTITY_MISMATCH",
        );
        let confirmedQuantityMismatch = false;
        if (quantityMismatches.length > 0) {
          confirmedQuantityMismatch = window.confirm(
            [
              "Lo escaneado difiere bastante de lo pedido en la OC citada:",
              "",
              ...quantityMismatches.map(
                (item) =>
                  `${item.articleCode}: pedido ${quantity(item.expectedQuantity)} / escaneado ${quantity(item.totalQuantity)}`,
              ),
              "",
              "¿Confirmás registrar el RT de compra igual?",
            ].join("\n"),
          );
          if (!confirmedQuantityMismatch) {
            setMessage("Registración cancelada. El lote permanece validado.");
            return preflight;
          }
        }
        const data = await postBejermanPurchaseEntryEmit(entry.id, {
          confirmed: true,
          preflightFingerprint: preflight.preflightFingerprint,
          ...(confirmedQuantityMismatch ? { confirmedQuantityMismatch: true } : {}),
        });
        await refreshCurrent(data);
        const emittedVerification = purchaseVerificationFrom(data);
        const emittedVerificationStatus = purchaseVerificationStatus(emittedVerification);
        if (emittedVerificationStatus === "confirmed") {
          setMessage(
            `RT de compra ${data.remitoNumber || ""} registrado y verificado en Bejerman`.trim(),
          );
        } else {
          setMessage(
            `Bejerman aceptó el RT de compra ${data.remitoNumber || ""}. La verificación posterior quedó pendiente.`.trim(),
          );
        }
        return data;
      },
      "",
      { refreshOnError: true, focusAfter: false },
    );
  }

  async function verifyEntry() {
    if (!entry?.id || !canVerifyEntry) return;
    await run(
      async () => {
        setVerificationLoading(true);
        setVerificationError("");
        try {
          const data = await postBejermanPurchaseEntryVerify(entry.id);
          await loadEntries();
          if (data?.entry?.id) applyEntryState(data.entry);
          else await loadEntry(entry.id);
          const nextVerification = purchaseVerificationFrom(data);
          setVerification(nextVerification);
          const status = purchaseVerificationStatus(nextVerification);
          const statusMessages = {
            confirmed: "Bejerman confirmó la cabecera RT/MC, el stock en VAL y las partidas.",
            pending: "La verificación posterior sigue pendiente. No vuelvas a registrar el lote.",
            ambiguous: "La verificación encontró diferencias. El lote queda bloqueado para revisión.",
            unavailable: "Bejerman no permitió completar la verificación. Reintentá sin volver a registrar.",
          };
          setMessage(statusMessages[status] || "Verificación completada.");
          return data;
        } catch (exc) {
          setVerificationError(errText(exc));
          throw exc;
        } finally {
          setVerificationLoading(false);
        }
      },
      "",
      { refreshOnError: false, focusAfter: false },
    );
  }

  async function reopenEntry() {
    if (!entry?.id || !canReopenEntry) return;
    const confirmed = window.confirm(
      [
        "¿Reabrir este lote para corregirlo o volver a registrarlo?",
        "",
        "Confirmá únicamente si la verificación indicó que Bejerman NO importó el RT.",
        "Si el RT existiera en Bejerman, reabrirlo podría provocar una duplicación.",
      ].join("\n"),
    );
    if (!confirmed) return;
    await run(
      async () => {
        await postBejermanPurchaseEntryReopen(entry.id, { confirmedNotImported: true });
        await loadEntries();
        await loadEntry(entry.id);
        setMessage("Lote reabierto como borrador.");
      },
      "",
      { refreshOnError: true, focusAfter: false },
    );
  }

  async function loadHistory() {
    const data = await run(
      () => getBejermanPurchaseHistory({ tipo: "RT", limit: 30 }),
      "",
      { focusAfter: false },
    );
    if (data) setHistory(data.items || []);
  }

  async function refreshView() {
    if (!confirmDiscardPendingCapture("actualizar la pantalla")) return;
    await run(() => refreshCurrent(), "Datos actualizados", { focusAfter: false });
  }

  async function selectEntry(entryId) {
    if (!entryId || entryId === entry?.id || loading) return;
    if (headerDirty || hasPendingCapture) {
      const confirmed = window.confirm(
        "Hay cambios, códigos o ediciones sin guardar. ¿Descartarlos y abrir otro lote?",
      );
      if (!confirmed) return;
    }
    await run(() => loadEntry(entryId), "", { focusAfter: false });
  }

  function setScanEdit(scanId, field, value) {
    setScanEdits((current) => ({
      ...current,
      [scanId]: {
        conversionFactor: current[scanId]?.conversionFactor ?? scans.find((scan) => scan.id === scanId)?.conversionFactor,
        unitValue: current[scanId]?.unitValue ?? scans.find((scan) => scan.id === scanId)?.unitValue,
        barcode: current[scanId]?.barcode ?? scans.find((scan) => scan.id === scanId)?.barcode,
        partidaExpirationDate:
          current[scanId]?.partidaExpirationDate ?? scans.find((scan) => scan.id === scanId)?.partidaExpirationDate,
        ...current[scanId],
        [field]: value,
      },
    }));
  }

  const validationErrors = validation?.errors || [];
  const validationWarnings = validation?.warnings || [];

  return (
    <div className={embedded ? "text-gray-900" : "min-h-screen bg-gray-50 p-2 text-gray-900 sm:p-4 md:p-6"}>
      <div className={embedded ? "space-y-4" : "mx-auto max-w-[1600px] space-y-4"}>
        <div className={`flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between ${embedded ? "rounded border border-gray-200 bg-white p-3" : "border-b border-gray-200 pb-3"}`}>
          <div className="min-w-0">
            <h2 className={embedded ? "text-lg font-semibold tracking-normal" : "text-2xl font-semibold tracking-normal"}>Ingresos de mercadería Bejerman</h2>
            <p className="text-sm text-gray-600">
              Circuito COMPRAS · RT/MC · SEPID · depósito VAL · numeración N · registración R.
            </p>
          </div>
          <ResponsiveActionBar>
            <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={loadHistory} disabled={loading}>
              <ClipboardList className="h-4 w-4" />
              Historial RT
            </button>
            <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={refreshView} disabled={loading || headerDirty} title={headerDirty ? "Guardá el encabezado antes de actualizar" : undefined}>
              <RefreshCw className="h-4 w-4" />
              Actualizar
            </button>
            {canManage && (
              <button type="button" className={`inline-flex h-9 items-center gap-2 rounded bg-emerald-700 px-3 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={createDraft} disabled={loading || headerDirty} title={headerDirty ? "Guardá el encabezado antes de crear otro lote" : undefined}>
                <PackagePlus className="h-4 w-4" />
                Nuevo lote
              </button>
            )}
          </ResponsiveActionBar>
        </div>

        {(error || message) && (
          <div role="status" aria-live="polite" className={`rounded border px-3 py-2 text-sm ${error ? "border-rose-200 bg-rose-50 text-rose-800" : "border-emerald-200 bg-emerald-50 text-emerald-800"}`}>
            {error || message}
          </div>
        )}

        <div className="grid gap-3 xl:grid-cols-[300px_minmax(0,1fr)]">
          <aside className="space-y-3 xl:sticky xl:top-4 xl:self-start">
            <div className="rounded border border-gray-200 bg-white">
              <div className="border-b border-gray-200 px-3 py-2 text-sm font-semibold">Lotes</div>
              <MobileSortControl
                sort={entriesSort}
                onSortChange={setEntriesSort}
                options={PURCHASE_SORT_OPTIONS}
                label="Ordenar lotes"
                mobileOnly={false}
                className="border-b border-gray-100 p-2"
              />
              <div className="max-h-56 overflow-y-auto md:max-h-72 xl:max-h-[520px]">
                {entries.length === 0 && <div className="px-3 py-4 text-sm text-gray-500">Sin lotes cargados.</div>}
                {displayEntries.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`block w-full border-b border-gray-100 px-3 py-2 text-left hover:bg-gray-50 ${entry?.id === item.id ? "bg-emerald-50" : "bg-white"}`}
                    onClick={() => selectEntry(item.id)}
                    disabled={loading}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{item.remitoNumber || `${item.comprobanteTipo || "RT"} ${item.comprobantePtoVenta || "-"}-${item.comprobanteNumero || "-"}`}</span>
                      <StatusBadge status={item.status} />
                    </div>
                    <div className="mt-1 truncate text-xs text-gray-600">{item.supplierName || "Proveedor sin asignar"}</div>
                    <div className="mt-1 flex justify-between text-xs text-gray-500">
                      <span>{item.issueDate || "-"}</span>
                      <span>Importe {money(item.totalValue)}</span>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            {history.length > 0 && (
              <div className="rounded border border-gray-200 bg-white">
                <div className="border-b border-gray-200 px-3 py-2 text-sm font-semibold">Historial Bejerman</div>
                <MobileSortControl
                  sort={historySort}
                  onSortChange={setHistorySort}
                  options={PURCHASE_SORT_OPTIONS.filter((option) => option.value !== "estado")}
                  label="Ordenar historial"
                  mobileOnly={false}
                  className="border-b border-gray-100 p-2"
                />
                <div className="max-h-56 overflow-y-auto md:max-h-80">
                  {displayHistory.map((item, index) => (
                    <div key={`${item.remitoNumber}-${index}`} className="border-b border-gray-100 px-3 py-2 text-xs">
                      <div className="font-medium text-gray-900">{item.remitoNumber}</div>
                      <div className="truncate text-gray-600">{item.supplierName || item.supplierCode}</div>
                      <div className="flex justify-between text-gray-500">
                        <span>{item.issueDate || "-"}</span>
                        <span>Importe {money(item.totalValue)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </aside>

          <main className="min-w-0 space-y-3 md:space-y-4">
            {!entry && (
              <div className="rounded border border-gray-200 bg-white px-4 py-8 text-center text-sm text-gray-600">
                {loading ? "Cargando..." : "Creá un lote para empezar a escanear mercadería."}
              </div>
            )}

            {entry && (
              <>
                <section className="rounded border border-gray-200 bg-white p-2 sm:p-3">
                  <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusBadge status={entry.status} />
                      <span className="text-sm text-gray-600">Lote {entry.id}</span>
                      {entry.remitoNumber && <span className="text-sm font-semibold text-emerald-700">{entry.remitoNumber}</span>}
                      {headerDirty && (
                        <span className="rounded border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-800">
                          Encabezado sin guardar
                        </span>
                      )}
                    </div>
                    <ResponsiveActionBar>
                      {canManage && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={saveHeader} disabled={loading || isLocked || !headerDirty}>
                          <Save className="h-4 w-4" />
                          Guardar encabezado
                        </button>
                      )}
                      {canManage && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-rose-200 bg-white px-3 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={discardEntry} disabled={loading || isLocked}>
                          <Trash2 className="h-4 w-4" />
                          Descartar
                        </button>
                      )}
                      {(canManage || canEmit) && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-emerald-300 bg-emerald-50 px-3 text-sm text-emerald-800 hover:bg-emerald-100 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={validateEntry} disabled={loading || isLocked}>
                          <CheckCircle2 className="h-4 w-4" />
                          Validar con Bejerman
                        </button>
                      )}
                      {canEmit && (
                        <button
                          type="button"
                          className={`inline-flex h-9 items-center gap-2 rounded bg-emerald-700 px-3 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50 ${fullWidthButtonClass}`}
                          onClick={emitEntry}
                          disabled={loading || isLocked || scans.length === 0 || !emissionEnabled}
                          title={!emissionEnabled ? "La registración está deshabilitada en este entorno" : undefined}
                        >
                          <Send className="h-4 w-4" />
                          Registrar RT de compra
                        </button>
                      )}
                      {canVerifyEntry && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-blue-300 bg-blue-50 px-3 text-sm text-blue-800 hover:bg-blue-100 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={verifyEntry} disabled={loading || verificationLoading}>
                          <RefreshCw className={`h-4 w-4 ${verificationLoading ? "animate-spin" : ""}`} />
                          {verificationLoading ? "Verificando en Bejerman..." : "Verificar en Bejerman"}
                        </button>
                      )}
                      {canReopenEntry && (
                        <button type="button" className={`inline-flex h-9 items-center gap-2 rounded border border-amber-300 bg-amber-50 px-3 text-sm text-amber-900 hover:bg-amber-100 disabled:opacity-50 ${fullWidthButtonClass}`} onClick={reopenEntry} disabled={loading}>
                          <RefreshCw className="h-4 w-4" />
                          Reabrir lote
                        </button>
                      )}
                    </ResponsiveActionBar>
                  </div>

                  {entry.lastError && (
                    <div className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                      <div className="font-semibold">Último error registrado</div>
                      <div className="mt-1 whitespace-pre-wrap break-words">{entry.lastError}</div>
                    </div>
                  )}

                  {showVerificationPanel && (
                    <PurchaseVerificationPanel
                      verification={displayVerification}
                      entryStatus={entry.status}
                      error={verificationError}
                    />
                  )}

                  <div className="mt-3 grid gap-3 2xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
                    <div className="space-y-2">
                      <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
                        <input
                          className="h-9 rounded border border-gray-300 px-2 text-sm"
                          value={providerQuery}
                          onChange={(event) => {
                            setProviderQuery(event.target.value);
                            setProviders([]);
                          }}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              searchProviders();
                            }
                          }}
                          placeholder="Buscar proveedor por código, CUIT o razón social"
                          aria-label="Buscar proveedor Bejerman"
                          disabled={loading || isLocked || !canManage}
                        />
                        <button type="button" className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-50" onClick={searchProviders} disabled={loading || isLocked || !canManage || providerQuery.trim().length < MIN_LOOKUP_QUERY_LENGTH}>
                          <Search className="h-4 w-4" />
                          Buscar
                        </button>
                      </div>
                      {providers.length > 0 && (
                        <div className="max-h-40 overflow-y-auto rounded border border-gray-200">
                          {providers.map((provider) => {
                            const availability = purchaseAvailability(provider);
                            return (
                              <button
                                key={`${provider.rawCode}-${provider.name}`}
                                type="button"
                                className="block w-full border-b border-gray-100 px-3 py-2 text-left text-sm hover:bg-gray-50 disabled:cursor-not-allowed disabled:bg-gray-50 disabled:opacity-60"
                                onClick={() => pickProvider(provider)}
                                disabled={loading || isLocked || !canManage || availability === false}
                              >
                                <div>
                                  <span className="font-mono text-xs text-gray-600">{provider.code}</span>
                                  <span className="ml-2 font-medium">{provider.name}</span>
                                  {provider.cuit && <span className="ml-2 text-xs text-gray-500">{provider.cuit}</span>}
                                </div>
                                {availability !== null && (
                                  <div className={`mt-1 text-xs font-medium ${availability ? "text-emerald-700" : "text-rose-700"}`}>
                                    {availability ? "Habilitado en Compras" : "Deshabilitado en Compras"}
                                  </div>
                                )}
                              </button>
                            );
                          })}
                        </div>
                      )}
                      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-[120px_1fr_160px_130px]">
                        <input
                          className="h-9 rounded border border-gray-300 px-2 text-sm"
                          value={header.supplierCode}
                          onChange={(event) => updateHeader({
                            ...header,
                            supplierCode: event.target.value,
                            supplierCodeRaw: event.target.value,
                            supplierSnapshot: {},
                            provider: null,
                          })}
                          placeholder="Código"
                          aria-label="Código de proveedor"
                          disabled={loading || isLocked || !canManage}
                        />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={header.supplierName} onChange={(event) => updateHeaderField("supplierName", event.target.value)} placeholder="Proveedor" aria-label="Razón social del proveedor" disabled={loading || isLocked || !canManage} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={header.supplierTaxId} onChange={(event) => updateHeaderField("supplierTaxId", event.target.value)} placeholder="CUIT" aria-label="CUIT del proveedor" disabled={loading || isLocked || !canManage} />
                        <input className="h-9 rounded border border-gray-300 px-2 text-sm" value={header.paymentTermCode} onChange={(event) => updateHeaderField("paymentTermCode", event.target.value)} placeholder="Cond. pago (opcional)" aria-label="Condición de pago" disabled={loading || isLocked || !canManage} />
                      </div>
                    </div>

                    <div className="grid gap-2 sm:grid-cols-3">
                      <label className="text-xs font-medium text-gray-600">
                        Letra
                        <input className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.comprobanteLetra} onChange={(event) => updateHeaderField("comprobanteLetra", event.target.value.toUpperCase())} disabled={loading || isLocked || !canManage} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Punto
                        <input className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.comprobantePtoVenta} onChange={(event) => updateHeaderField("comprobantePtoVenta", event.target.value)} inputMode="numeric" disabled={loading || isLocked || !canManage} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Número
                        <input className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.comprobanteNumero} onChange={(event) => updateHeaderField("comprobanteNumero", event.target.value)} inputMode="numeric" disabled={loading || isLocked || !canManage} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Fecha
                        <input type="date" className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.issueDate} onChange={(event) => updateHeader({ ...header, issueDate: event.target.value, accountingDate: event.target.value, ddjjDate: event.target.value })} disabled={loading || isLocked || !canManage} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        Contable
                        <input type="date" className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.accountingDate} onChange={(event) => updateHeaderField("accountingDate", event.target.value)} disabled={loading || isLocked || !canManage} />
                      </label>
                      <label className="text-xs font-medium text-gray-600">
                        DDJJ
                        <input type="date" className="mt-1 h-9 w-full rounded border border-gray-300 px-2 text-sm" value={header.ddjjDate} onChange={(event) => updateHeaderField("ddjjDate", event.target.value)} disabled={loading || isLocked || !canManage} />
                      </label>
                    </div>
                  </div>

                  <div className="mt-3 rounded border border-gray-200 bg-gray-50 p-3">
                    <div className="mb-2 text-xs font-semibold uppercase text-gray-600">Orden de compra</div>
                    {header.comprobanteNumeroOrigen ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-sm font-medium text-emerald-800">
                          {header.comprobanteOrigenLabel ||
                            `${header.comprobanteTipoOrigen} ${header.comprobanteLetraOrigen} ${header.comprobantePtoVentaOrigen}-${header.comprobanteNumeroOrigen}`}
                        </span>
                        <span className="text-xs text-gray-500">Los artículos, cantidades y precios de esta OC ya se cargaron abajo.</span>
                        <button
                          type="button"
                          className="inline-flex h-8 items-center gap-1 rounded border border-rose-200 bg-white px-2 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                          onClick={clearOrder}
                          disabled={loading || isLocked || !canManage}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          Quitar OC citada
                        </button>
                      </div>
                    ) : (
                      <>
                        <div className="mb-2 text-xs text-gray-500">
                          Elegí una OC para autocompletar sus artículos, cantidades pedidas y precios.
                        </div>
                        <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
                          <input
                            className="h-9 rounded border border-gray-300 px-2 text-sm disabled:bg-gray-100"
                            value={orderQuery}
                            onChange={(event) => {
                              setOrderQuery(event.target.value);
                              setOrders([]);
                              setOrderError("");
                            }}
                            onKeyDown={(event) => {
                              if (event.key === "Enter") {
                                event.preventDefault();
                                searchOrders();
                              }
                            }}
                            placeholder={
                              header.supplierCode
                                ? "Buscar OC por número"
                                : "Elegí un proveedor para buscar sus órdenes de compra"
                            }
                            aria-label="Buscar orden de compra Bejerman"
                            disabled={loading || isLocked || !canManage || !header.supplierCode}
                          />
                          <button
                            type="button"
                            className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-50"
                            onClick={searchOrders}
                            disabled={loading || isLocked || !canManage || !header.supplierCode || orderLoading}
                          >
                            <Search className="h-4 w-4" />
                            Buscar
                          </button>
                        </div>
                        {orderLoading && (
                          <div className="mt-2 text-xs text-gray-500">Buscando órdenes de compra...</div>
                        )}
                        {orderError && !orderLoading && (
                          <div className="mt-2 text-xs text-rose-700">{orderError}</div>
                        )}
                        {orders.length > 0 && (
                          <div className="mt-2 max-h-40 overflow-y-auto rounded border border-gray-200">
                            {orders.map((order) => (
                              <button
                                key={`${order.comprobanteLetra}-${order.comprobantePtoVenta}-${order.comprobanteNumero}`}
                                type="button"
                                className="block w-full border-b border-gray-100 px-3 py-2 text-left text-sm hover:bg-gray-50 disabled:opacity-50"
                                onClick={() => pickOrder(order)}
                                disabled={loading || isLocked || !canManage}
                              >
                                <div className="font-medium">{order.label}</div>
                                <div className="text-xs text-gray-500">
                                  {order.issueDate ? `Emitida ${order.issueDate}` : ""}
                                  {order.totalValue != null ? ` · Importe ${money(order.totalValue)}` : ""}
                                </div>
                              </button>
                            ))}
                          </div>
                        )}
                      </>
                    )}
                  </div>

                  <textarea className="mt-3 min-h-16 w-full rounded border border-gray-300 px-2 py-2 text-sm" value={header.notes} onChange={(event) => updateHeaderField("notes", event.target.value)} placeholder="Observaciones para auditoría" aria-label="Observaciones para auditoría" disabled={loading || isLocked || !canManage} />
                </section>

                <section className="rounded border border-gray-200 bg-white p-3">
                  <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(420px,0.42fr)]">
                    <div className="min-w-0">
                      <div className="grid gap-2 lg:grid-cols-[minmax(320px,1fr)_auto]">
                        <input
                          className="h-9 rounded border border-gray-300 px-2 text-sm"
                          value={articleQuery}
                          onChange={(event) => {
                            setArticleQuery(event.target.value);
                            setArticles([]);
                            setArticleError("");
                          }}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              searchArticles();
                            }
                          }}
                          placeholder="Buscar artículo Bejerman por código o descripción"
                          aria-label="Buscar artículo Bejerman"
                          disabled={loading || isLocked || !canManage}
                          autoComplete="off"
                        />
                        <button type="button" className="inline-flex h-9 items-center justify-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-50" onClick={searchArticles} disabled={loading || articleLoading || isLocked || !canManage || articleQuery.trim().length < MIN_LOOKUP_QUERY_LENGTH}>
                          <Search className="h-4 w-4" />
                          Buscar
                        </button>
                      </div>
                      {(articles.length > 0 || articleLoading || articleError) && (
                        <div className="mt-2 max-h-44 overflow-y-auto rounded border border-gray-200">
                          {articleLoading && (
                            <div className="px-3 py-2 text-sm text-gray-500">Buscando artículos...</div>
                          )}
                          {articleError && !articleLoading && (
                            <div className="px-3 py-2 text-sm text-rose-700">{articleError}</div>
                          )}
                          {articles.map((article) => {
                            const availability = purchaseAvailability(article);
                            const articleRequiresPartida = requiresPartida(article);
                            return (
                              <button
                                key={`${article.code}-${article.name}`}
                                type="button"
                                className="block w-full border-b border-gray-100 px-3 py-2 text-left hover:bg-gray-50 disabled:cursor-not-allowed disabled:bg-gray-50 disabled:opacity-60"
                                onClick={() => pickArticle(article)}
                                disabled={loading || isLocked || !canManage || availability === false}
                              >
                                <div className="font-mono text-xs text-gray-600">{article.code || article.id}</div>
                                <div className="text-sm font-medium text-gray-900">{article.description || article.name}</div>
                                {(availability !== null || articleRequiresPartida !== null) && (
                                  <div className="mt-1 flex flex-wrap gap-2 text-xs">
                                    {availability !== null && (
                                      <span className={availability ? "font-medium text-emerald-700" : "font-medium text-rose-700"}>
                                        {availability ? "Habilitado en Compras" : "Deshabilitado en Compras"}
                                      </span>
                                    )}
                                    {articleRequiresPartida !== null && (
                                      <span className={articleRequiresPartida ? "font-medium text-blue-700" : "text-gray-500"}>
                                        {articleRequiresPartida ? "Stock por partida/serie" : "Sin control por partida"}
                                      </span>
                                    )}
                                  </div>
                                )}
                              </button>
                            );
                          })}
                        </div>
                      )}
                      <div className="mt-2 flex flex-wrap gap-2">
                        <input
                          className="h-9 min-w-[110px] flex-1 basis-32 rounded border border-gray-300 px-2 font-mono text-sm"
                          value={lineForm.articleCode}
                          onChange={(event) => setLineForm({
                            ...lineForm,
                            articleCode: event.target.value,
                            purchaseEnabled: null,
                            enabled: null,
                            controlsStock: null,
                            stockEnabled: null,
                            stockByPartida: null,
                            requiresPartida: null,
                            partidaMask: "",
                          })}
                          placeholder="Artículo"
                          aria-label="Código de artículo"
                          disabled={loading || isLocked || !canManage}
                        />
                        <input className="h-9 min-w-[160px] flex-[2] basis-56 rounded border border-gray-300 px-2 text-sm" value={lineForm.articleDescription} onChange={(e) => setLineForm({ ...lineForm, articleDescription: e.target.value })} placeholder="Descripción" aria-label="Descripción del artículo" disabled={loading || isLocked || !canManage} />
                        <input className="h-9 min-w-[80px] flex-1 basis-24 rounded border border-gray-300 px-2 text-sm" value={lineForm.defaultConversionFactor} onChange={(e) => setLineForm({ ...lineForm, defaultConversionFactor: e.target.value })} placeholder="Factor" aria-label="Factor por defecto" inputMode="decimal" disabled={loading || isLocked || !canManage} />
                        <input className="h-9 min-w-[100px] flex-1 basis-28 rounded border border-gray-300 px-2 text-sm" value={lineForm.defaultUnitValue} onChange={(e) => setLineForm({ ...lineForm, defaultUnitValue: e.target.value })} placeholder="Valor" aria-label="Valor unitario por defecto" inputMode="decimal" disabled={loading || isLocked || !canManage} />
                        <button
                          type="button"
                          className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded bg-emerald-700 px-3 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50"
                          onClick={addLine}
                          disabled={
                            loading ||
                            isLocked ||
                            !canManage ||
                            !lineForm.articleCode.trim() ||
                            purchaseAvailability(lineForm) !== true ||
                            !(numericInput(lineForm.defaultUnitValue) > 0)
                          }
                          title={
                            purchaseAvailability(lineForm) !== true
                              ? "Seleccioná un artículo habilitado desde el catálogo Bejerman"
                              : !(numericInput(lineForm.defaultUnitValue) > 0)
                                ? "Cargá un valor unitario mayor a cero"
                                : undefined
                          }
                        >
                          <PackagePlus className="h-4 w-4" />
                          Agregar
                        </button>
                      </div>
                    </div>

                    <div className="min-w-0 rounded border border-gray-200 bg-gray-50 p-3">
                      <div className="mb-2 text-sm font-semibold">Línea activa</div>
                      {activeLine ? (
                        <div className="space-y-2">
                          <select className="h-9 w-full rounded border border-gray-300 px-2 text-sm" value={activeLine.id} onChange={(e) => selectActiveLine(e.target.value)} disabled={loading}>
                            {(entry.lines || []).map((line) => (
                              <option key={line.id} value={line.id}>
                                {line.articleCode} - {line.articleDescription || "Sin descripción"}
                                {line.expectedQuantity != null ? ` (pedido ${quantity(line.expectedQuantity)})` : ""}
                              </option>
                            ))}
                          </select>
                          <div className="rounded border border-gray-200 bg-white p-2">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-mono text-xs text-gray-600">{activeLine.articleCode}</span>
                              {activeLine.expectedQuantity != null && (
                                <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-800">
                                  De la OC citada
                                </span>
                              )}
                            </div>
                            <div className="mt-0.5 break-words text-sm font-medium text-gray-900">
                              {activeLine.articleDescription || "Sin descripción"}
                            </div>
                            {activeLine.expectedQuantity != null && (
                              <div className="mt-1 text-xs text-gray-600">
                                Pedido en la OC: <span className="font-medium text-gray-900">{quantity(activeLine.expectedQuantity)}</span>
                                {" · "}Escaneado: <span className="font-medium text-gray-900">{quantity(activeLine.totalQuantity)}</span>
                                {Number(activeLine.expectedQuantity) > Number(activeLine.totalQuantity || 0) && (
                                  <>
                                    {" · "}Falta:{" "}
                                    <span className="font-medium text-amber-700">
                                      {quantity(activeLine.expectedQuantity - Number(activeLine.totalQuantity || 0))}
                                    </span>
                                  </>
                                )}
                              </div>
                            )}
                            {activeRequiresPartida !== null && (
                              <div className={`mt-1 text-xs font-medium ${activeRequiresPartida ? "text-blue-700" : "text-gray-500"}`}>
                                {activeRequiresPartida
                                  ? "Controla stock por partida/serie"
                                  : "No controla stock por partida"}
                              </div>
                            )}
                          </div>
                          <p className="text-xs text-gray-500">
                            Estos dos valores se van a aplicar a cada bulto nuevo que escanees de acá en adelante (no cambian los bultos que ya cargaste).
                          </p>
                          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[minmax(100px,1fr)_minmax(120px,1fr)_44px]">
                            <label className="block text-xs">
                              <span className="mb-1 block font-semibold uppercase text-gray-500">Factor</span>
                              <input className="h-9 w-full rounded border border-gray-300 px-2 text-sm" value={activeDefaults.defaultConversionFactor} onChange={(e) => setActiveDefaults({ ...activeDefaults, defaultConversionFactor: e.target.value })} placeholder={`Factor ${activeLine.defaultConversionFactor}`} aria-label="Factor por defecto de la línea activa" inputMode="decimal" disabled={loading || isLocked || !canManage} />
                            </label>
                            <label className="block text-xs">
                              <span className="mb-1 block font-semibold uppercase text-gray-500">Valor unitario</span>
                              <input className="h-9 w-full rounded border border-gray-300 px-2 text-sm" value={activeDefaults.defaultUnitValue} onChange={(e) => setActiveDefaults({ ...activeDefaults, defaultUnitValue: e.target.value })} placeholder={`Valor ${activeLine.defaultUnitValue}`} aria-label="Valor unitario de la línea activa" inputMode="decimal" disabled={loading || isLocked || !canManage} />
                            </label>
                            <button type="button" className="inline-flex h-9 w-9 items-center justify-center self-end rounded border border-gray-300 bg-white hover:bg-gray-50 disabled:opacity-50" onClick={updateActiveLine} disabled={loading || isLocked || !canManage} title="Actualizar valores de la línea">
                              <Save className="h-4 w-4" />
                            </button>
                          </div>
                          <button type="button" className="inline-flex h-8 items-center gap-2 rounded border border-rose-200 bg-white px-2 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50" onClick={() => deleteLine(activeLine.id)} disabled={loading || isLocked || !canManage}>
                            <Trash2 className="h-3.5 w-3.5" />
                            Eliminar línea
                          </button>
                        </div>
                      ) : (
                        <div className="text-sm text-gray-600">Agregá un artículo para habilitar el escáner.</div>
                      )}
                    </div>
                  </div>
                </section>

                <section className="rounded border border-gray-200 bg-white p-2 sm:p-3">
                  {activeLine && !activeHasPositiveValue && (
                    <div className="mb-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                      Cargá un valor unitario mayor a cero y guardá los valores de la línea antes de escanear.
                    </div>
                  )}
                  <div className="grid gap-3 2xl:grid-cols-[minmax(0,1fr)_minmax(420px,0.42fr)]">
                    <div>
                      <label className="text-xs font-semibold uppercase text-gray-500">
                        {activeRequiresPartida ? "Escáner de partida/serie" : "Escáner"}
                      </label>
                      <input
                        ref={scannerRef}
                        className="mt-1 h-12 w-full rounded border border-emerald-500 px-3 font-mono text-lg outline-none ring-2 ring-transparent focus:ring-emerald-200 disabled:border-gray-300 disabled:bg-gray-100"
                        value={scanText}
                        onChange={(event) => setScanText(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") {
                            event.preventDefault();
                            submitScan(scanText);
                          }
                        }}
                        placeholder={
                          activeLine
                            ? activeRequiresPartida
                              ? "Escanear una partida/serie y presionar Enter"
                              : "Escanear un código y presionar Enter"
                            : "Seleccioná una línea activa"
                        }
                        disabled={loading || isLocked || !activeLine || !canManage || !activeHasPositiveValue}
                      />
                      {activeRequiresPartida === true && (
                        <p className="mt-1 text-xs font-medium text-blue-700">
                          Cada código escaneado se enviará como partida/serie del artículo en Bejerman.
                        </p>
                      )}
                      {activeLine && activeRequiresPartida === null && (
                        <div className={`mt-1 flex flex-wrap items-center gap-2 text-xs font-medium ${activeArticleMetadataError ? "text-rose-700" : "text-amber-700"}`}>
                          <span>
                            {activeArticleMetadataError || "Verificando en Bejerman si el artículo controla partida."}
                          </span>
                          {activeArticleMetadataError && (
                            <button
                              type="button"
                              className="rounded border border-rose-300 bg-white px-2 py-1 text-rose-700 hover:bg-rose-50"
                              onClick={() => setActiveArticleMetadataRetry((current) => current + 1)}
                              disabled={loading}
                            >
                              Reintentar
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                    <div>
                      <label className="text-xs font-semibold uppercase text-gray-500">Cantidad sin código</label>
                      <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_auto]">
                        <input className="h-12 rounded border border-gray-300 px-3 text-sm" value={manualQuantity} onChange={(e) => setManualQuantity(e.target.value)} placeholder={activeRequiresPartida === false ? "Cantidad" : "No disponible hasta verificar el control de partida"} inputMode="decimal" disabled={loading || isLocked || !activeLine || !canManage || activeRequiresPartida !== false || !activeHasPositiveValue} />
                        <button type="button" className="inline-flex h-12 items-center justify-center gap-2 rounded border border-gray-300 bg-white px-3 text-sm hover:bg-gray-50 disabled:opacity-50" onClick={addManualQuantity} disabled={loading || isLocked || !activeLine || !manualQuantity || !canManage || activeRequiresPartida !== false || !activeHasPositiveValue}>
                          <PackagePlus className="h-4 w-4" />
                          Agregar
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="mt-3 rounded border border-gray-200 bg-gray-50 p-3">
                    <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                      <div>
                        <label htmlFor="purchase-bulk-barcodes" className="text-xs font-semibold uppercase text-gray-600">
                          {activeRequiresPartida ? "Carga masiva de partidas/series" : "Carga masiva de códigos"}
                        </label>
                        <p className="text-xs text-gray-500">
                          Pegá hasta {MAX_BULK_SCAN_CODES.toLocaleString("es-AR")} códigos separados por salto de línea, tabulación, coma o punto y coma.
                        </p>
                      </div>
                      <div className="text-xs text-gray-600">
                        Detectados: <strong>{bulkScanSummary.total}</strong>
                        {" · "}
                        Únicos: <strong>{bulkScanSummary.unique}</strong>
                        {bulkScanSummary.duplicateCount > 0 && (
                          <span className="ml-1 font-semibold text-rose-700">
                            · Repetidos: {bulkScanSummary.duplicateCount}
                          </span>
                        )}
                      </div>
                    </div>
                    <textarea
                      id="purchase-bulk-barcodes"
                      className="mt-2 min-h-28 w-full rounded border border-gray-300 bg-white px-3 py-2 font-mono text-sm disabled:bg-gray-100"
                      value={bulkScanText}
                      onChange={(event) => setBulkScanText(event.target.value)}
                      placeholder={"NS0001\nNS0002\nNS0003"}
                      disabled={loading || isLocked || !activeLine || !canManage || !activeHasPositiveValue}
                    />
                    {bulkScanSummary.duplicateCount > 0 && (
                      <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2 py-1.5 text-xs text-rose-800">
                        Eliminá los repetidos del pegado antes de cargar:
                        {" "}
                        {bulkScanSummary.duplicates.slice(0, 5).join(", ")}
                        {bulkScanSummary.duplicates.length > 5 ? "…" : ""}
                      </div>
                    )}
                    {bulkLimitExceeded && (
                      <div className="mt-2 rounded border border-rose-200 bg-rose-50 px-2 py-1.5 text-xs font-medium text-rose-800">
                        El bloque supera el máximo de {MAX_BULK_SCAN_CODES.toLocaleString("es-AR")} códigos. Dividilo antes de cargar.
                      </div>
                    )}
                    <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                      <div className="text-xs text-gray-600">
                        {activeLine
                          ? activeRequiresPartida
                            ? `Cada código se enviará como partida/serie de ${activeLine.articleCode} - ${activeLine.articleDescription || "Sin descripción"}.`
                            : `Los códigos se asignarán a ${activeLine.articleCode} - ${activeLine.articleDescription || "Sin descripción"}.`
                          : "Seleccioná una línea activa."}
                      </div>
                      <button
                        type="button"
                        className="inline-flex h-10 items-center justify-center gap-2 rounded bg-emerald-700 px-4 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-50"
                        onClick={submitBulkScans}
                        disabled={
                          loading ||
                          isLocked ||
                          !activeLine ||
                          !canManage ||
                          !activeHasPositiveValue ||
                          bulkScanSummary.total === 0 ||
                          bulkScanSummary.duplicateCount > 0 ||
                          bulkLimitExceeded
                        }
                      >
                        <PackagePlus className="h-4 w-4" />
                        Cargar {bulkScanSummary.total || 0} {bulkScanSummary.total === 1 ? "código" : "códigos"}
                      </button>
                    </div>
                  </div>
                </section>

                <section className="rounded border border-gray-200 bg-white">
                  <div className="flex flex-col gap-2 border-b border-gray-200 px-3 py-2 md:flex-row md:items-center md:justify-between">
                    <div className="text-sm font-semibold">Bultos cargados</div>
                    <div className="flex flex-wrap gap-2 text-sm">
                      <span className="rounded border border-gray-200 bg-gray-50 px-2 py-1">Bultos {scans.length}</span>
                      <span className="rounded border border-gray-200 bg-gray-50 px-2 py-1">Unidades {quantity(entry.totalQuantity)}</span>
                      <span className="rounded border border-gray-200 bg-gray-50 px-2 py-1">Importe neto {money(entry.totalValue)}</span>
                    </div>
                  </div>
                  {scanPageCount > 1 && (
                    <div className="flex flex-col gap-2 border-b border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-700 sm:flex-row sm:items-center sm:justify-between">
                      <span>
                        Mostrando {(scanPage - 1) * SCAN_PAGE_SIZE + 1}–
                        {Math.min(scanPage * SCAN_PAGE_SIZE, scans.length)} de {scans.length}
                      </span>
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          className="h-8 rounded border border-gray-300 bg-white px-3 disabled:opacity-50"
                          onClick={() => setScanPage((current) => Math.max(1, current - 1))}
                          disabled={scanPage <= 1}
                        >
                          Anterior
                        </button>
                        <span>Página {scanPage} de {scanPageCount}</span>
                        <button
                          type="button"
                          className="h-8 rounded border border-gray-300 bg-white px-3 disabled:opacity-50"
                          onClick={() => setScanPage((current) => Math.min(scanPageCount, current + 1))}
                          disabled={scanPage >= scanPageCount}
                        >
                          Siguiente
                        </button>
                      </div>
                    </div>
                  )}
                  <MobileDataList className="p-3">
                    {scans.length === 0 && (
                      <MobileDataCard className="text-center text-gray-500">
                        Sin bultos cargados.
                      </MobileDataCard>
                    )}
                    {visibleScans.map((scan) => {
                      const edit = scanEdits[scan.id] || {};
                      return (
                        <MobileDataCard key={scan.id} className="space-y-3">
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="font-mono text-xs text-gray-600">{scan.articleCode}</div>
                              <div className="break-words font-medium text-gray-900">{scan.articleDescription || "-"}</div>
                            </div>
                            <span className="shrink-0 rounded border border-gray-200 bg-gray-50 px-2 py-1 text-xs text-gray-600">
                              {scan.isManualQuantity ? "Manual" : "Escaneo"}
                            </span>
                          </div>

                          <label className="block">
                            <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Código</span>
                            <input
                              className="h-10 w-full rounded border border-gray-300 px-2 font-mono text-sm disabled:bg-gray-100"
                              value={edit.barcode ?? scan.barcode ?? ""}
                              onChange={(e) => setScanEdit(scan.id, "barcode", e.target.value)}
                              onBlur={() => saveScan(scan)}
                              disabled={loading || isLocked || !canManage || scan.isManualQuantity}
                            />
                          </label>

                          <div className="grid grid-cols-2 gap-2">
                            <label className="block">
                              <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Factor</span>
                              <input
                                className="h-10 w-full rounded border border-gray-300 px-2 text-right text-sm"
                                 value={edit.conversionFactor ?? scan.conversionFactor}
                                 onChange={(e) => setScanEdit(scan.id, "conversionFactor", e.target.value)}
                                 onBlur={() => saveScan(scan)}
                                 inputMode="decimal"
                                 disabled={loading || isLocked || !canManage}
                              />
                            </label>
                            <label className="block">
                              <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Valor unit.</span>
                              <input
                                className="h-10 w-full rounded border border-gray-300 px-2 text-right text-sm"
                                 value={edit.unitValue ?? scan.unitValue}
                                 onChange={(e) => setScanEdit(scan.id, "unitValue", e.target.value)}
                                 onBlur={() => saveScan(scan)}
                                 inputMode="decimal"
                                 disabled={loading || isLocked || !canManage}
                              />
                            </label>
                          </div>

                          <label className="block">
                            <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Vencimiento</span>
                            <input
                              type="date"
                              className="h-10 w-full rounded border border-gray-300 px-2 text-sm"
                              value={edit.partidaExpirationDate ?? scan.partidaExpirationDate ?? ""}
                              onChange={(e) => setScanEdit(scan.id, "partidaExpirationDate", e.target.value)}
                              onBlur={() => saveScan(scan)}
                              disabled={loading || isLocked || !canManage}
                            />
                          </label>

                          <div className="flex items-center justify-between gap-3 border-t border-gray-100 pt-2">
                            <div>
                              <div className="text-[11px] font-semibold uppercase text-gray-500">Total</div>
                              <div className="font-semibold text-gray-900">{money(scan.totalValue)}</div>
                            </div>
                            <button
                              type="button"
                              className="inline-flex h-10 items-center justify-center gap-2 rounded border border-rose-200 px-3 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                              onClick={() => deleteScan(scan.id)}
                              disabled={loading || isLocked || !canManage}
                            >
                              <Trash2 className="h-4 w-4" />
                              Eliminar
                            </button>
                          </div>
                        </MobileDataCard>
                      );
                    })}
                  </MobileDataList>
                  <DesktopTableWrap>
                    <table className="min-w-full text-left text-sm">
                      <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                        <tr>
                          <th className="px-3 py-2">Código</th>
                          <th className="px-3 py-2">Artículo</th>
                          <th className="px-3 py-2">Factor</th>
                          <th className="px-3 py-2">Valor unit.</th>
                          <th className="px-3 py-2">Vencimiento</th>
                          <th className="px-3 py-2">Total</th>
                          <th className="px-3 py-2">Modo</th>
                          <th className="px-3 py-2"></th>
                        </tr>
                      </thead>
                      <tbody>
                        {scans.length === 0 && (
                          <tr>
                            <td colSpan={8} className="px-3 py-6 text-center text-gray-500">
                              Sin bultos cargados.
                            </td>
                          </tr>
                        )}
                        {visibleScans.map((scan) => {
                          const edit = scanEdits[scan.id] || {};
                          return (
                            <tr key={scan.id} className="border-t border-gray-100">
                              <td className="px-3 py-2">
                                <input className="h-8 w-48 rounded border border-gray-300 px-2 font-mono text-xs" value={edit.barcode ?? scan.barcode ?? ""} onChange={(e) => setScanEdit(scan.id, "barcode", e.target.value)} onBlur={() => saveScan(scan)} disabled={loading || isLocked || !canManage || scan.isManualQuantity} />
                              </td>
                              <td className="px-3 py-2">
                                <div className="font-mono text-xs text-gray-600">{scan.articleCode}</div>
                                <div className="max-w-sm truncate text-gray-900">{scan.articleDescription}</div>
                              </td>
                              <td className="px-3 py-2">
                                <input className="h-8 w-24 rounded border border-gray-300 px-2 text-right text-sm" value={edit.conversionFactor ?? scan.conversionFactor} onChange={(e) => setScanEdit(scan.id, "conversionFactor", e.target.value)} onBlur={() => saveScan(scan)} inputMode="decimal" disabled={loading || isLocked || !canManage} />
                              </td>
                              <td className="px-3 py-2">
                                <input className="h-8 w-28 rounded border border-gray-300 px-2 text-right text-sm" value={edit.unitValue ?? scan.unitValue} onChange={(e) => setScanEdit(scan.id, "unitValue", e.target.value)} onBlur={() => saveScan(scan)} inputMode="decimal" disabled={loading || isLocked || !canManage} />
                              </td>
                              <td className="px-3 py-2">
                                <input type="date" className="h-8 w-36 rounded border border-gray-300 px-2 text-sm" value={edit.partidaExpirationDate ?? scan.partidaExpirationDate ?? ""} onChange={(e) => setScanEdit(scan.id, "partidaExpirationDate", e.target.value)} onBlur={() => saveScan(scan)} disabled={loading || isLocked || !canManage} />
                              </td>
                              <td className="px-3 py-2 font-medium">{money(scan.totalValue)}</td>
                              <td className="px-3 py-2 text-xs text-gray-600">{scan.isManualQuantity ? "Manual" : "Escaneo"}</td>
                              <td className="px-3 py-2 text-right">
                                <button type="button" className="inline-flex h-8 w-8 items-center justify-center rounded border border-rose-200 text-rose-700 hover:bg-rose-50 disabled:opacity-50" onClick={() => deleteScan(scan.id)} disabled={loading || isLocked || !canManage} title="Eliminar bulto">
                                  <Trash2 className="h-4 w-4" />
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </DesktopTableWrap>
                </section>

                {(validationErrors.length > 0 || validation?.ok) && (
                  <section className={`rounded border p-3 text-sm ${validation?.ok ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-rose-200 bg-rose-50 text-rose-900"}`}>
                    <div className="mb-2 flex items-center gap-2 font-semibold">
                      {validation?.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
                      {validation?.ok ? "Prevalidación local y de catálogos Bejerman completada" : "Hay errores para corregir"}
                    </div>
                    {validationErrors.map((item, index) => (
                      <div key={`${item.code}-${item.field}-${index}`}>{item.message}</div>
                    ))}
                  </section>
                )}

                {validationWarnings.length > 0 && (
                  <section className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                    <div className="mb-2 flex items-center gap-2 font-semibold">
                      <AlertTriangle className="h-4 w-4" />
                      Avisos a revisar antes de registrar
                    </div>
                    {validationWarnings.map((item, index) => (
                      <div key={`${item.code}-${item.field}-${index}`}>{item.message}</div>
                    ))}
                  </section>
                )}

                {Array.isArray(entry.events) && entry.events.length > 0 && (
                  <details className="rounded border border-gray-200 bg-white">
                    <summary className="cursor-pointer px-3 py-2 text-sm font-semibold text-gray-800">
                      Actividad del lote ({entry.events.length})
                    </summary>
                    <div className="max-h-72 divide-y divide-gray-100 overflow-y-auto border-t border-gray-200">
                      {entry.events.map((event, index) => (
                        <div key={event.id || `${event.eventType}-${event.createdAt}-${index}`} className="px-3 py-2 text-sm">
                          <div className="flex flex-col gap-0.5 sm:flex-row sm:items-center sm:justify-between">
                            <span className="font-medium text-gray-900">{eventLabel(event.eventType)}</span>
                            <span className="text-xs text-gray-500">{formatEventDate(event.createdAt)}</span>
                          </div>
                          {event.note && (
                            <div className="mt-1 whitespace-pre-wrap break-words text-xs text-gray-700">{event.note}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </>
            )}
          </main>
        </div>
      </div>
    </div>
  );
}
