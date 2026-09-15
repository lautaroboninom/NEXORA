import { useEffect, useMemo, useState } from "react";
import { Plus, Search, Trash2, X } from "lucide-react";
import { getBejermanRemitosBySerial, postDeliveryOrderRemitos } from "../lib/api";
import { ResponsiveModalOverlay, ResponsiveModalPanel } from "./Responsive.jsx";

const ALLOWED_POINTS_OF_SALE = new Set(["00001", "00004", "00007"]);
const QUANTITY_TOLERANCE = 0.0001;

let remitoDraftSequence = 0;

function clean(value) {
  return String(value ?? "").trim();
}

function requestErrorMessage(error, fallback) {
  const detail = error?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  const nestedMessage = error?.data?.error?.message;
  if (typeof nestedMessage === "string" && nestedMessage.trim()) return nestedMessage;
  return error?.message || fallback;
}

function draftRemito({ remitoNumber = "", completed = false, equipmentSerials = [], issueDate = "" } = {}) {
  remitoDraftSequence += 1;
  // `equipmentSerials` es el equipo que eligió el buscador: el backend lo cuelga del
  // renglón, porque un alquiler sin equipo identificado no queda registrado en ningún lado.
  return { id: `manual-remito-${remitoDraftSequence}`, remitoNumber, completed, equipmentSerials, issueDate };
}

function suggestedDraftCount(order) {
  const persistedExpectedCount = Array.isArray(order?.remitos)
    ? Math.max(
        0,
        ...order.remitos.map((remito) => Number.parseInt(String(remito?.expectedCount ?? ""), 10) || 0),
      )
    : 0;
  const candidates = [
    order?.expectedCount,
    order?.expectedRemitoCount,
    order?.remitoExpectedCount,
    order?.suggestedRemitoCount,
    persistedExpectedCount,
    Array.isArray(order?.remitos) ? order.remitos.length : 0,
    Array.isArray(order?.remitoNumbers) ? order.remitoNumbers.length : 0,
  ]
    .map((value) => Number.parseInt(String(value ?? ""), 10))
    .filter((value) => Number.isFinite(value) && value > 0);
  return candidates.length ? Math.max(...candidates) : 1;
}

function persistedOrderRemitos(order) {
  if (Array.isArray(order?.remitos) && order.remitos.length) {
    return order.remitos.filter((remito) => remito && typeof remito === "object");
  }
  if (Array.isArray(order?.remitoNumbers)) {
    return order.remitoNumbers
      .map((remitoNumber) => clean(remitoNumber))
      .filter(Boolean)
      .map((remitoNumber) => ({ remitoNumber, status: "reconciled", generated: true }));
  }
  const remitoNumber = clean(order?.remitoNumber);
  return remitoNumber ? [{ remitoNumber, status: "reconciled", generated: true }] : [];
}

function persistedRemitoCompleted(remito) {
  const status = clean(remito?.status).toLowerCase();
  return Boolean(remito?.generated) || status === "generated" || status === "reconciled";
}

function pointOfSaleFromRemito(value) {
  const match = clean(value).match(/(?:^|[^\d])(\d{1,5})\s*-\s*\d{1,8}(?:[^\d]|$)/);
  return match ? match[1].padStart(5, "0") : "";
}

function remitoDocumentKey(value) {
  const normalized = clean(value).toUpperCase();
  const match = normalized.match(/(?:^|[^\d])(\d{1,5})\s*-\s*(\d{1,8})(?:[^\d]|$)/);
  if (!match) return normalized;
  return `${match[1].padStart(5, "0")}-${match[2].padStart(8, "0")}`;
}

