import TechnicalInterventionEditor from "../../../components/TechnicalInterventionEditor";
import { useCallback, useEffect, useState } from "react";

import IngresoPhotos from "../../../components/IngresoPhotos";
import RejectedBudgetChargeModal from "../../../components/RejectedBudgetChargeModal";
import Row from "../../../components/Row";
import { useAuth } from "../../../context/AuthContext";
import { can, PERMISSION_CODES } from "../../../lib/permissions";
import { RESOLUCION, RESOLUCION_OPTIONS, ESTADO } from "../../../lib/constants";
import {
  deleteAccesorioIngreso,
  getQuote,
  postAccesorioIngreso,
  postCerrarReparacion,
  postHabilitarReparacionCotizacion,
  postMarcarControladoSinDefecto,
  postMarcarNoSeRepara,
  postMarcarParaReparar,
  postMarcarReparado,
  postQuitarEstadoFinalReparacion,
  postQuitarReparar,
} from "../../../lib/api";
import { isSaleTicketState } from "../../../lib/ui-helpers";
import { describeSwapSummary } from "../../../lib/device-swap.js";

function resolveRejectedQuoteReference(quotePayload, preferredQuoteId = null) {
  if (!quotePayload) return null;

  const versions = Array.isArray(quotePayload?.versions) ? quotePayload.versions : [];
  const preferredIdNum = Number(preferredQuoteId || 0) || null;
  const preferredRejected = preferredIdNum
    ? versions.find(
      (version) => (
        Number(version?.quote_id || 0) === preferredIdNum
        && String(version?.estado || "").trim() === "rechazado"
      ),
    )
    : null;
  const latestRejected = versions.find(
    (version) => String(version?.estado || "").trim() === "rechazado",
  );
  const currentRejected = String(quotePayload?.estado || "").trim() === "rechazado"
    ? {
      quote_id: quotePayload.quote_id,
      version_num: quotePayload.version_num,
      subtotal: quotePayload.subtotal,
      iva_21: quotePayload.iva_21,
      total: quotePayload.total,
    }
    : null;

  const resolved = preferredRejected || latestRejected || currentRejected;
  if (!resolved) return null;

  return {
    quoteId: resolved.quote_id,
    versionNum: resolved.version_num,
    subtotal: resolved.subtotal,
    iva_21: resolved.iva_21,
    total: resolved.total,
  };
}