function numberValue(value) {
  if (value === "" || value === null || value === undefined) return 0;
  const parsed = Number(String(value).replace(",", "."));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function formatQuantity(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "-";
  return parsed.toLocaleString("es-AR", { maximumFractionDigits: 4 });
}

function itemPartidas(item) {
  if (Array.isArray(item?.partidas) && item.partidas.length) return item.partidas;
  if (!clean(item?.partida)) return [];
  return [
    {
      partida: item.partida,
      assignedQuantity: item.quantity,
      partidaExpirationDate: item.partidaExpirationDate,
      stockDepositCode: item.stockDepositCode,
      legacyTopLevel: true,
    },
  ];
}

function allocationUnits(order) {
  return (Array.isArray(order?.items) ? order.items : []).flatMap((item, itemIndex) => {
    const itemId = clean(item?.id);
    const itemLabel = clean(item?.articleName) || clean(item?.description) || clean(item?.sourceText) || "Artículo sin detalle";
    const articleCode = clean(item?.articleCode);
    const partidas = itemPartidas(item);
    if (!partidas.length) {
      return [
        {
          key: `${itemId || `item-${itemIndex}`}:sin-partida`,
          itemId,
          itemLabel,
          articleCode,
          partida: "",
          quantity: Number(item?.quantity || 0),
        },
      ];
    }
    return partidas.map((partida, partidaIndex) => ({
      key: `${itemId || `item-${itemIndex}`}:partida-${partidaIndex}`,
      itemId,
      itemLabel,
      articleCode,
      partida: clean(partida?.partida),
      partidaId: clean(partida?.id),
      legacyPartida: Boolean(partida?.legacyTopLevel),
      quantity: Number(partida?.assignedQuantity ?? partida?.quantity ?? 0),
      expirationDate: clean(partida?.partidaExpirationDate),
      depositCode: clean(partida?.stockDepositCode),
    }));
  });
}

function allocationInputKey(unitKey, remitoId) {
  return `${unitKey}::${remitoId}`;
}

function initializeManualState(order, units) {
  const persisted = persistedOrderRemitos(order);
  const drafts = persisted.map((remito) =>
    draftRemito({
      remitoNumber: clean(remito?.remitoNumber),
      completed: Boolean(clean(remito?.remitoNumber)) && persistedRemitoCompleted(remito),
    }),
  );
  const partial = clean(order?.remitoSetStatus).toLowerCase() === "partial";
  const hasPendingDraft = persisted.some((remito) => !persistedRemitoCompleted(remito));
  const minimumForPartial = partial && !hasPendingDraft ? persisted.length + 1 : persisted.length;
  const targetCount = Math.max(1, suggestedDraftCount(order), minimumForPartial);
  while (drafts.length < targetCount) drafts.push(draftRemito());

  const allocationValues = {};
  const addAllocationValue = (unit, draft, rawQuantity) => {
    const quantity = numberValue(rawQuantity);
    if (!unit || !Number.isFinite(quantity) || quantity <= 0) return;
    const key = allocationInputKey(unit.key, draft.id);
    allocationValues[key] = String(numberValue(allocationValues[key]) + quantity);
  };

  persisted.forEach((remito, remitoIndex) => {
    const draft = drafts[remitoIndex];
    if (!draft) return;
    const allocations = Array.isArray(remito?.allocations) ? remito.allocations : [];
    allocations.forEach((allocation) => {
      const itemId = clean(allocation?.itemId || allocation?.orderItemId);
      const itemUnits = units.filter((unit) => unit.itemId === itemId);
      const partidas = Array.isArray(allocation?.partidas) ? allocation.partidas : [];
      if (!partidas.length) {
        const partidaId = clean(allocation?.partidaId || allocation?.orderItemPartidaId);
        const partidaNumber = clean(allocation?.partida);
        const unit = partidaId || partidaNumber
          ? itemUnits.find((candidate) => (
              partidaId ? candidate.partidaId === partidaId : candidate.partida === partidaNumber
            ))
          : itemUnits.find((candidate) => !candidate.partida || candidate.legacyPartida);
        addAllocationValue(unit, draft, allocation?.quantity ?? allocation?.assignedQuantity);
        return;
      }
      partidas.forEach((partida) => {
        const partidaId = clean(partida?.partidaId || partida?.orderItemPartidaId);
        const partidaNumber = clean(partida?.partida);
        const unit = itemUnits.find((candidate) =>
          partidaId
            ? candidate.partidaId === partidaId
            : candidate.partida === partidaNumber,
        );
        addAllocationValue(unit, draft, partida?.quantity ?? partida?.assignedQuantity);
      });
    });
  });

  return { drafts, allocationValues };
}

function remitoMode(drafts) {
  const points = drafts.map((draft) => pointOfSaleFromRemito(draft.remitoNumber));
  if (!points.length || points.some((point) => !point)) return "incomplete";
  if (points.some((point) => !ALLOWED_POINTS_OF_SALE.has(point))) return "invalid";
  const modes = new Set(points.map((point) => (point === "00001" ? "register" : "existing")));
  return modes.size === 1 ? Array.from(modes)[0] : "mixed";
}

function allocationValidation(units, drafts, values) {
  const invalidUnits = [];
  units.forEach((unit) => {
    const quantities = drafts.map((draft) => numberValue(values[allocationInputKey(unit.key, draft.id)]));
    const assigned = quantities.reduce((sum, quantity) => sum + quantity, 0);
    if (
      !Number.isFinite(unit.quantity) ||
      unit.quantity <= 0 ||
      quantities.some((quantity) => !Number.isFinite(quantity) || quantity < 0) ||
      Math.abs(assigned - unit.quantity) > QUANTITY_TOLERANCE
    ) {
      invalidUnits.push(unit.key);
    }
  });
  return invalidUnits;
}

function remitoPayload(drafts, units, allocationValues, withAllocations) {
  return drafts.map((draft) => {
    const remito = { remitoNumber: clean(draft.remitoNumber) };
    if (draft.equipmentSerials?.length) remito.equipmentSerials = draft.equipmentSerials;
    if (clean(draft.issueDate)) remito.issueDate = clean(draft.issueDate);
    if (!withAllocations) return remito;

    const byItem = new Map();
    units.forEach((unit) => {
      const quantity = numberValue(allocationValues[allocationInputKey(unit.key, draft.id)]);
      if (!Number.isFinite(quantity) || quantity <= 0) return;
      const current = byItem.get(unit.itemId) || {
        itemId: unit.itemId,
        quantity: 0,
        partidas: [],
      };
      current.quantity += quantity;
      if (unit.partida && !unit.legacyPartida) {
        current.partidas.push({
          partidaId: unit.partidaId || undefined,
          partida: unit.partida,
          quantity,
        });
      }
      byItem.set(unit.itemId, current);
    });
    const allocations = Array.from(byItem.values()).map(({ partidas, ...allocation }) =>
      partidas.length ? { ...allocation, partidas } : allocation,
    );
    return { ...remito, allocations };
  });
}

function remitoShortLabel(draft, index) {
  return clean(draft.remitoNumber) || `Remito ${index + 1}`;
}

export default function DeliveryOrderManualRemitoModal({ open, order, onClose, onSaved }) {
  const [drafts, setDrafts] = useState([draftRemito()]);
  const [allocationValues, setAllocationValues] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  // El operador tiene el equipo, no el número: se busca en Bejerman por serie o MG.
  const [lookupQuery, setLookupQuery] = useState("");
  const [lookup, setLookup] = useState(null);
  const [lookupLoading, setLookupLoading] = useState(false);
  const units = useMemo(() => allocationUnits(order), [order]);

  useEffect(() => {
    if (!open || !order?.id) return;
    const initialized = initializeManualState(order, units);
    setDrafts(initialized.drafts);
    setAllocationValues(initialized.allocationValues);
    setSaving(false);
    setError("");
    setLookupQuery("");
    setLookup(null);
    setLookupLoading(false);
  }, [open, order?.id, order?.expectedCount, order?.expectedRemitoCount, order?.remitoExpectedCount, order?.suggestedRemitoCount]);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose, saving]);

  const mode = useMemo(() => remitoMode(drafts), [drafts]);
  const partialRecovery = clean(order?.remitoSetStatus).toLowerCase() === "partial";
  const requiresAllocations = drafts.length > 1 && mode === "register";
  const invalidAllocationUnits = useMemo(
    () => (requiresAllocations ? allocationValidation(units, drafts, allocationValues) : []),
    [allocationValues, drafts, requiresAllocations, units],
  );
  const invalidAllocationUnitSet = useMemo(
    () => new Set(invalidAllocationUnits),
    [invalidAllocationUnits],
  );

  if (!open || !order) return null;

  const lookupFlow = clean(order?.deliveryType) === "rental" ? "rental" : "service_release";
  const lookupIngresoId = order?.ingresoId
    || (order?.items || []).map((item) => item?.ingresoId).find(Boolean)
    || (order?.items || []).flatMap((item) => item?.partidas || []).map((row) => row?.ingresoId).find(Boolean);

  const buscarRemitos = async () => {
    const serial = clean(lookupQuery);
    if (!serial || lookupLoading) return;
    setLookupLoading(true);
    setError("");
    try {
      const result = await getBejermanRemitosBySerial({
        serial,
        flow: lookupFlow,
        companyKey: order?.companyKey,
        ingresoId: lookupIngresoId,
      });
      setLookup(result || null);
    } catch (err) {
      setLookup(null);
      setError(requestErrorMessage(err, "No se pudo buscar el remito en Bejerman."));
    } finally {
      setLookupLoading(false);
    }
  };

  const usarCandidato = (candidate) => {
    const numero = clean(candidate?.remitoNumber);
    if (!numero) return;
    const serial = clean(candidate?.partida) || clean(lookup?.serial);
    const serials = serial ? [serial] : [];
    const issueDate = clean(candidate?.issueDate);
    setDrafts((current) => {
      const libre = current.find((draft) => !draft.completed && !clean(draft.remitoNumber));
      if (libre) {
        return current.map((draft) => (
          draft.id === libre.id
            ? { ...draft, remitoNumber: numero, equipmentSerials: serials, issueDate }
            : draft
        ));
      }
      return [...current, draftRemito({ remitoNumber: numero, equipmentSerials: serials, issueDate })];
    });
    setError("");
  };

  const updateRemitoNumber = (id, value) => {
    setDrafts((current) => current.map((draft) => (
      draft.id === id && !draft.completed ? { ...draft, remitoNumber: value } : draft
    )));
    setError("");
  };

  const removeRemito = (id) => {
    setDrafts((current) => (
      current.length > 1 ? current.filter((draft) => draft.id !== id || draft.completed) : current
    ));
    setError("");
  };

  const addRemito = () => {
    setDrafts((current) => [...current, draftRemito()]);
    setError("");
  };

  const updateAllocation = (unitKey, remitoId, value) => {
    if (drafts.some((draft) => draft.id === remitoId && draft.completed)) return;
    setAllocationValues((current) => ({
      ...current,
      [allocationInputKey(unitKey, remitoId)]: value,
    }));
    setError("");
  };

  const submit = async () => {
    const numbers = drafts.map((draft) => clean(draft.remitoNumber));
    if (numbers.some((number) => !number)) {
      setError("Complete el número de todos los remitos.");
      return;
    }
    if (new Set(numbers.map(remitoDocumentKey)).size !== numbers.length) {
      setError("No se puede cargar el mismo remito más de una vez.");
      return;
    }
    if (mode === "incomplete" || mode === "invalid") {
      setError("Cargue cada remito completo con punto de venta 00001, 00004 o 00007. Por ejemplo: RT R 00004-00004715.");
      return;
    }
    if (mode === "mixed") {
      setError("No mezcle remitos manuales 00001 con remitos existentes 00004/00007 en la misma registración.");
      return;
    }
    if (requiresAllocations) {
      if (!units.length || units.some((unit) => !unit.itemId)) {
        setError("No se pueden distribuir los artículos porque falta su identificador. Cierre el modal, actualice la orden y vuelva a intentar.");
        return;
      }
      if (invalidAllocationUnits.length) {
        setError("Distribuya la cantidad completa de cada artículo o partida entre los remitos, sin faltantes ni excedentes.");
        return;
      }
      const usedRemitos = new Set();
      units.forEach((unit) => {
        drafts.forEach((draft) => {
          if (numberValue(allocationValues[allocationInputKey(unit.key, draft.id)]) > 0) usedRemitos.add(draft.id);
        });
      });
      if (usedRemitos.size !== drafts.length) {
        setError("Cada remito debe tener al menos un artículo o partida asignado.");
        return;
      }
    }

    setSaving(true);
    setError("");
    try {
      const payload = {
        remitos: remitoPayload(drafts, units, allocationValues, requiresAllocations),
      };
      const result = await postDeliveryOrderRemitos(order.id, payload);
      if (result?.success === false) {
        const partialMessage =
          (typeof result?.error === "string" ? result.error : result?.error?.message) ||
          "Algunos remitos quedaron pendientes.";
        if (result?.order?.id) {
          const refreshedUnits = allocationUnits(result.order);
          const initialized = initializeManualState(result.order, refreshedUnits);
          setDrafts(initialized.drafts);
          setAllocationValues(initialized.allocationValues);
        }
        setError(
          `${partialMessage} Puede volver a guardar el mismo conjunto: NEXORA conservará los remitos completados y procesará solamente los pendientes.`,
        );
        onSaved?.(result, { keepOpen: true });
        return;
      }
      onSaved?.(result);
    } catch (err) {
      setError(requestErrorMessage(err, "No se pudieron registrar los remitos."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ResponsiveModalOverlay role="dialog" aria-modal="true" align="center" onClick={() => !saving && onClose()}>
      <ResponsiveModalPanel scroll={false} className="flex max-w-5xl flex-col" onClick={(event) => event.stopPropagation()}>
        <div className="flex shrink-0 items-start justify-between gap-4 border-b px-4 py-3 sm:px-5 sm:py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Cargar remito</h2>
            <p className="text-sm text-gray-600">
              {order.orderNumber || "Orden de entrega"} seguirá siendo una sola orden, aunque tenga varios remitos.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="rounded p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900 disabled:opacity-50"
            aria-label="Cerrar"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3 sm:p-5">
          {suggestedDraftCount(order) > 1 && (
            <div className="mb-4 rounded border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-900">
              {partialRecovery
                ? `Este conjunto requiere ${suggestedDraftCount(order)} remitos.`
                : `Por el límite físico de Bejerman, esta orden sugiere ${suggestedDraftCount(order)} remitos. Puede agregar o quitar campos según los comprobantes reales.`}
            </div>
          )}
          {partialRecovery && (
            <div className="mb-4 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              Este conjunto quedó incompleto. Los remitos ya registrados están bloqueados; complete los pendientes y vuelva a guardar el conjunto completo.
            </div>
          )}
          {String(order?.remitoSetStatus || "").toLowerCase() === "review" && (
            <div className="mb-4 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              La emisión anterior quedó en revisión. Registre los números solamente si confirmó los comprobantes directamente en Bejerman.
            </div>
          )}

          <div className="mb-4 rounded border border-sky-200 bg-sky-50 p-3">
            <label className="block text-sm">
              <span className="mb-1 block text-xs font-semibold uppercase text-sky-900">
                Buscar el remito en Bejerman por equipo
              </span>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  value={lookupQuery}
                  onChange={(event) => setLookupQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    void buscarRemitos();
                  }}
                  disabled={saving || lookupLoading}
                  className="h-10 min-w-[200px] flex-1 rounded border bg-white px-3 text-sm"
                  placeholder="N° de serie o MG"
                />
                <button
                  type="button"
                  onClick={() => void buscarRemitos()}
                  disabled={saving || lookupLoading || !clean(lookupQuery)}
                  className="inline-flex h-10 items-center gap-1 rounded border border-sky-300 bg-white px-3 text-sm text-sky-900 hover:bg-sky-100 disabled:opacity-50"
                >
                  <Search className="h-4 w-4" aria-hidden="true" />
                  {lookupLoading ? "Buscando..." : "Buscar"}
                </button>
              </div>
              <span className="mt-1 block text-xs text-sky-800">
                Se listan los {lookupFlow === "rental" ? "RTA" : "RSS"} de Bejerman que citan ese equipo, posteriores a su ingreso.
              </span>
            </label>

            {lookup?.equipment && (
              <div className="mt-2 text-xs text-sky-900">
                {[lookup.equipment.marca, lookup.equipment.modelo].filter(Boolean).join(" ")}
                {lookup.serial ? ` · N/S ${lookup.serial}` : ""}
                {lookup.equipment.numero_interno ? ` · ${lookup.equipment.numero_interno}` : ""}
              </div>
            )}
            {lookup && lookup.status !== "ok" && (
              <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                {lookup.detail || "No se pudo consultar Bejerman."}
              </div>
            )}
            {lookup?.status === "ok" && (
              <div className="mt-2 space-y-2">
                {(lookup.candidates || []).map((candidate) => (
                  <button
                    key={candidate.remitoNumber}
                    type="button"
                    onClick={() => usarCandidato(candidate)}
                    disabled={saving}
                    className="block w-full rounded border border-sky-200 bg-white px-3 py-2 text-left text-sm hover:bg-sky-100 disabled:opacity-50"
                  >
                    <span className="block font-mono font-semibold text-gray-900">{candidate.remitoNumber}</span>
                    <span className="mt-0.5 block text-xs text-gray-600">
                      {candidate.issueDate}
                      {candidate.customerCode ? ` · ${candidate.customerCode}` : ""}
                      {candidate.articleCode ? ` · ${candidate.articleCode}` : ""}
                    </span>
                  </button>
                ))}
                {!(lookup.candidates || []).length && (
                  <div className="rounded border bg-white px-3 py-2 text-xs text-gray-600">
                    {(lookup.alreadyLinked || []).length
                      ? "El único remito que cita ese equipo ya está asociado a esa serie."
                      : "Bejerman no tiene remitos de ese tipo para ese equipo desde su fecha de ingreso."}
                  </div>
                )}
                {(lookup.alreadyLinked || []).length > 0 && (
                  <div className="text-xs text-gray-500">
                    Ya asociados a esta serie: {(lookup.alreadyLinked || []).map((item) => item.remitoNumber).join(", ")}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="space-y-3">
            {drafts.map((draft, index) => (
              <div
                key={draft.id}
                className={`rounded border p-3 ${draft.completed ? "border-emerald-200 bg-emerald-50" : "bg-gray-50"}`}
              >
                <div className="flex items-end gap-2">
                  <label className="min-w-0 flex-1 text-sm">
                    <span className="mb-1 flex flex-wrap items-center gap-2 text-xs font-semibold uppercase text-gray-500">
                      Remito {index + 1}
                      {draft.completed && (
                        <span className="rounded-full border border-emerald-300 bg-white px-2 py-0.5 text-[10px] normal-case text-emerald-800">
                          Ya registrado
                        </span>
                      )}
                    </span>
                    <input
                      value={draft.remitoNumber}
                      onChange={(event) => updateRemitoNumber(draft.id, event.target.value)}
                      disabled={saving}
                      readOnly={draft.completed}
                      className={`h-10 w-full rounded border px-3 font-mono text-sm ${draft.completed ? "bg-gray-100 text-gray-700" : "bg-white"}`}
                      placeholder="Ej.: RT R 00004-00004715"
                      autoFocus={index === 0}
                    />
                  </label>
                  {drafts.length > 1 && !draft.completed && !partialRecovery && (
                    <button
                      type="button"
                      onClick={() => removeRemito(draft.id)}
                      disabled={saving}
                      className="inline-flex h-10 items-center gap-1 rounded border bg-white px-3 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50"
                      aria-label={`Quitar remito ${index + 1}`}
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                      <span className="hidden sm:inline">Quitar</span>
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>

          {!partialRecovery && (
            <button
              type="button"
              onClick={addRemito}
              disabled={saving}
              className="mt-3 inline-flex items-center gap-2 rounded border border-dashed px-3 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Agregar otro remito
            </button>
          )}

          {drafts.length > 1 && mode === "existing" && (
            <div className="mt-4 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
              Los remitos 00004/00007 se asociarán a esta orden sin volver a mover stock. No hace falta distribuir los artículos.
            </div>
          )}
          {mode === "mixed" && (
            <div className="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              No se pueden mezclar remitos 00001 con remitos 00004/00007 en la misma registración.
            </div>
          )}

          {requiresAllocations && (
            <section className="mt-5">
              <div>
                <h3 className="font-semibold text-gray-900">Distribución de artículos y partidas</h3>
                <p className="text-sm text-gray-600">
                  Indique la cantidad real incluida en cada remito. La suma debe coincidir exactamente con la orden.
                </p>
              </div>
              <div className="mt-3 space-y-3">
                {units.map((unit) => {
                  const assigned = drafts.reduce(
                    (sum, draft) => sum + numberValue(allocationValues[allocationInputKey(unit.key, draft.id)]),
                    0,
                  );
                  const invalid = invalidAllocationUnitSet.has(unit.key);
                  return (
                    <div key={unit.key} className={`rounded border p-3 ${invalid ? "border-amber-300 bg-amber-50" : "bg-white"}`}>
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div>
                          <div className="font-medium text-gray-900">
                            {[unit.articleCode, unit.itemLabel].filter(Boolean).join(" - ")}
                          </div>
                          <div className="mt-0.5 text-xs text-gray-600">
                            {unit.partida ? `Partida ${unit.partida}` : "Sin partida"}
                            {unit.depositCode ? ` · Depósito ${unit.depositCode}` : ""}
                            {unit.expirationDate ? ` · Vto. ${unit.expirationDate}` : ""}
                          </div>
                        </div>
                        <div className={`text-xs font-medium ${invalid ? "text-amber-800" : "text-emerald-700"}`}>
                          Asignado {formatQuantity(assigned)} de {formatQuantity(unit.quantity)}
                        </div>
                      </div>
                      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                        {drafts.map((draft, index) => (
                          <label key={draft.id} className="text-xs text-gray-600">
                            <span className="mb-1 block truncate" title={remitoShortLabel(draft, index)}>
                              {remitoShortLabel(draft, index)}
                            </span>
                            <input
                              type="number"
                              min="0"
                              step="any"
                              value={allocationValues[allocationInputKey(unit.key, draft.id)] ?? ""}
                              onChange={(event) => updateAllocation(unit.key, draft.id, event.target.value)}
                              disabled={saving}
                              readOnly={draft.completed}
                              className={`h-9 w-full rounded border px-2 text-sm text-gray-900 ${draft.completed ? "bg-gray-100" : "bg-white"}`}
                              placeholder="0"
                            />
                          </label>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          {error && (
            <div className="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-col-reverse gap-2 border-t bg-white px-4 py-3 sm:flex-row sm:justify-end sm:px-5 sm:py-4">
          <button type="button" onClick={onClose} disabled={saving} className="rounded border px-4 py-2 text-sm hover:bg-gray-50 disabled:opacity-50">
            Cancelar
          </button>
          <button type="button" onClick={submit} disabled={saving} className="btn px-4 py-2 disabled:opacity-50">
            {saving ? "Guardando..." : drafts.length > 1 ? "Guardar remitos" : "Guardar remito"}
          </button>
        </div>
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
  );
}