export default function DiagnosticoTab({
  id,
  data,
  money,
  canEditAcc,
  accesCatalogo,
  nuevoAcc,
  setNuevoAcc,
  descripcion,
  setDescripcion,
  trabajos,
  setTrabajos,
  fechaServStr,
  setFechaServStr,
  maxLocalNow,
  canResolve,
  resolucion,
  setResolucion,
  canAutorizarReparar,
  isCotizacion,
  permiteReparacion,
  canHabilitarReparacionCotizacion,
  actAsTech,
  canEditDiag,
  canMarkReparado,
  patch,
  setErr,
  refreshIngreso,
  setToastMsg,
  setShowReparadoToast,
  savingDiag,
  canManagePhotos,
  onOpenReleaseModal,
  onOpenSwapModal,
}) {
  const [technicalDirty, setTechnicalDirty] = useState(false);
  const [addingAcc, setAddingAcc] = useState(false);
  const [deletingAccId, setDeletingAccId] = useState(null);
  const [savingAll, setSavingAll] = useState(false);
  const [savingResol, setSavingResol] = useState(false);
  const [marcandoReparar, setMarcandoReparar] = useState(false);
  const [quitandoReparar, setQuitandoReparar] = useState(false);
  const [marcandoReparado, setMarcandoReparado] = useState(false);
  const [marcandoNoSeRepara, setMarcandoNoSeRepara] = useState(false);
  const [quitandoEstadoFinal, setQuitandoEstadoFinal] = useState(false);
  const [habilitandoReparacion, setHabilitandoReparacion] = useState(false);
  const [fajaGarantiaInput, setFajaGarantiaInput] = useState(data?.faja_garantia || "");
  const [rejectedChargeModalOpen, setRejectedChargeModalOpen] = useState(false);
  const [rejectedChargeModalLoading, setRejectedChargeModalLoading] = useState(false);
  const [rejectedChargeModalError, setRejectedChargeModalError] = useState("");
  const [rejectedChargeReferenceWarning, setRejectedChargeReferenceWarning] = useState("");
  const [rejectedQuoteReference, setRejectedQuoteReference] = useState(null);

  const estadoLower = (data?.estado || "").toLowerCase();
  const isEntregadoOBaja = ["entregado", "baja"].includes(estadoLower) || isSaleTicketState(estadoLower);
  // "Cambio" no se guarda desde este botón: abre el modal, que exige `device_swap`. Sin el
  // permiso la opción era un callejón sin salida — se elegía, se abría el modal con todo
  // deshabilitado y la OS no se podía cerrar por ninguna vía.
  const { user: usuarioActual } = useAuth();
  const puedeCambioEquipo = can(usuarioActual, PERMISSION_CODES.ACTION_INGRESO_DEVICE_SWAP);
  const opcionesResolucion = puedeCambioEquipo
    ? RESOLUCION_OPTIONS
    : RESOLUCION_OPTIONS.filter((opt) => opt.value !== RESOLUCION.CAMBIO);
  const estadosCierreSalida = new Set([
    ESTADO.LIBERADO,
    ESTADO.ENTREGADO,
    ESTADO.BAJA,
    ESTADO.ALQUILADO,
    ESTADO.VENDIDO_PENDIENTE_ENTREGA,
    ESTADO.VENDIDO_ENTREGADO,
  ].map((value) => String(value || "").toLowerCase()));
  const isEstadoCierreSalida = estadosCierreSalida.has(estadoLower) || isSaleTicketState(estadoLower);
  const estadosBloqueadosDiag = new Set([
    ESTADO.REPARADO,
    ESTADO.LIBERADO,
    ESTADO.ENTREGADO,
    ESTADO.BAJA,
    ESTADO.ALQUILADO,
    ESTADO.CONTROLADO_SIN_DEFECTO,
    ESTADO.NO_SE_REPARA,
    "controlado_sin_defecto",
    "no_se_repara",
    ESTADO.VENDIDO_PENDIENTE_ENTREGA,
    ESTADO.VENDIDO_ENTREGADO,
  ].map((value) => String(value || "").toLowerCase()));
  const isEstadoBloqueadoDiag = estadosBloqueadosDiag.has(estadoLower);
  const isEstadoBloqueadoReparado = isEstadoCierreSalida;
  const isReparadoActual = estadoLower === String(ESTADO.REPARADO || "reparado").toLowerCase();
  const isControladoSinDefectoActual = estadoLower === "controlado_sin_defecto"
    || estadoLower === String(ESTADO.CONTROLADO_SIN_DEFECTO || "").toLowerCase();
  const isNoSeReparaActual = estadoLower === "no_se_repara"
    || estadoLower === String(ESTADO.NO_SE_REPARA || "").toLowerCase();
  const accionTecnicaOcupada = savingAll || marcandoReparado || marcandoNoSeRepara || quitandoEstadoFinal;
  const reparacionBloqueadaCotizacion = !!isCotizacion && !permiteReparacion;
  const puedeReparar = !!canAutorizarReparar
    && estadoLower !== "reparar"
    && !isEstadoBloqueadoDiag
    && !reparacionBloqueadaCotizacion;
  const puedeQuitarReparar = !!canAutorizarReparar
    && estadoLower === "reparar"
    && !isEstadoCierreSalida;
  const sinTecnicoAsignado = !data?.asignado_a;
  const swapSummary = describeSwapSummary(data || {});

  useEffect(() => {
    setFajaGarantiaInput(data?.faja_garantia || "");
  }, [data?.id, data?.faja_garantia]);

  const commitFajaGarantia = useCallback(async () => {
    const next = String(fajaGarantiaInput || "").trim();
    const current = String(data?.faja_garantia || "").trim();
    setFajaGarantiaInput(next);
    if (next === current) return;
    await patch({ faja_garantia: next });
  }, [data?.faja_garantia, fajaGarantiaInput, patch]);

  const blurOnEnter = useCallback((event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    event.currentTarget.blur();
  }, []);

  async function submitResolucion(payload) {
    try {
      setSavingResol(true);
      await postCerrarReparacion(id, payload);
      await refreshIngreso();
      // Por acá ya no pasa el cambio de equipo: tiene su propio circuito.
      onOpenReleaseModal?.({
        ...data,
        resolucion: payload.resolucion,
        presupuesto_rechazado_cobro_neto:
          payload.presupuesto_rechazado_cobro_neto ?? data?.presupuesto_rechazado_cobro_neto,
      });
      setErr("");
      return true;
    } catch (e) {
      setErr(e?.message || "No se pudo guardar la resolución");
      return false;
    } finally {
      setSavingResol(false);
    }
  }

  async function openRejectedChargeModal() {
    setRejectedChargeModalError("");
    setRejectedChargeReferenceWarning("");
    setRejectedQuoteReference(null);
    setRejectedChargeModalOpen(true);
    setRejectedChargeModalLoading(true);

    try {
      const quotePayload = await getQuote(id);
      const preferredQuoteId = data?.presupuesto_rechazado_quote_id;
      const resolvedReference = resolveRejectedQuoteReference(quotePayload, preferredQuoteId);
      setRejectedQuoteReference(resolvedReference);

      if (!resolvedReference) {
        setRejectedChargeReferenceWarning("No se encontró un presupuesto rechazado vinculado. El remito se imprimirá sin esa referencia.");
      } else if (
        preferredQuoteId
        && Number(preferredQuoteId) !== Number(resolvedReference.quoteId || 0)
      ) {
        setRejectedChargeReferenceWarning("No se encontró la versión vinculada y se usará la última versión rechazada disponible.");
      }
    } catch {
      setRejectedChargeReferenceWarning("No se pudo cargar la referencia del presupuesto rechazado.");
    } finally {
      setRejectedChargeModalLoading(false);
    }
  }

  async function confirmRejectedCharge(payload) {
    setRejectedChargeModalError("");
    const ok = await submitResolucion({
      resolucion: RESOLUCION.PRESUPUESTO_RECHAZADO,
      presupuesto_rechazado_cobro_neto: payload.presupuesto_rechazado_cobro_neto,
    });
    if (ok) {
      setRejectedChargeModalOpen(false);
      setRejectedChargeReferenceWarning("");
    } else {
      setRejectedChargeModalError("No se pudo guardar el cobro del presupuesto rechazado.");
    }
  }

  async function saveResolucionCambioAware() {
    if (!resolucion) {
      setErr("Seleccione una resolución.");
      return;
    }
    if (String(resolucion) === RESOLUCION.PRESUPUESTO_RECHAZADO) {
      await openRejectedChargeModal();
      return;
    }
    // El cambio de equipo no se guarda desde acá: tiene su propio circuito, que vincula el
    // equipo entregado de verdad y decide qué pasa con el que queda. Ese modal ya deja la
    // resolución puesta, así que este botón sólo lo abre.
    if (String(resolucion) === RESOLUCION.CAMBIO) {
      onOpenSwapModal?.();
      return;
    }

    await submitResolucion({ resolucion });
  }

  async function marcarControladoSinDefecto() {
    if (technicalDirty) { setErr("Guardá los cambios de la intervención técnica antes de cambiar el estado."); return; }
    try {
      setSavingAll(true);
      const resp = await postMarcarControladoSinDefecto(id);
      await refreshIngreso();
      const movedMsg = resp && resp.auto_moved
        ? `Marcado como controlado sin defecto. Movido a ${resp.ubicacion_nombre || resp.auto_moved_to || "Estantería de Alquiler"}`
        : "Marcado como controlado sin defecto";
      setToastMsg(movedMsg);
      setTimeout(() => setToastMsg(""), 3000);
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo marcar como controlado sin defecto");
    } finally {
      setSavingAll(false);
    }
  }

  async function marcarReparado() {
    if (technicalDirty) { setErr("Guardá los cambios de la intervención técnica antes de cambiar el estado."); return; }
    try {
      setMarcandoReparado(true);
      const resp = await postMarcarReparado(id);
      await refreshIngreso();
      if (typeof setShowReparadoToast === "function") {
        setShowReparadoToast(true);
        setTimeout(() => setShowReparadoToast(false), 2000);
      }
      if (resp && resp.auto_moved) {
        const movedMsg = `Marcado como reparado. Movido a ${resp.ubicacion_nombre || resp.auto_moved_to || "Estantería de Alquiler"}`;
        setToastMsg(movedMsg);
        setTimeout(() => setToastMsg(""), 3000);
      }
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo marcar como reparado");
    } finally {
      setMarcandoReparado(false);
    }
  }

  async function marcarNoSeRepara() {
    if (technicalDirty) { setErr("Guardá los cambios de la intervención técnica antes de cambiar el estado."); return; }
    try {
      setMarcandoNoSeRepara(true);
      await postMarcarNoSeRepara(id);
      await refreshIngreso();
      setToastMsg("Marcado como no se repara");
      setTimeout(() => setToastMsg(""), 3000);
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo marcar como no se repara");
    } finally {
      setMarcandoNoSeRepara(false);
    }
  }

  async function quitarEstadoFinalReparacion() {
    const estadoAntes = estadoLower;
    const labels = {
      reparado: "reparado",
      controlado_sin_defecto: "controlado sin defecto",
      no_se_repara: "no se repara",
    };
    try {
      setQuitandoEstadoFinal(true);
      const resp = await postQuitarEstadoFinalReparacion(id);
      await refreshIngreso();
      const nextEstado = String(resp?.estado || "").replaceAll("_", " ") || "diagnóstico";
      setToastMsg(`Quitado ${labels[estadoAntes] || "estado final"}. La OS vuelve a ${nextEstado}`);
      setTimeout(() => setToastMsg(""), 3000);
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo quitar el estado final");
    } finally {
      setQuitandoEstadoFinal(false);
    }
  }

  async function marcarParaReparar() {
    try {
      setErr("");
      setMarcandoReparar(true);
      const resp = await postMarcarParaReparar(id);
      await refreshIngreso();
      const msg = resp?.email_sent
        ? "Aviso enviado al técnico para reparar"
        : "Estado actualizado a 'reparar'";
      setToastMsg(msg);
      setTimeout(() => setToastMsg(""), 3000);
    } catch (e) {
      setErr(e?.message || "No se pudo marcar para reparar");
    } finally {
      setMarcandoReparar(false);
    }
  }

  async function quitarReparar() {
    try {
      setErr("");
      setQuitandoReparar(true);
      const resp = await postQuitarReparar(id);
      await refreshIngreso();
      const nextEstado = String(resp?.estado || "").replaceAll("_", " ") || "diagnosticado";
      setToastMsg(`Quitado reparar. La OS vuelve a ${nextEstado}`);
      setTimeout(() => setToastMsg(""), 3000);
    } catch (e) {
      setErr(e?.message || "No se pudo quitar reparar");
    } finally {
      setQuitandoReparar(false);
    }
  }

  async function habilitarReparacionCotizacion() {
    try {
      setErr("");
      setHabilitandoReparacion(true);
      await postHabilitarReparacionCotizacion(id);
      await refreshIngreso();
      setToastMsg("Reparación habilitada para esta cotización.");
      setTimeout(() => setToastMsg(""), 3000);
    } catch (e) {
      setErr(e?.message || "No se pudo habilitar la reparación");
    } finally {
      setHabilitandoReparacion(false);
    }
  }

  async function addAccesorio() {
    try {
      const descripcionAcc = (nuevoAcc?.descripcion || "").trim().toLowerCase();
      if (!descripcionAcc) {
        setErr("Escriba una descripción.");
        return;
      }
      const acc = (accesCatalogo || []).find(
        (item) => (item?.nombre || "").trim().toLowerCase() === descripcionAcc,
      );
      if (!acc) {
        setErr("Elija una descripción válida de la lista.");
        return;
      }
      setAddingAcc(true);
      await postAccesorioIngreso(id, {
        accesorio_id: Number(acc.id),
        referencia: (nuevoAcc?.referencia || "").trim() || null,
      });
      setNuevoAcc?.({ descripcion: "", referencia: "" });
      await refreshIngreso();
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo agregar el accesorio");
    } finally {
      setAddingAcc(false);
    }
  }

  async function removeAccesorio(itemId) {
    try {
      setDeletingAccId(itemId);
      await deleteAccesorioIngreso(id, itemId);
      await refreshIngreso();
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo quitar el accesorio");
    } finally {
      setDeletingAccId(null);
    }
  }

  return (
    <div className="border rounded p-4">
      <div className="grid grid-cols-1 gap-3 mb-4 md:grid-cols-2">
        <div className="border rounded p-3 bg-gray-50">
          <div className="text-xs uppercase text-gray-500 mb-1">Informe preliminar</div>
          <div className="whitespace-pre-wrap">{data.informe_preliminar || "-"}</div>
        </div>

        <div className="border rounded p-3 bg-gray-50">
          <div className="text-xs uppercase text-gray-500 mb-1">Accesorios</div>
          <div>
            {Array.isArray(data.accesorios_items) && data.accesorios_items.length > 0 ? (
              <ul className="list-disc list-inside text-sm">
                {data.accesorios_items.map((item) => (
                  <li key={item.id} className="flex items-center justify-between gap-2">
                    <span>
                      {item.accesorio_nombre}
                      {item.referencia ? ` (ref: ${item.referencia})` : ""}
                    </span>
                    {canEditAcc ? (
                      <button
                        className="text-red-600 text-xs"
                        onClick={() => removeAccesorio(item.id)}
                        disabled={deletingAccId === item.id}
                        type="button"
                      >
                        {deletingAccId === item.id ? "Quitando..." : "Quitar"}
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="whitespace-pre-wrap">{data.accesorios || "-"}</div>
            )}
          </div>

          {canEditAcc ? (
            <div className="mt-3 border-t pt-3">
              <div className="text-xs uppercase text-gray-500 mb-2">Agregar accesorio</div>
              <div className="flex flex-wrap items-end gap-2">
                <input
                  className="border rounded p-2 min-w-[240px]"
                  list="accesorios_catalogo"
                  placeholder="Descripción (elija de la lista)"
                  value={nuevoAcc.descripcion}
                  onChange={(e) => setNuevoAcc((state) => ({ ...state, descripcion: e.target.value }))}
                />
                <datalist id="accesorios_catalogo">
                  {accesCatalogo.map((item) => (
                    <option key={item.id} value={item.nombre} />
                  ))}
                </datalist>
                <input
                  className="border rounded p-2 w-40"
                  placeholder="Nro. de referencia (opcional)"
                  value={nuevoAcc.referencia}
                  onChange={(e) => setNuevoAcc((state) => ({ ...state, referencia: e.target.value }))}
                />
                <button
                  className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60"
                  onClick={addAccesorio}
                  disabled={addingAcc || !(nuevoAcc.descripcion || "").trim()}
                  type="button"
                >
                  {addingAcc ? "Agregando..." : "Agregar"}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </div>


      <div className="flex flex-wrap items-end gap-3 mb-3">
        <div>
          <label className="block text-sm text-gray-600 mb-1">Fecha de servicio</label>
          <input
            type="date"
            className="border rounded p-2"
            value={fechaServStr ? fechaServStr.slice(0, 10) : ""}
            onChange={(e) => {
              const value = e.currentTarget.value;
              setFechaServStr(value ? `${value}T00:00` : "");
            }}
            max={maxLocalNow ? maxLocalNow.slice(0, 10) : undefined}
            placeholder="YYYY-MM-DD"
            disabled={typeof canEditDiag === "boolean" ? !canEditDiag : false}
          />
        </div>

        <div className="flex w-full flex-wrap items-end gap-2 lg:ml-auto lg:w-auto">
          {reparacionBloqueadaCotizacion ? (
            <div className="max-w-xs rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Ingreso en cotización de equipo. La reparación está bloqueada hasta habilitarla.
            </div>
          ) : null}

          {reparacionBloqueadaCotizacion && canHabilitarReparacionCotizacion ? (
            <button
            className="bg-amber-700 text-white px-3 py-2 rounded disabled:opacity-60"
            disabled={habilitandoReparacion}
            onClick={habilitarReparacionCotizacion}
            type="button"
            >
              {habilitandoReparacion ? "Habilitando..." : "Habilitar reparación"}
            </button>
          ) : null}

          {puedeReparar ? (
            <button
            className="bg-amber-600 text-white px-3 py-2 rounded disabled:opacity-60"
            disabled={marcandoReparar || sinTecnicoAsignado || !puedeReparar}
            onClick={marcarParaReparar}
            title={sinTecnicoAsignado ? "Asigná un técnico para habilitar reparación" : undefined}
            type="button"
            >
              {marcandoReparar ? "Avisando..." : "Reparar"}
            </button>
          ) : null}

          {canResolve && !reparacionBloqueadaCotizacion ? (
            <>
              <div className="min-w-[260px]">
                <label className="block text-sm text-gray-600 mb-1">Resolución de reparación</label>
                <select
                  className="border rounded p-2 w-full"
                  value={resolucion}
                  onChange={(e) => setResolucion(e.target.value)}
                  disabled={isEntregadoOBaja}
                >
                  <option value="">-- Seleccionar --</option>
                  {opcionesResolucion.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>

              {String(resolucion) === RESOLUCION.CAMBIO ? (
                <div className="min-w-[260px]">
                  <label className="block text-sm text-gray-600 mb-1">Cambio de equipo</label>
                  <div className="rounded border bg-gray-50 p-2 text-sm">
                    {swapSummary || "Falta elegir el equipo que se entrega."}
                  </div>
                </div>
              ) : null}

              <button
                className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60"
                disabled={savingResol || !resolucion}
                onClick={saveResolucionCambioAware}
                type="button"
              >
                {savingResol
                  ? "Guardando..."
                  : String(resolucion) === RESOLUCION.CAMBIO
                    ? (swapSummary ? "Ver / editar cambio" : "Registrar cambio de equipo")
                    : "Guardar resolución"}
              </button>

              {puedeQuitarReparar ? (
                <button
                  className="border border-amber-600 bg-white text-amber-700 px-3 py-2 rounded disabled:opacity-60"
                  disabled={quitandoReparar}
                  onClick={quitarReparar}
                  type="button"
                  >
                  {quitandoReparar ? "Guardando..." : "Quitar reparar"}
                </button>
              ) : null}
            </>
          ) : null}

          {(typeof canMarkReparado === "boolean" ? canMarkReparado : actAsTech) && !isEstadoBloqueadoReparado ? (
            <>
              <button
                className={`${isReparadoActual ? "border border-emerald-600 bg-white text-emerald-700" : "bg-emerald-600 text-white"} px-3 py-2 rounded disabled:opacity-60`}
                disabled={accionTecnicaOcupada}
                onClick={isReparadoActual ? quitarEstadoFinalReparacion : marcarReparado}
                type="button"
              >
                {isReparadoActual
                  ? (quitandoEstadoFinal ? "Guardando..." : "Quitar reparado")
                  : (marcandoReparado ? "Guardando..." : "Reparado")}
              </button>

              <button
                className={`${isControladoSinDefectoActual ? "border border-blue-600 bg-white text-blue-700" : "bg-blue-600 text-white"} px-3 py-2 rounded disabled:opacity-60`}
                disabled={accionTecnicaOcupada}
                onClick={isControladoSinDefectoActual ? quitarEstadoFinalReparacion : marcarControladoSinDefecto}
                type="button"
              >
                {isControladoSinDefectoActual
                  ? (quitandoEstadoFinal ? "Guardando..." : "Quitar controlado sin defecto")
                  : (savingAll ? "Guardando..." : "Controlado sin defecto")}
              </button>

              <button
                className={`${isNoSeReparaActual ? "border border-gray-700 bg-white text-gray-800" : "bg-gray-700 text-white"} px-3 py-2 rounded disabled:opacity-60`}
                disabled={accionTecnicaOcupada}
                onClick={isNoSeReparaActual ? quitarEstadoFinalReparacion : marcarNoSeRepara}
                type="button"
              >
                {isNoSeReparaActual
                  ? (quitandoEstadoFinal ? "Guardando..." : "Quitar no se repara")
                  : (marcandoNoSeRepara ? "Guardando..." : "No se repara")}
              </button>
            </>
          ) : null}
        </div>
      </div>

      <TechnicalInterventionEditor ingresoId={id} disabled={!canEditDiag} refreshKey={data?.estado}
        equipmentHint={[data?.marca, data?.modelo, data?.equipo_variante].filter(Boolean).join(" | ")}
        onSaved={refreshIngreso} onDirtyChange={setTechnicalDirty} />

      <IngresoPhotos ingresoId={Number(id)} canManage={canManagePhotos} />

      <Row label="Faja de garantía Nro.">
        <input
          className="border rounded p-1 w-60"
          value={fajaGarantiaInput}
          onChange={(e) => setFajaGarantiaInput(e.target.value)}
          onBlur={commitFajaGarantia}
          onKeyDown={blurOnEnter}
          disabled={typeof canEditDiag === "boolean" ? !canEditDiag : false}
        />
      </Row>

      <RejectedBudgetChargeModal
        open={rejectedChargeModalOpen}
        title="Cobro del presupuesto rechazado"
        confirmLabel="Guardar y revisar salida"
        saving={savingResol}
        loading={rejectedChargeModalLoading}
        error={rejectedChargeModalError}
        money={money}
        initialCharge={data?.presupuesto_rechazado_cobro_neto ?? ""}
        showComment={false}
        quoteSummary={rejectedQuoteReference}
        referenceTitle="Presupuesto rechazado a informar en el remito"
        referenceWarning={rejectedChargeReferenceWarning}
        onClose={() => {
          if (savingResol) return;
          setRejectedChargeModalOpen(false);
          setRejectedChargeModalError("");
        }}
        onConfirm={confirmRejectedCharge}
      />
    </div>
  );
}
