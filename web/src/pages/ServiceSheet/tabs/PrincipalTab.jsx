import Row from "../../../components/Row";
import StatusChip from "../../../components/StatusChip";
import DerivadoBadge from "../../../components/DerivadoBadge.jsx";
import { useMemo, useState, useEffect, useCallback } from "react";
import {
  formatDateOnly as formatDateOnlyHelper,
  formatDateTime as formatDateTimeHelper,
  hasMgCode,
  isMgOwned,
  isRentalShelfLocationName,
  isSepidLocationName,
  canonicalLocationName,
  isSaleTicketState,
  bejermanDepositForLocation,
  locationDisplayName,
  locationNameKey,
  propiedadEquipoLabelOf,
  resolveFechaIngreso,
} from "../../../lib/ui-helpers";
import { resolutionLabel } from "../../../lib/constants";
import { isJefe } from "../../../lib/authz";
import { documentNameFromRis, isRisRegistered, risRemitoFrom, risRemitoWithDocumentName } from "../../../lib/ris-print";
import { getReleaseFlow } from "../../../lib/release-flow";
import { getDeviceSwapState, swapConfirmMatches } from "../../../lib/device-swap.js";
import { openPdfBlob } from "../../../lib/pdf";
import { Printer, X } from "lucide-react";
import { ResponsiveModalOverlay, ResponsiveModalPanel } from "../../../components/Responsive.jsx";
import {
  postEntregarIngreso,
  patchIngreso,
  getIngresoBejermanEstado,
  postIngresoBejermanEstadoAccion,
  getBejermanRemitosBySerial,
  getIngresoAlquilerRemitoEstado,
  getIngresoAlquilerRemitoPdfBlob,
  postIngresoAlquilerRemitoAccion,
  checkGarantiaFabrica,
  patchIngresoTecnico,
  postSolicitarAsignacion,
  getAccesoriosCatalogo,
  postAccesorioAlquilerIngreso,
  deleteAccesorioAlquilerIngreso,
} from "../../../lib/api";

const BEJERMAN_ESTADO_LABELS = {
  found: "En stock Bejerman",
  zero_stock: "Sin stock positivo",
  not_found: "No encontrado",
  missing_identifier: "Sin identificador",
};

function bejermanEstadoLabel(status, hasError = false) {
  if (hasError) return "No disponible";
  return BEJERMAN_ESTADO_LABELS[status] || "No disponible";
}

function bejermanEstadoClass(status, hasError = false) {
  if (hasError) return "bg-red-100 text-red-700";
  if (status === "found") return "bg-emerald-100 text-emerald-700";
  if (status === "zero_stock") return "bg-amber-100 text-amber-800";
  if (status === "not_found" || status === "missing_identifier") return "bg-gray-100 text-gray-700";
  return "bg-slate-100 text-slate-700";
}

function formatStockQuantity(value) {
  if (value === null || value === undefined || value === "") return "-";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return numeric.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

function bejermanItemQuantity(item) {
  const available = item?.availableQuantity;
  if (available !== null && available !== undefined && available !== "") return available;
  return item?.realQuantity;
}

// El comprobante dice a quién salió, pero con el código de Bejerman: se muestra la razón
// social, que es lo que el operador reconoce.
function nombreClienteDeCandidato(candidate) {
  return String(candidate?.customer?.name || candidate?.customerName || candidate?.customerCode || "").trim();
}

function textoCantidadRenglon(value) {
  if (value === null || value === undefined || value === "") return "-";
  const numero = Number(value);
  if (!Number.isFinite(numero)) return String(value);
  return numero.toLocaleString("es-AR", { maximumFractionDigits: 4 });
}

// Ficha del comprobante tal como está en Bejerman. Es lo que se mira antes de confirmar:
// el número solo no alcanza para reconocerlo, y una vez asociado el movimiento se deshace
// a mano.
function DetalleRemitoBejerman({ candidate, serial }) {
  const renglones = Array.isArray(candidate?.lines) ? candidate.lines : [];
  const clienteLocal = String(candidate?.customer?.name || "").trim();
  const clienteBejerman = String(candidate?.customerName || "").trim();
  const codigo = String(candidate?.customerCode || "").trim();
  const otros = Number(candidate?.otherItemCount || 0);
  const traeElEquipo = renglones.some((linea) => linea?.isSearchedItem);
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div>
          <div className="text-[11px] font-semibold uppercase text-gray-500">Comprobante</div>
          <div className="font-mono text-sm text-gray-900">{candidate?.remitoNumber || "-"}</div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase text-gray-500">Fecha de emisión</div>
          <div className="text-sm text-gray-900">
            {candidate?.issueDate ? formatDateOnlyHelper(candidate.issueDate) : "-"}
          </div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase text-gray-500">Cliente en Bejerman</div>
          <div className="text-sm text-gray-900">
            {clienteBejerman || "-"}
            {codigo ? <span className="ml-1 text-xs text-gray-500">({codigo})</span> : null}
          </div>
          {candidate?.customerCuit ? (
            <div className="text-xs text-gray-500">CUIT {candidate.customerCuit}</div>
          ) : null}
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase text-gray-500">Cliente en NEXORA</div>
          <div className="text-sm text-gray-900">
            {clienteLocal || <span className="text-amber-700">No está en el maestro de clientes</span>}
          </div>
        </div>
      </div>
      {candidate?.notes ? (
        <div>
          <div className="text-[11px] font-semibold uppercase text-gray-500">Observaciones</div>
          <div className="text-sm text-gray-900">{candidate.notes}</div>
        </div>
      ) : null}
      <div>
        <div className="mb-1 text-[11px] font-semibold uppercase text-gray-500">
          Renglones del remito ({candidate?.lineCount ?? renglones.length})
        </div>
        <div className="overflow-x-auto rounded border">
          <table className="min-w-full text-xs">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="px-2 py-1 text-left">Artículo</th>
                <th className="px-2 py-1 text-left">Descripción</th>
                <th className="px-2 py-1 text-left">Serie / partida</th>
                <th className="px-2 py-1 text-right">Cant.</th>
                <th className="px-2 py-1 text-left">Depósito</th>
              </tr>
            </thead>
            <tbody>
              {renglones.map((linea, index) => (
                <tr
                  key={`${candidate?.remitoNumber}-${linea?.row || index}`}
                  className={linea?.isSearchedItem ? "bg-emerald-50 font-medium text-emerald-900" : "text-gray-800"}
                >
                  <td className="px-2 py-1 font-mono">{linea?.articleCode || "-"}</td>
                  <td className="px-2 py-1">{linea?.description || "-"}</td>
                  <td className="px-2 py-1 font-mono">
                    {linea?.partida || "-"}
                    {linea?.isSearchedItem ? <span className="ml-1 text-[10px] uppercase">(este equipo)</span> : null}
                  </td>
                  <td className="px-2 py-1 text-right">{textoCantidadRenglon(linea?.quantity)}</td>
                  <td className="px-2 py-1">{linea?.warehouse || "-"}</td>
                </tr>
              ))}
              {!renglones.length && (
                <tr>
                  <td className="px-2 py-2 text-gray-600" colSpan={5}>Bejerman no devolvió el detalle de este remito.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {candidate?.linesTruncated && (
          <div className="mt-1 text-xs text-gray-600">Se muestran los primeros renglones del comprobante.</div>
        )}
        {otros > 0 && (
          <div className="mt-1 text-xs text-gray-700">
            El remito lleva {otros === 1 ? "un renglón más" : `${otros} renglones más`} además de este equipo.
          </div>
        )}
        {renglones.length > 0 && !traeElEquipo && (
          <div className="mt-1 text-xs text-amber-700">
            En el detalle no figura la serie {serial || "buscada"}: revisá que sea el remito correcto.
          </div>
        )}
      </div>
    </div>
  );
}

// Busca en Bejerman el remito ya emitido para este equipo. El operador tiene el equipo
// delante, no el número de comprobante: pedirle el número era pedirle lo que no tiene.
// Elegir un candidato no aplica nada por sí solo: abre la ficha del comprobante y recién
// ahí se confirma, porque asociarlo escribe el alquiler y deshacerlo es a mano.
function BuscadorRemitoBejerman({
  flow,
  ingresoId,
  serial,
  onPick,
  disabled = false,
  resumenCambios,
  advertencia = "",
  confirmLabel = "Confirmar y guardar",
}) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [pendiente, setPendiente] = useState(null);
  const [aplicando, setAplicando] = useState(false);
  const [errorConfirmacion, setErrorConfirmacion] = useState("");
  const etiqueta = flow === "rental" ? "RTA" : flow === "sale" ? "RT" : "RSS";

  const buscar = async () => {
    const identificador = String(serial || "").trim();
    if (!identificador) {
      setResult({ status: "sin_serie", detail: "El ingreso no tiene número de serie ni MG cargado." });
      return;
    }
    setLoading(true);
    try {
      setResult(await getBejermanRemitosBySerial({ serial: identificador, flow, ingresoId }));
    } catch (err) {
      setResult({ status: "error", detail: err?.data?.detail || err?.message || "No se pudo consultar Bejerman." });
    } finally {
      setLoading(false);
    }
  };

  const cerrarConfirmacion = () => {
    if (aplicando) return;
    setPendiente(null);
    setErrorConfirmacion("");
  };

  useEffect(() => {
    if (!pendiente) return undefined;
    const alTeclear = (event) => {
      if (event.key === "Escape" && !aplicando) {
        setPendiente(null);
        setErrorConfirmacion("");
      }
    };
    window.addEventListener("keydown", alTeclear);
    return () => window.removeEventListener("keydown", alTeclear);
  }, [pendiente, aplicando]);

  const confirmar = async () => {
    if (!pendiente || aplicando) return;
    setAplicando(true);
    setErrorConfirmacion("");
    try {
      await onPick(pendiente.remitoNumber, pendiente);
      setPendiente(null);
      setResult(null);
    } catch (err) {
      setErrorConfirmacion(err?.data?.detail || err?.message || "No se pudo guardar el remito.");
    } finally {
      setAplicando(false);
    }
  };

  const candidatos = result?.status === "ok" ? result.candidates || [] : [];
  const yaAsociados = result?.status === "ok" ? result.alreadyLinked || [] : [];
  const cambios = pendiente && typeof resumenCambios === "function" ? resumenCambios(pendiente) || [] : [];

  return (
    <div className="mt-1">
      <button
        type="button"
        className="rounded border border-sky-300 px-2 py-1 text-xs text-sky-900 hover:bg-sky-50 disabled:opacity-60"
        onClick={buscar}
        disabled={disabled || loading}
      >
        {loading ? "Buscando..." : `Buscar ${etiqueta} en Bejerman`}
      </button>
      {result && result.status !== "ok" && (
        <div className="mt-1 text-xs text-amber-700">{result.detail || "No se pudo consultar Bejerman."}</div>
      )}
      {result?.status === "ok" && (
        <div className="mt-1 space-y-1">
          {candidatos.map((candidate) => (
            <button
              key={candidate.remitoNumber}
              type="button"
              className="block w-full rounded border border-sky-200 bg-sky-50 px-2 py-1 text-left text-xs hover:bg-sky-100"
              onClick={() => { setErrorConfirmacion(""); setPendiente(candidate); }}
            >
              <span className="font-mono font-semibold">{candidate.remitoNumber}</span>
              <span className="ml-2 text-gray-600">
                {candidate.issueDate}
                {nombreClienteDeCandidato(candidate) ? ` · ${nombreClienteDeCandidato(candidate)}` : ""}
              </span>
              {Number(candidate?.lineCount || 0) > 1 && (
                <span className="ml-2 text-gray-500">{candidate.lineCount} renglones</span>
              )}
              <span className="ml-2 text-sky-800 underline">Ver y confirmar</span>
            </button>
          ))}
          {!candidatos.length && (
            <div className="text-xs text-gray-600">
              {yaAsociados.length
                ? `Ya asociado a esta serie: ${yaAsociados.map((item) => item.remitoNumber).join(", ")}`
                : `Bejerman no tiene ${etiqueta} para este equipo desde su fecha de ingreso.`}
            </div>
          )}
        </div>
      )}
      {pendiente && (
        <ResponsiveModalOverlay align="center" onClick={cerrarConfirmacion}>
          <ResponsiveModalPanel className="max-w-3xl" onClick={(event) => event.stopPropagation()}>
            <div className="flex items-start justify-between gap-4 border-b px-4 py-3">
              <div>
                <h2 className="text-base font-semibold text-gray-900">Confirmar el remito {etiqueta}</h2>
                <p className="text-xs text-gray-600">Revisá el comprobante en Bejerman antes de asociarlo al equipo.</p>
              </div>
              <button
                type="button"
                onClick={cerrarConfirmacion}
                disabled={aplicando}
                className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-900 disabled:opacity-50"
                aria-label="Cerrar"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="space-y-3 px-4 py-3">
              <DetalleRemitoBejerman candidate={pendiente} serial={result?.serial || serial} />
              {cambios.length > 0 && (
                <div className="rounded border border-sky-200 bg-sky-50 p-2">
                  <div className="text-[11px] font-semibold uppercase text-sky-900">Se va a guardar en NEXORA</div>
                  <ul className="mt-1 space-y-0.5 text-xs text-sky-950">
                    {cambios.map((cambio) => (
                      <li key={cambio.campo}>
                        <span className="font-semibold">{cambio.campo}:</span>{" "}
                        <span className="text-gray-600 line-through">{cambio.actual || "vacío"}</span>{" "}
                        <span aria-hidden="true">→</span> <span>{cambio.nuevo || "vacío"}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {typeof resumenCambios === "function" && !cambios.length && (
                <div className="text-xs text-gray-600">
                  Este remito ya figura así en NEXORA: confirmar no cambia nada.
                </div>
              )}
              {advertencia ? <div className="text-xs text-amber-700">{advertencia}</div> : null}
              {errorConfirmacion ? <div className="text-xs text-red-700">{errorConfirmacion}</div> : null}
            </div>
            <div className="flex justify-end gap-2 border-t px-4 py-3">
              <button
                type="button"
                className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                onClick={cerrarConfirmacion}
                disabled={aplicando}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="rounded bg-sky-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-sky-800 disabled:opacity-50"
                onClick={confirmar}
                disabled={aplicando}
              >
                {aplicando ? "Guardando..." : confirmLabel}
              </button>
            </div>
          </ResponsiveModalPanel>
        </ResponsiveModalOverlay>
      )}
    </div>
  );
}

export default function PrincipalTab(props) {
  const {
    id,
    data,
    user,
    release,
    numeroSerie,
    // basics edit
    editBasics,
    formBasics,
    setFormBasics,
    clientes,
    clientesPerm,
    clienteRsInput,
    setClienteRsInput,
    clienteCodInput,
    setClienteCodInput,
    syncClienteFromInputs,
    marcas,
    marcaIdSel,
    setMarcaIdSel,
    modelos,
    modeloIdSel,
    setModeloIdSel,
    tipoSel,
    setTipoSel,
    variantes,
    motivos,
    // ubicacion/tecnico
    ubicaciones,
    ubicacionId,
    setUbicacionId,
    canEditLocation,
    canEditAlquiler,
    canForceBejermanFix,
    canForceBejermanStockFix,
    canManageReleaseResolution,
    canShowRisAction,
    ingressDocumentName: ingressDocumentNameProp,
    risBusy,
    onEmitRis,
    onVerRis,
    onOpenReleaseModal,
    // savingUb,
    // saveUbicacion,
    // ubDirty,
    tecnicos,
    tecnicoId,
    setTecnicoId,
    // saveTecnico,
    // savingTech,
    // techDirty,
    canAssignTecnico,
    isTech,
    userId,
    // entrega
    canEditEntrega,
    editEntrega,
    setEditEntrega,
    entrega,
    setEntrega,
    savingEntrega,
    setSavingEntrega,
    // callbacks and helpers
    patch,
    refreshIngreso,
    setErr,
    setRelatedOpen,
    toDateInputStr,
    maxDateOnly,
  } = props;

  const canUncheckAlquilado = isJefe(user);
  const isRegisteredRis = isRisRegistered(data);
  const risRemito = risRemitoFrom(data);
  const remitoSalidaBejerman = (data?.remito_salida_bejerman || "").toString().trim();
  const remitoSalidaAlquiler = data?.alquilado ? (data?.alquiler_remito || "").toString().trim() : "";
  const remitoSalida = (data?.remito_salida || remitoSalidaAlquiler || remitoSalidaBejerman || "").toString().trim();
  const remitoSalidaFromBejerman = Boolean(remitoSalidaBejerman) && remitoSalidaBejerman === remitoSalida;
  const hasRisRemito = Boolean(risRemito);
  const hasRisError = String(data?.ris?.status || "").toLowerCase() === "failed"
    || String(data?.ris?.pdf_status || "").toLowerCase() === "failed";
  const ingressDocumentName = documentNameFromRis(data, ingressDocumentNameProp || "RIS");
  const releaseFlow = useMemo(
    () => getReleaseFlow(data, { canManageResolution: canManageReleaseResolution }),
    [data, canManageReleaseResolution],
  );
  // Qué equipo hay que entregar cuando la resolución es un cambio.
  const swapEsperado = getDeviceSwapState(data || {}, { canSwap: true }).deliveredLabel;

  // Derivados del catlogo (para filtros de equipo)
  const tiposDisponibles = useMemo(() => {
    const norm = (s) => (s || "").toString().trim().toUpperCase();
    const set = new Set();
    (modelos || []).forEach((m) => {
      const t = norm(m?.tipo_equipo);
      if (t) set.add(t);
    });
    return Array.from(set);
  }, [modelos]);
  const modelosFiltrados = useMemo(() => {
    const norm = (s) => (s || "").toString().trim().toUpperCase();
    const all = modelos || [];
    if (!tipoSel) return all;
    return all.filter((m) => norm(m?.tipo_equipo) === norm(tipoSel));
  }, [modelos, tipoSel]);
  const motivosOptions = useMemo(() => {
    const base = Array.isArray(motivos) ? motivos : [];
    return base
      .map((item) => {
        if (typeof item === "string") return { value: item, label: item };
        const value = (item?.value ?? "").toString();
        const label = (item?.label ?? item?.value ?? "").toString();
        return { value, label };
      })
      .filter((opt) => opt.value);
  }, [motivos]);
  const motivoValue = (formBasics?.motivo ?? data?.motivo ?? "").toString();
  const motivoHasCurrent = useMemo(
    () => motivosOptions.some((opt) => opt.value === motivoValue),
    [motivosOptions, motivoValue]
  );

  // Estados/dirty locales (encapsulados en el tab)
  const [savingTech, setSavingTech] = useState(false);
  const [selTecnicoId, setSelTecnicoId] = useState(tecnicoId ?? null);
  useEffect(() => {
    console.log('[PrincipalTab] sync selTecnicoId from prop', { tecnicoIdProp: tecnicoId });
    setSelTecnicoId(tecnicoId ?? null);
  }, [tecnicoId]);
  const [savingUb, setSavingUb] = useState(false);
  const [ubConfirmOpen, setUbConfirmOpen] = useState(false);
  const [ubNotice, setUbNotice] = useState(null);
  const [mailEnviado, setMailEnviado] = useState(false);
  const [mailFallo, setMailFallo] = useState(false);
  const [emailDebug, setEmailDebug] = useState(null);
  const [solicitando, setSolicitando] = useState(false);
  const [solicitudAsignacionEnviada, setSolicitudAsignacionEnviada] = useState(false);
  const [assignedNameHint, setAssignedNameHint] = useState(null);
  const techDirty = Boolean(
    canAssignTecnico && Number(selTecnicoId ?? -1) !== Number(data?.asignado_a ?? -1)
  );
  useEffect(() => {
    console.log('[PrincipalTab] data asignado_a changed', { asignado_a: data?.asignado_a, asignado_a_nombre: data?.asignado_a_nombre });
  }, [data?.asignado_a, data?.asignado_a_nombre]);
  useEffect(() => {
    const requestedId = Number(data?.tecnico_solicitado_id || 0);
    const currentUserId = Number(userId || 0);
    setSolicitudAsignacionEnviada(Boolean(currentUserId > 0 && requestedId === currentUserId));
  }, [data?.id, data?.tecnico_solicitado_id, userId]);
  useEffect(() => {
    // Si el backend ya refleja el nombre, descartamos el hint local
    if (data?.asignado_a_nombre) setAssignedNameHint(null);
  }, [data?.asignado_a_nombre]);
  useEffect(() => {
    console.log('[PrincipalTab] techDirty recalculated', {
      canAssignTecnico,
      selTecnicoId,
      asignado_a: data?.asignado_a,
      techDirty,
    });
  }, [canAssignTecnico, selTecnicoId, data?.asignado_a, techDirty]);

  useEffect(() => {
    const disabled = (savingTech || !techDirty || selTecnicoId == null);
    console.log('[PrincipalTab] guardar disabled state', { savingTech, techDirty, selTecnicoId, disabled });
  }, [savingTech, techDirty, selTecnicoId]);

  useEffect(() => {
    try {
      console.log('[PrincipalTab] técnicos options', { count: (tecnicos || []).length, ids: (tecnicos || []).map(t => t.id) });
    } catch {}
  }, [tecnicos]);
  const _selUb = (ubicacionId ? Number(ubicacionId) : null);
  const _curUb = (data?.ubicacion_id ?? null);
  const ubDirty = Boolean(canEditLocation && _selUb !== null && _selUb !== _curUb);
  const estadoLower = (data?.estado || "").toLowerCase();
  const presupuestoLower = (data?.presupuesto_estado || "").toLowerCase();
  const isVentaPendienteEntrega = estadoLower === "vendido_pendiente_entrega";
  const mgVendido = Boolean(data?.mg_inactivo_venta)
    || String(data?.mg_estado || "").trim().toLowerCase() === "inactivo_venta"
    || isSaleTicketState(estadoLower);
  const numeroInternoActual = String((editBasics ? formBasics?.numero_interno : data?.numero_interno) || data?.numero_interno || "").trim();
  const equipoOwnership = { ...data, numero_interno: numeroInternoActual };
  const isOwnedByMgCustomer = isMgOwned(equipoOwnership);
  const mgHistorico = Boolean(hasMgCode(equipoOwnership) && mgVendido);
  const propiedadLabel = propiedadEquipoLabelOf(equipoOwnership);
  // El color sale de la etiqueta y no de `isOwnedByMgCustomer`: una devolución de demo es
  // patrimonio nuestro sin ser un MG, y tiene que verse igual de propia que uno.
  const propiedadClass = propiedadLabel === "MG BIO"
    ? "bg-emerald-100 text-emerald-800"
    : mgHistorico
      ? "bg-amber-100 text-amber-800"
    : "bg-gray-100 text-gray-700";
  const mgHistoricoHelp = "Equipo vendido: este MG queda solo como trazabilidad y ya no indica stock propio.";
  const isEntregadoOBaja = ["entregado", "baja"].includes(estadoLower) || isSaleTicketState(estadoLower);
  // Cierre por venta: el equipo salió con un RT emitido por comercial, no con la orden de
  // salida del taller. Por eso se puede registrar sin haber pasado por 'liberado'.
  const [ventaSalida, setVentaSalida] = useState(false);
  // La ruta es /ingresos/:id y ServiceSheet no se remonta al cambiar de OS: sin este reset el
  // tilde se arrastraba a la OS siguiente y le abría el form de entrega en modo venta.
  useEffect(() => {
    setVentaSalida(false);
  }, [data?.id]);
  // Cerrar por venta es registrar una entrega: mismo permiso que "Editar entrega", que el
  // bloque de abajo ya exige. Sin esto el técnico veía el checkbox y comía un 403.
  const puedeCerrarPorVenta =
    canEditEntrega &&
    !data?.derivacion_activa &&
    ["reparado", "controlado_sin_defecto", "no_se_repara", "liberado"].includes(estadoLower);
  const registrandoVenta = Boolean(ventaSalida && puedeCerrarPorVenta);
  const mostrarFormEntrega = estadoLower === "liberado" || isVentaPendienteEntrega || registrandoVenta;
  // Un RTA acredita la salida física del equipo. Una vez asociado, la hoja queda en
  // consulta: cambiar el alquiler o buscar otro comprobante podría deshacer trazabilidad.
  const alquilerBloqueadoPorEntrega = Boolean(data?.alquilado && String(data?.alquiler_remito || "").trim());
  const alquilerEditable = Boolean(canEditAlquiler && !mgVendido && !alquilerBloqueadoPorEntrega);
  const alquilerPuedeDestildarse = Boolean(
    !data?.alquilado || (canUncheckAlquilado && !alquilerBloqueadoPorEntrega)
  );
  const [alquilerAInput, setAlquilerAInput] = useState(data?.alquiler_a || "");
  const [alquilerRemitoInput, setAlquilerRemitoInput] = useState(data?.alquiler_remito || "");
  useEffect(() => {
    setAlquilerAInput(data?.alquiler_a || "");
  }, [data?.id, data?.alquiler_a]);
  useEffect(() => {
    setAlquilerRemitoInput(data?.alquiler_remito || "");
  }, [data?.id, data?.alquiler_remito]);
  const presupuestoLabel = useMemo(() => {
    const v = data?.presupuesto_estado;
    if (!v) return "-";
    if (v === "presupuestado") return "Presupuestado";
    if (v === "no_aplica") return "No aplica";
    try {
      const s = String(v);
      return s.charAt(0).toUpperCase() + s.slice(1);
    } catch (_) {
      return String(v);
    }
  }, [data?.presupuesto_estado]);
  const etapaFlujo = useMemo(() => {
    if (isSaleTicketState(estadoLower)) return "Venta";
    if (["entregado", "alquilado", "baja"].includes(estadoLower)) return "Cierre";
    if (["reparar", "controlado_sin_defecto", "no_se_repara", "reparado", "liberado"].includes(estadoLower)) return "Reparación / Salida";
    if (estadoLower === "presupuestado" || ["emitido", "presupuestado", "aprobado", "rechazado"].includes(presupuestoLower)) return "Presupuesto";
    if (estadoLower === "diagnosticado") return "Diagnóstico";
    if (estadoLower === "ingresado") return "Ingreso";
    return "Ingreso";
  }, [estadoLower, presupuestoLower]);
  const bajaSolicitadaLabel = useMemo(() => {
    if (!data?.baja_solicitada_id) return "";
    const base = data?.baja_solicitada_nombre
      ? `Solicitud de BAJA pendiente: ${data.baja_solicitada_nombre}`
      : `Solicitud de BAJA pendiente (ID ${data.baja_solicitada_id})`;
    const fecha = data?.baja_solicitada_fecha ? formatDateTimeHelper(data.baja_solicitada_fecha) : "";
    return fecha ? `${base} - ${fecha}` : base;
  }, [data?.baja_solicitada_fecha, data?.baja_solicitada_id, data?.baja_solicitada_nombre]);
  const fechasHitos = data?.fechas_hitos || {};
  const firstDateValue = (...values) => values.find((value) => value !== null && value !== undefined && String(value).trim() !== "");
  const fechaAlquiler = firstDateValue(fechasHitos.fecha_alquiler, data?.alquiler_fecha);
  const fechaVentaMg = firstDateValue(fechasHitos.fecha_venta_mg, data?.mg_venta_fecha);
  const fechaBajaSolicitada = firstDateValue(fechasHitos.fecha_baja_solicitada, data?.baja_solicitada_fecha);
  const fechasImportantes = [
    { label: "Creación", value: firstDateValue(fechasHitos.fecha_creacion, data?.fecha_creacion) },
    { label: "Ingreso", value: firstDateValue(fechasHitos.fecha_ingreso, resolveFechaIngreso(data)) },
    // "Servicio" era la fecha de diagnóstico con otro nombre: el backend ya la unifica en
    // fecha_diagnosticado, acá queda el fallback para respuestas viejas del detalle.
    { label: "Diagnóstico", value: firstDateValue(fechasHitos.fecha_diagnosticado, fechasHitos.fecha_servicio, data?.fecha_servicio) },
    { label: "Presupuestado", value: fechasHitos.fecha_presupuestado },
    { label: "Reparación", value: fechasHitos.fecha_reparado },
    { label: "Liberación", value: fechasHitos.fecha_liberacion },
    { label: "Entrega", value: firstDateValue(fechasHitos.fecha_entrega, data?.fecha_entrega) },
    ...(data?.alquilado || fechaAlquiler ? [{ label: "Alquiler", value: fechaAlquiler }] : []),
    ...(data?.mg_inactivo_venta || fechaVentaMg ? [{ label: "Venta MG", value: fechaVentaMg }] : []),
    ...(data?.baja_solicitada_id || fechaBajaSolicitada ? [{ label: "Solicitud baja", value: fechaBajaSolicitada }] : []),
  ];
  const [bejermanEstado, setBejermanEstado] = useState(null);
  const [bejermanEstadoLoading, setBejermanEstadoLoading] = useState(false);
  const [bejermanEstadoError, setBejermanEstadoError] = useState("");
  const cargarEstadoBejerman = useCallback(async () => {
    if (!id) return;
    setBejermanEstadoLoading(true);
    setBejermanEstadoError("");
    try {
      const result = await getIngresoBejermanEstado(id);
      setBejermanEstado(result || null);
    } catch (error) {
      setBejermanEstado(null);
      setBejermanEstadoError(error?.data?.detail || error?.message || "No fue posible consultar Bejerman.");
    } finally {
      setBejermanEstadoLoading(false);
    }
  }, [id]);
  useEffect(() => {
    setBejermanEstado(null);
    setBejermanEstadoError("");
    cargarEstadoBejerman();
  }, [cargarEstadoBejerman, data?.numero_serie, data?.numero_interno, data?.empresa_bejerman]);
  const [bejermanActionBusy, setBejermanActionBusy] = useState(false);
  const forzarAccionBejerman = useCallback(async () => {
    const action = bejermanEstado?.action?.type;
    if (!action || bejermanActionBusy) return;
    const label = bejermanEstado?.action?.label || "esta corrección";
    if (!window.confirm(`Esto va a generar un movimiento real en Bejerman (${label}). ¿Confirmás?`)) return;
    setBejermanActionBusy(true);
    setBejermanEstadoError("");
    try {
      const result = await postIngresoBejermanEstadoAccion(id, { action });
      setBejermanEstado(result?.state || null);
      await refreshIngreso?.({ strong: 1 });
    } catch (error) {
      setBejermanEstadoError(error?.data?.detail || error?.message || "No se pudo ejecutar la corrección en Bejerman.");
    } finally {
      setBejermanActionBusy(false);
    }
  }, [id, bejermanEstado, bejermanActionBusy, refreshIngreso]);
  const bejermanItems = Array.isArray(bejermanEstado?.items) ? bejermanEstado.items : [];
  const [rentalRemitoState, setRentalRemitoState] = useState(null);
  const [rentalRemitoLoading, setRentalRemitoLoading] = useState(false);
  const [rentalRemitoError, setRentalRemitoError] = useState("");
  const [rentalActionBusy, setRentalActionBusy] = useState(false);
  const [rentalPdfLoading, setRentalPdfLoading] = useState(false);
  const equipoIdentificador = String(data?.numero_serie || data?.numero_interno || "").trim();
  const cargarEstadoAlquilerRemito = useCallback(async () => {
    if (!id) return;
    setRentalRemitoLoading(true);
    setRentalRemitoError("");
    try {
      const result = await getIngresoAlquilerRemitoEstado(id);
      setRentalRemitoState(result || null);
    } catch (error) {
      setRentalRemitoState(null);
      setRentalRemitoError(
        error?.data?.detail || error?.message || "No se pudo consultar el remito de alquiler en Bejerman."
      );
    } finally {
      setRentalRemitoLoading(false);
    }
  }, [id]);
  useEffect(() => {
    setRentalRemitoState(null);
    setRentalRemitoError("");
    if (data?.alquilado && (data?.alquiler_remito || "").trim()) {
      cargarEstadoAlquilerRemito();
    }
  }, [cargarEstadoAlquilerRemito, data?.alquilado, data?.alquiler_remito]);
  const forzarAccionAlquilerRemito = useCallback(async () => {
    const action = rentalRemitoState?.action?.type;
    if (!action || rentalActionBusy) return;
    const label = rentalRemitoState?.action?.label || "esta corrección";
    if (!window.confirm(`Esto va a generar un movimiento real en Bejerman (${label}). ¿Confirmás?`)) return;
    setRentalActionBusy(true);
    setRentalRemitoError("");
    try {
      const result = await postIngresoAlquilerRemitoAccion(id, { action });
      setRentalRemitoState(result?.state || null);
      await refreshIngreso?.({ strong: 1 });
    } catch (error) {
      setRentalRemitoError(
        error?.data?.detail || error?.message || "No se pudo ejecutar la corrección en Bejerman."
      );
    } finally {
      setRentalActionBusy(false);
    }
  }, [id, rentalRemitoState, rentalActionBusy, refreshIngreso]);
  const verRta = useCallback(async () => {
    if (rentalPdfLoading) return;
    const printWindow = window.open("", "_blank");
    setRentalPdfLoading(true);
    setRentalRemitoError("");
    try {
      const pdf = await getIngresoAlquilerRemitoPdfBlob(id);
      if (!openPdfBlob(pdf, printWindow)) {
        throw new Error("El navegador bloqueó la ventana del PDF. Habilite las ventanas emergentes e intente nuevamente.");
      }
    } catch (error) {
      try { printWindow?.close(); } catch (_) {}
      setRentalRemitoError(error?.data?.detail || error?.message || "No se pudo abrir el PDF del RTA.");
    } finally {
      setRentalPdfLoading(false);
    }
  }, [id, rentalPdfLoading]);
  const rentalRemitoStatusLabel = (() => {
    const status = rentalRemitoState?.status;
    if (rentalRemitoError) return "No disponible";
    if ({
      empty: 1, blocked: 1, stock_inconsistent: 1, needs_stock_action: 1, verified: 1,
    }[status]) {
      return {
        empty: "Sin remito cargado",
        blocked: "Bloqueado",
        stock_inconsistent: "Stock inconsistente",
        needs_stock_action: "Acción pendiente",
        verified: "Verificado",
      }[status];
    }
    return "Sin verificar";
  })();
  const rentalRemitoStatusClass = (() => {
    const status = rentalRemitoState?.status;
    if (rentalRemitoError) return "bg-red-100 text-red-700";
    if (status === "verified") return "bg-emerald-100 text-emerald-700";
    if (status === "stock_inconsistent" || status === "needs_stock_action") return "bg-amber-100 text-amber-800";
    if (status === "blocked") return "bg-red-100 text-red-700";
    return "bg-slate-100 text-slate-700";
  })();
  // Labels auxiliares (evitar expresiones JSX complejas)
  const pendingLabel = (() => {
    if (data?.tecnico_solicitado_nombre) return `Solicitud de asignación pendiente: ${data.tecnico_solicitado_nombre}`;
    if (data?.tecnico_solicitado_id) return `Solicitud de asignación pendiente (ID ${data.tecnico_solicitado_id})`;
    return "Solicitud de asignación pendiente";
  })();
  const otherTechLabel = (() => {
    const name = data?.tecnico_solicitado_nombre;
    const id = data?.tecnico_solicitado_id;
    const quien = name ? name : (id ? `ID ${id}` : "otro técnico");
    return `Ya hay una solicitud pendiente para ${quien}.`;
  })();
  const requestedTecnicoId = Number(data?.tecnico_solicitado_id || 0);
  const currentUserId = Number(userId || 0);
  const hasOwnAssignmentRequest = Boolean(
    currentUserId > 0 && (requestedTecnicoId === currentUserId || solicitudAsignacionEnviada)
  );
  const hasOtherAssignmentRequest = Boolean(requestedTecnicoId > 0 && requestedTecnicoId !== currentUserId);

  // Cliente: validación contra catálogo (igual que en NuevoIngreso)
  const normClient = useCallback(
    (val) =>
      String(val || "")
        .trim()
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .replace(/\s+/g, " "),
    [],
  );
  const rsMatch = useMemo(() => {
    if (!clienteRsInput) return null;
    const needle = normClient(clienteRsInput);
    if (!needle) return null;
    return (clientes || []).find((c) => normClient(c?.razon_social) === needle || normClient(c?.alias_interno) === needle) || null;
  }, [clienteRsInput, clientes, normClient]);
  const alquilerMatch = useMemo(() => {
    const val = normClient(alquilerAInput);
    if (!val) return null;
    return (clientes || []).find((c) => normClient(c?.razon_social) === val || normClient(c?.alias_interno) === val) || null;
  }, [alquilerAInput, clientes, normClient]);

  const commitAlquilerText = useCallback(async (field, value) => {
    if (!alquilerEditable) return;
    let next = String(value || "").trim();
    if (field === "alquiler_a") {
      const selected = (clientes || []).find((c) => normClient(c?.razon_social) === normClient(next) || normClient(c?.alias_interno) === normClient(next));
      next = selected?.razon_social || next;
    }
    const current = String((field === "alquiler_a" ? data?.alquiler_a : data?.alquiler_remito) || "").trim();
    if (field === "alquiler_a") setAlquilerAInput(next);
    if (field === "alquiler_remito") setAlquilerRemitoInput(next);
    if (next === current) return;
    await patch({ [field]: next });
  }, [alquilerEditable, clientes, data?.alquiler_a, data?.alquiler_remito, normClient, patch]);

  // Al elegir el RTA encontrado se completa también a quién salió: el comprobante ya lo
  // dice y volver a tipearlo a mano es justo donde se perdía el cliente del alquiler.
  // La fecha se pone sólo si estaba vacía, para no pisar una carga deliberada.
  const camposAlquilerDesdeRemito = useCallback((candidato) => {
    const remito = String(candidato?.remitoNumber || "").trim();
    const nombreBejerman = String(candidato?.customer?.name || candidato?.customerName || "").trim();
    const codigoBejerman = String(candidato?.customer?.code || candidato?.customerCode || "").trim();
    const clienteCatalogo = (clientes || []).find((c) => (
      (nombreBejerman && normClient(c?.razon_social) === normClient(nombreBejerman))
      || (codigoBejerman && normClient(c?.cod_empresa) === normClient(codigoBejerman))
    ));
    const cliente = String(clienteCatalogo?.razon_social || nombreBejerman).trim();
    const fecha = String(candidato?.issueDate || "").slice(0, 10);
    const fields = {};
    if (remito && remito !== String(data?.alquiler_remito || "").trim()) fields.alquiler_remito = remito;
    if (cliente && cliente !== String(data?.alquiler_a || "").trim()) fields.alquiler_a = cliente;
    if (/^\d{4}-\d{2}-\d{2}$/.test(fecha) && !String(data?.alquiler_fecha || "").trim()) {
      fields.alquiler_fecha = fecha;
    }
    return { fields, remito, cliente };
  }, [clientes, data?.alquiler_a, data?.alquiler_fecha, data?.alquiler_remito, normClient]);

  // Lo que va a quedar escrito, campo por campo: el operador confirma sobre esto, no sobre
  // un numero suelto.
  const resumenAlquilerDesdeRemito = useCallback((candidato) => {
    const { fields } = camposAlquilerDesdeRemito(candidato);
    const etiquetas = { alquiler_remito: "Remito", alquiler_a: "¿A quién?", alquiler_fecha: "Fecha" };
    const actuales = {
      alquiler_remito: String(data?.alquiler_remito || "").trim(),
      alquiler_a: String(data?.alquiler_a || "").trim(),
      alquiler_fecha: String(data?.alquiler_fecha || "").slice(0, 10),
    };
    return Object.keys(fields).map((campo) => ({
      campo: etiquetas[campo] || campo,
      actual: campo === "alquiler_fecha" && actuales[campo] ? formatDateOnlyHelper(actuales[campo]) : actuales[campo],
      nuevo: campo === "alquiler_fecha" ? formatDateOnlyHelper(fields[campo]) : String(fields[campo] || ""),
    }));
  }, [camposAlquilerDesdeRemito, data?.alquiler_a, data?.alquiler_fecha, data?.alquiler_remito]);

  const aplicarRemitoAlquilerEncontrado = useCallback(async (_numero, candidato) => {
    if (!alquilerEditable) return;
    const { fields, remito, cliente } = camposAlquilerDesdeRemito(candidato);
    if (remito) setAlquilerRemitoInput(remito);
    if (cliente) setAlquilerAInput(cliente);
    if (Object.keys(fields).length) await patch(fields);
  }, [alquilerEditable, camposAlquilerDesdeRemito, patch]);

  const blurOnEnter = useCallback((event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    event.currentTarget.blur();
  }, []);

  // Catálogo de accesorios (para alquiler)
  const [accesCatalogo, setAccesCatalogo] = useState([]);
  const [nuevoAccAlq, setNuevoAccAlq] = useState({ descripcion: "", referencia: "" });
  const [addingAccAlq, setAddingAccAlq] = useState(false);
  const [deletingAccAlqId, setDeletingAccAlqId] = useState(null);
  useEffect(() => {
    if (!alquilerEditable) return;
    (async () => { try { setAccesCatalogo(await getAccesoriosCatalogo()); } catch {} })();
  }, [alquilerEditable]);

  async function addAccesorioAlquiler() {
    try {
      const d = (nuevoAccAlq?.descripcion || "").trim().toLowerCase();
      if (!d) { setErr && setErr("Escriba una descripción"); return; }
      const acc = (accesCatalogo || []).find(a => (a?.nombre || "").trim().toLowerCase() === d);
      if (!acc) { setErr && setErr("Elija una descripción válida de la lista"); return; }
      setAddingAccAlq(true);
      await postAccesorioAlquilerIngreso(id, {
        accesorio_id: Number(acc.id),
        referencia: (nuevoAccAlq?.referencia || "").trim() || null,
      });
      setNuevoAccAlq({ descripcion: "", referencia: "" });
      await refreshIngreso();
      setErr && setErr("");
    } catch (e) {
      setErr && setErr(e?.message || "No se pudo agregar el accesorio de alquiler");
    } finally {
      setAddingAccAlq(false);
    }
  }

  async function removeAccesorioAlquiler(itemId) {
    try {
      setDeletingAccAlqId(itemId);
      await deleteAccesorioAlquilerIngreso(id, itemId);
      await refreshIngreso();
      setErr && setErr("");
    } catch (e) {
      setErr && setErr(e?.message || "No se pudo quitar el accesorio de alquiler");
    } finally {
      setDeletingAccAlqId(null);
    }
  }

  async function saveTecnico() {
    console.log('[PrincipalTab] saveTecnico called', { canAssignTecnico, selTecnicoId, asignado_a: data?.asignado_a, techDirty, id });
    if (!canAssignTecnico || selTecnicoId == null) { console.log('[PrincipalTab] saveTecnico aborted: sin permiso o sin selección', { canAssignTecnico, selTecnicoId }); return; }
    if (!techDirty) { console.log('[PrincipalTab] saveTecnico aborted: sin cambios', { selTecnicoId, current: data?.asignado_a }); return; }
    try {
      setSavingTech(true);
      console.log('[PrincipalTab] calling patchIngresoTecnico', { id, selTecnicoId: Number(selTecnicoId) });
      const resp = await patchIngresoTecnico(id, Number(selTecnicoId));
      console.log('[PrincipalTab] patchIngresoTecnico done', resp);
      try {
        setMailEnviado(!!(resp && resp.email_sent));
      } catch {}
      try {
        const name = resp && (resp.asignado_a_nombre || resp.nombre);
        if (name) setAssignedNameHint(name);
        else {
          const t = (tecnicos || []).find(x => Number(x.id) === Number(selTecnicoId));
          if (t && t.nombre) setAssignedNameHint(t.nombre);
        }
      } catch {}
      await refreshIngreso({ strong: 1 });
      console.log('[PrincipalTab] refreshIngreso done');
      setErr("");
    } catch (e) {
      console.log('[PrincipalTab] saveTecnico error', e);
      setErr(e?.message || "No se pudo asignar el técnico");
    } finally {
      console.log('[PrincipalTab] saveTecnico finally -> setSavingTech(false)');
      setSavingTech(false);
    }
  }

  // Las variantes históricas de la estantería sólo deben aparecer una vez. El guion es
  // una ubicación virtual para estados cerrados, no un destino físico seleccionable.
  const ubicacionesSeleccionables = useMemo(() => {
    const seen = new Set();
    return (ubicaciones || []).filter((ubicacion) => {
      const nombre = String(ubicacion?.nombre || "").trim();
      const key = locationNameKey(canonicalLocationName(nombre));
      if (!nombre || key === "-" || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [ubicaciones]);

  const ubicacionDestino = ubicacionesSeleccionables.find((u) => Number(u?.id) === Number(_selUb));
  const ubicacionDestinoNombre = ubicacionDestino?.nombre || "";
  const ubicacionOrigenNombre = String(data?.ubicacion_nombre || "").trim();
  const depositoOrigenBejerman = bejermanDepositForLocation(ubicacionOrigenNombre);
  const depositoDestinoBejerman =
    String(ubicacionDestino?.bejerman_deposit || "").trim() || bejermanDepositForLocation(ubicacionDestinoNombre);
  const cambiaDepositoBejerman = Boolean(
    depositoOrigenBejerman &&
    depositoDestinoBejerman &&
    depositoOrigenBejerman !== depositoDestinoBejerman
  );
  const ubicacionDestinoEsEstanteria = isRentalShelfLocationName(ubicacionDestinoNombre);

  async function saveUbicacion() {
    if (!canEditLocation || !ubDirty) return;
    setUbNotice(null);
    setUbConfirmOpen(true);
  }

  async function guardarUbicacion() {
    try {
      setSavingUb(true);
      const res = await patch({
        ubicacion_id: _selUb,
        confirm_bejerman_location_change: true,
      });
      const locationSync = res?.bejerman_location_sync;
      const shelf = res?.rental_shelf;
      if (locationSync?.queued) {
        setUbNotice({
          tone: "ok",
          text: `Ubicación guardada. Se encoló el traslado de stock ${locationSync.source_deposit} → ${locationSync.target_deposit} en Bejerman.`,
        });
      } else if (shelf && !shelf.applies) {
        setUbNotice({
          tone: "warn",
          text: `Ubicación guardada. El stock no se mueve a STL: ${shelf.reason || "el equipo no es del parque de alquiler."}`,
        });
      } else if (shelf && shelf.stock_queued) {
        setUbNotice({ tone: "ok", text: "Ubicación guardada. Se pidió el pase del stock a STL en Bejerman." });
      } else if (shelf) {
        setUbNotice({
          tone: "error",
          text: `Ubicación guardada, pero no se pudo pedir el pase del stock a STL: ${shelf.stock_error || "error desconocido"}`,
        });
      }
      setErr("");
      if (typeof refreshIngreso === "function") await refreshIngreso({ strong: 1 });
    } catch (e) {
      setErr(e?.message || "No se pudo actualizar la ubicación.");
    } finally {
      setSavingUb(false);
      setUbConfirmOpen(false);
    }
  }

  // Auto-chequeo de garantía de fábrica cuando se edita N/S
  useEffect(() => {
    if (!editBasics) return;
    const ns = (formBasics?.numero_serie || "").trim();
    const marcaName = (formBasics?.marca || data?.marca || "").toString();
    if (!ns) {
      if (formBasics) setFormBasics((s) => ({ ...(s || {}), garantia: false }));
      return;
    }
    const h = setTimeout(async () => {
      try {
        const r = await checkGarantiaFabrica(ns, marcaName, {
          brand_id: data?.marca_id ?? null,
          model_id: data?.model_id ?? null,
        });
        if (typeof r?.within_365_days !== "boolean") {
          setFormBasics((s) => ({ ...(s || {}), garantia: false }));
          return;
        }
        // Fecha estimada por remito: se deja como está y decide la persona.
        if (r?.estimated === true) return;
        const enGarantia = r.within_365_days;
        setFormBasics((s) => ({ ...(s || {}), garantia: enGarantia }));
      } catch {
        /* noop */
      }
    }, 400);
    return () => clearTimeout(h);
  }, [editBasics, formBasics?.numero_serie, formBasics?.marca, data?.marca, data?.marca_id, data?.model_id]);

  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Columna izquierda: Cliente/Equipo/Notas */}
        <div className="border rounded p-4">
          <h2 className="font-semibold mb-2">Cliente</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
            <Row label="Razón social">
              {editBasics ? (
                <>
                  <input
                    className="border rounded p-1 w-64"
                    list={clientesPerm ? "service_clientes_rs" : undefined}
                    autoComplete="off"
                    value={clienteRsInput}
                    onChange={(e) => {
                      const v = e.target.value;
                      const selected = (clientes || []).find((c) => normClient(c?.razon_social) === normClient(v) || normClient(c?.alias_interno) === normClient(v));
                      setClienteRsInput(v);
                      const nextCod = selected?.cod_empresa || "";
                      setClienteCodInput(nextCod);
                      syncClienteFromInputs(v, nextCod);
                    }}
                    placeholder="Elija de la lista"
                  />
                  {clientesPerm && (
                    <datalist id="service_clientes_rs">
                      {(clientes || []).map((c) => (
                        <option key={`rs-${c.id}`} value={c.razon_social} label={[c.alias_interno, c.cod_empresa].filter(Boolean).join(" - ")} />
                      ))}
                      {(clientes || []).filter((c) => c.alias_interno).map((c) => (
                        <option key={`alias-${c.id}`} value={c.alias_interno} label={c.razon_social} />
                      ))}
                    </datalist>
                  )}
                  {clientesPerm && clienteRsInput && !rsMatch && (
                    <div className="text-xs text-amber-700 mt-1">Seleccione una razón social válida de la lista.</div>
                  )}
                </>
              ) : (
                data.razon_social
              )}
            </Row>
            <Row label="Código empresa">
              {editBasics ? (
                <>
                  <input
                    className="border rounded p-1 w-40 bg-gray-50 text-gray-700"
                    value={clienteCodInput}
                    readOnly
                    tabIndex={-1}
                    placeholder="Se completa solo"
                  />
                </>
              ) : (
                data.cod_empresa || "-"
              )}
            </Row>
            <Row label="Teléfono">
              {editBasics ? (
                <input
                  className="border rounded p-1 w-48"
                  value={formBasics?.telefono ?? ""}
                  onChange={(e) => setFormBasics((s) => ({ ...s, telefono: e.target.value }))}
                />
              ) : (
                data.telefono || "-"
              )}
            </Row>
          </div>

          {(editBasics || data.propietario_nombre || data.propietario_contacto || data.propietario_doc) && (
            <>
              <h2 className="font-semibold mt-4 mb-2">Propietario</h2>
              <Row label="Nombre">
                {editBasics ? (
                  <input
                    className="border rounded p-1 w-64"
                    value={formBasics?.propietario_nombre ?? ""}
                    onChange={(e) => setFormBasics((s) => ({ ...s, propietario_nombre: e.target.value }))}
                  />
                ) : (
                  data.propietario_nombre || "-"
                )}
              </Row>
              <Row label="Contacto">
                {editBasics ? (
                  <input
                    className="border rounded p-1 w-64"
                    value={formBasics?.propietario_contacto ?? ""}
                    onChange={(e) => setFormBasics((s) => ({ ...s, propietario_contacto: e.target.value }))}
                  />
                ) : (
                  data.propietario_contacto || "-"
                )}
              </Row>
              <Row label="CUIT">
                {editBasics ? (
                  <input
                    className="border rounded p-1 w-64"
                    value={formBasics?.propietario_doc ?? ""}
                    onChange={(e) => setFormBasics((s) => ({ ...s, propietario_doc: e.target.value }))}
                  />
                ) : (
                  data.propietario_doc || "-"
                )}
              </Row>
            </>
          )}

          <div className="mt-4 mb-2 flex flex-wrap items-center gap-2">
            <h2 className="font-semibold">Equipo</h2>
            <span className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${propiedadClass}`}>
              Propiedad: {propiedadLabel}
            </span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
            <Row label="Tipo de equipo">
              {editBasics ? (
                <select
                  className="border rounded p-1 w-60"
                  value={tipoSel}
                  onChange={(e) => { setTipoSel(e.target.value); setModeloIdSel(null); }}
                >
                  <option value="">(todos)</option>
                  {tiposDisponibles.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              ) : (
                data.tipo_equipo_nombre || data.tipo_equipo || "-"
              )}
            </Row>
            {/* Marca / Modelo */}
            <Row label="Marca">
              {editBasics ? (
                <select
                  className="border rounded p-1 w-60"
                  value={marcaIdSel == null ? "" : String(marcaIdSel)}
                  onChange={(e) => { const v = e.target.value === "" ? null : Number(e.target.value); setMarcaIdSel(v); setModeloIdSel(null); }}
                >
                  <option value="">(sin marca)</option>
                  {(marcas || []).map((m) => (
                    <option key={m.id} value={String(m.id)}>
                      {m.nombre}
                    </option>
                  ))}
                </select>
              ) : (
                data.marca
              )}
            </Row>
            <Row label="Modelo">
              {editBasics ? (
                <select
                  className="border rounded p-1 w-60"
                  value={modeloIdSel == null ? "" : String(modeloIdSel)}
                  onChange={(e) => setModeloIdSel(e.target.value === "" ? null : Number(e.target.value))}
                >
                  <option value="">(sin modelo)</option>
                  {(modelosFiltrados || []).map((m) => (
                    <option key={m.id} value={String(m.id)}>
                      {m.nombre}
                    </option>
                  ))}
                </select>
              ) : (
                data.modelo || "-"
              )}
            </Row>
            <Row label="Variante">
              {editBasics ? (
                <>
                  <input
                    list="variantesOptions"
                    className="border rounded p-1 w-60"
                    value={formBasics?.equipo_variante ?? ""}
                    onChange={(e) => setFormBasics((f) => ({ ...(f || {}), equipo_variante: e.target.value }))}
                  />
                  <datalist id="variantesOptions">
                    {(variantes || []).map((v, idx) => (
                      <option key={idx} value={v} />
                    ))}
                  </datalist>
                </>
              ) : (
                data.equipo_variante || "-"
              )}
            </Row>
            <Row label="Garantía (fábrica)">
              {editBasics ? (
                <input
                  type="checkbox"
                  checked={!!(formBasics?.garantia)}
                  onChange={(e) => setFormBasics((s) => ({ ...(s || {}), garantia: e.target.checked }))}
                />
              ) : (
                <span>{data.garantia ? "Sí" : "No"}</span>
              )}
            </Row>
            <Row label={"N° serie"}>
              {editBasics ? (
                <input
                  className="border rounded p-1 w-60"
                  value={formBasics?.numero_serie ?? ""}
                  onChange={(e) => setFormBasics((s) => ({ ...s, numero_serie: e.target.value }))}
                />
              ) : (
                <span>{numeroSerie || "-"}</span>
              )}
            </Row>
            <Row label="Garantía de reparación">
              {editBasics ? (
                <input
                  type="checkbox"
                  checked={!!(formBasics?.garantia_reparacion)}
                  onChange={(e) => setFormBasics((s) => ({ ...(s || {}), garantia_reparacion: e.target.checked }))}
                />
              ) : (
                <span>{data?.garantia_reparacion ? "Sí" : "No"}</span>
              )}
            </Row>
            <Row label={"N° interno (MG)"}>
              {editBasics ? (
                <div>
                  <input
                    className="border rounded p-1 w-60 disabled:bg-gray-100 disabled:text-gray-500"
                    value={formBasics?.numero_interno || ""}
                    onChange={(e) => setFormBasics((s) => ({ ...s, numero_interno: e.target.value }))}
                    disabled={mgVendido}
                  />
                  {mgHistorico && (
                    <div className="mt-1 max-w-md text-xs text-amber-700">
                      {mgHistoricoHelp}
                    </div>
                  )}
                </div>
              ) : (
                <div>
                  <span>{data.numero_interno || "-"}</span>
                  {mgHistorico && (
                    <div className="mt-1 max-w-md text-xs text-amber-700">
                      {mgHistoricoHelp}
                    </div>
                  )}
                </div>
              )}
            </Row>
            <Row label="Remito ingreso">
              {editBasics ? (
                <input
                  className="border rounded p-1 w-60"
                  value={formBasics?.remito_ingreso ?? ""}
                  onChange={(e) => setFormBasics((s) => ({ ...s, remito_ingreso: e.target.value }))}
                />
              ) : (
                <span className="inline-flex flex-col items-start gap-1">
                  <span>{risRemito || data.remito_ingreso || "-"}</span>
                  {data?.ris?.available && (
                    <span className="block text-xs text-gray-500">
                      {isRegisteredRis
                        ? "Remito registrado"
                        : data?.ris?.remito_number
                          ? risRemitoWithDocumentName(data.ris.remito_number, ingressDocumentName)
                          : `${ingressDocumentName}: ${data?.ris?.status || "pendiente"}`}
                    </span>
                  )}
                  {!isRegisteredRis && canShowRisAction && (typeof onEmitRis === "function" || typeof onVerRis === "function") && (
                    <button
                      type="button"
                      onClick={hasRisRemito ? (onVerRis || onEmitRis) : onEmitRis}
                      disabled={risBusy}
                      className="mt-1 inline-flex items-center gap-1 rounded border border-sky-200 px-2 py-1 text-xs font-medium text-sky-700 hover:bg-sky-50 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <Printer className="h-3.5 w-3.5" aria-hidden="true" />
                      {risBusy
                        ? `Preparando ${ingressDocumentName}...`
                        : hasRisRemito
                          ? `Ver ${ingressDocumentName}`
                          : hasRisError
                            ? `Reintentar ${ingressDocumentName}`
                            : `Emitir ${ingressDocumentName}`}
                    </button>
                  )}
                </span>
              )}
            </Row>
            <Row label="Remito salida">
              <span className="inline-flex flex-col items-start gap-1">
                <span>{remitoSalida || "-"}</span>
                {remitoSalidaFromBejerman && remitoSalida && (
                  <span className="block text-xs text-gray-500">RSS Bejerman</span>
                )}
                {remitoSalidaAlquiler && remitoSalida === remitoSalidaAlquiler && (
                  <span className="block text-xs text-gray-500">RTA de alquiler</span>
                )}
              </span>
            </Row>
            <Row label={"Faja de garantía"}>
              {editBasics ? (
                <select
                  className="border rounded p-1 w-60"
                  value={formBasics?.etiq_garantia_ok ? "ok" : "abiertas"}
                  onChange={(e) => setFormBasics((s) => ({ ...(s || {}), etiq_garantia_ok: e.target.value === "ok" }))}
                >
                  <option value="ok">OK</option>
                  <option value="abiertas">Abiertas</option>
                </select>
              ) : (
                <span>{data?.etiq_garantia_ok ? "OK" : "Abiertas"}</span>
              )}
            </Row>
          </div>
          {/* Notas */}
          <h2 className="font-semibold mt-4 mb-2">Notas</h2>
          <Row label="Informe preliminar">
            {editBasics ? (
              <textarea
                className="border rounded p-2 w-full min-h-[100px]"
                value={formBasics?.informe_preliminar ?? ""}
                onChange={(e) => setFormBasics((s) => ({ ...s, informe_preliminar: e.target.value }))}
              />
            ) : (
              <div className="whitespace-pre-wrap">{data.informe_preliminar || "-"}</div>
            )}
          </Row>
          <Row label="Accesorios">
            {Array.isArray(data.accesorios_items) && data.accesorios_items.length > 0 ? (
              <ul className="list-disc list-inside">
                {data.accesorios_items.map((it) => (
                  <li key={it.id}>
                    {it.accesorio_nombre}
                    {it.referencia ? ` (ref: ${it.referencia})` : ""}
                  </li>
                ))}
              </ul>
            ) : (
              data.accesorios || "-"
            )}
          </Row>
        </div>

        {/* Columna derecha: Estado/Asignación/ubicación */}
        <div className="border rounded p-4">
          <h2 className="font-semibold mb-2">Estado</h2>
          <div className="mb-3 rounded-lg border border-slate-200 bg-slate-50 p-3">

            {bajaSolicitadaLabel && (
              <div className="mt-2 text-xs font-medium text-amber-700">{bajaSolicitadaLabel}</div>
            )}
            {data?.baja_solicitada_motivo && (
              <div className="mt-1 text-xs text-amber-800 whitespace-pre-wrap">
                Motivo: {data.baja_solicitada_motivo}
              </div>
            )}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
            <Row label="Motivo">
              {editBasics ? (
                <select
                  className="border rounded p-1 w-60"
                  value={motivoValue}
                  onChange={(e) => setFormBasics((s) => ({ ...(s || {}), motivo: e.target.value }))}
                >
                  <option value="">Seleccionar motivo</option>
                  {!motivoHasCurrent && motivoValue && (
                    <option value={motivoValue}>{motivoValue}</option>
                  )}
                  {motivosOptions.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              ) : (
                data.motivo || "-"
              )}
            </Row>
            <Row label="Estado"><StatusChip value={data?.estado} title="Estado del equipo" /></Row>
            <Row label="Presupuesto"><StatusChip value={data?.presupuesto_estado} title={presupuestoLabel} /></Row>
            <Row label="Resolución">{data.resolucion ? resolutionLabel(data.resolucion) : "-"}</Row>
            <Row label="Derivado">
              {data?.derivacion_activa ? (
                <span className="flex flex-wrap items-center gap-1.5">
                  <DerivadoBadge row={data} />
                  <span>
                    {data?.derivacion_proveedor || "Proveedor sin identificar"}
                    {data?.derivacion_fecha ? ` · desde ${formatDateOnlyHelper(data.derivacion_fecha)}` : ""}
                  </span>
                </span>
              ) : "No"}
            </Row>
          </div>
          <div className="mt-3 border-t pt-3">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Fechas importantes</div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3 xl:grid-cols-4">
              {fechasImportantes.map((item) => (
                <div key={item.label} className="min-w-0">
                  <span className="text-gray-500">{item.label}: </span>
                  <span className="font-medium text-gray-900">{item.value ? formatDateOnlyHelper(item.value) : "-"}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="mt-3 border-t pt-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">Bejerman</div>
              <button
                type="button"
                className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-60"
                onClick={cargarEstadoBejerman}
                disabled={bejermanEstadoLoading}
              >
                {bejermanEstadoLoading ? "Consultando..." : "Actualizar"}
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className={`rounded-full px-2 py-0.5 font-semibold ${bejermanEstadoClass(bejermanEstado?.status, Boolean(bejermanEstadoError))}`}>
                {bejermanEstadoLoading && !bejermanEstado ? "Consultando Bejerman" : bejermanEstadoLabel(bejermanEstado?.status, Boolean(bejermanEstadoError))}
              </span>
              {bejermanEstado?.companyKey && (
                <span className="text-gray-600">Empresa: <span className="font-medium text-gray-900">{bejermanEstado.companyKey}</span></span>
              )}
              {bejermanEstado?.identifier && (
                <span className="text-gray-600">
                  Partida: <span className="font-medium text-gray-900">{bejermanEstado.identifier}</span>
                  {bejermanEstado.identifierSource === "numero_interno" ? " (MG)" : ""}
                </span>
              )}
              {bejermanEstado?.checkedAt && (
                <span className="text-gray-500">Consultado: {formatDateTimeHelper(bejermanEstado.checkedAt)}</span>
              )}
            </div>
            {bejermanEstadoError ? (
              <div className="mt-2 text-xs text-red-700">{bejermanEstadoError}</div>
            ) : bejermanEstado?.warning ? (
              <div className="mt-2 text-xs text-amber-700">{bejermanEstado.warning}</div>
            ) : null}
            {canForceBejermanStockFix && bejermanEstado?.action && (
              <button
                type="button"
                className="mt-2 rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-60"
                onClick={forzarAccionBejerman}
                disabled={bejermanActionBusy}
              >
                {bejermanActionBusy ? "Ejecutando..." : bejermanEstado.action.label}
              </button>
            )}
            {bejermanItems.length > 0 && (
              <div className="mt-2 overflow-x-auto">
                <div className="min-w-[520px] text-xs">
                  <div className="grid grid-cols-[1fr_1.5fr_1fr_0.8fr_0.8fr_0.8fr] gap-2 border-b pb-1 font-semibold text-gray-500">
                    <div>Depósito</div>
                    <div>Artículo</div>
                    <div>Partida</div>
                    <div>Real</div>
                    <div>Comp.</div>
                    <div>Disp.</div>
                  </div>
                  {bejermanItems.map((item, idx) => (
                    <div
                      key={`${item.depositCode || "dep"}-${item.articleCode || "art"}-${item.partida || "partida"}-${idx}`}
                      className="grid grid-cols-[1fr_1.5fr_1fr_0.8fr_0.8fr_0.8fr] gap-2 border-b py-1 last:border-b-0"
                    >
                      <div className="min-w-0">
                        <div className="font-medium text-gray-900">{item.depositCode || "-"}</div>
                        {item.depositName && <div className="truncate text-gray-500">{item.depositName}</div>}
                      </div>
                      <div className="min-w-0">
                        <div className="truncate font-medium text-gray-900">{item.articleCode || "-"}</div>
                        {item.articleDescription && <div className="truncate text-gray-500">{item.articleDescription}</div>}
                      </div>
                      <div className="min-w-0">
                        <div className="truncate text-gray-900">{item.partida || "-"}</div>
                        {item.expirationDate && <div className="text-gray-500">Vto. {formatDateOnlyHelper(item.expirationDate)}</div>}
                      </div>
                      <div className="text-gray-900">{formatStockQuantity(item.realQuantity)}</div>
                      <div className="text-gray-900">{formatStockQuantity(item.committedQuantity)}</div>
                      <div className="font-medium text-gray-900">{formatStockQuantity(bejermanItemQuantity(item))}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
          <div className="mt-1 text-xs text-gray-500">
            Ingresado por: {data?.ingresado_por_nombre || (data?.ingresado_por_id ? `ID ${data.ingresado_por_id}` : "-")}
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mt-4">
            <div>
              <h2 className="font-semibold mb-2">Asignación</h2>
              <div className="flex flex-col items-start gap-2">
                {canAssignTecnico ? (
                  <>
                    <select
                      className="border rounded p-2"
                      value={selTecnicoId == null ? "" : String(selTecnicoId)}
                      onChange={(e) => {
                        const v = e.target.value === "" ? null : Number(e.target.value);
                        console.log('[PrincipalTab] select técnico change', { prev: selTecnicoId, next: v });
                        setSelTecnicoId(v);
                        setTecnicoId(v);
                      }}
                    >
                      <option value="">-- Seleccionar técnico --</option>
                      {tecnicos.map((t) => (
                        <option key={t.id} value={String(t.id)}>
                          {t.nombre}
                        </option>
                      ))}
                    </select>
                    <button
                      className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60"
                      onClick={saveTecnico}
                      disabled={savingTech || !techDirty || selTecnicoId == null}
                      aria-busy={savingTech ? "true" : "false"}
                      type="button"
                    >
                      {savingTech ? "Guardando..." : "Guardar"}
                    </button>
                    {mailEnviado && (
                      <div className="text-xs text-emerald-700">Se envió el mail</div>
                    )}
                    {(data?.tecnico_solicitado_id && data?.tecnico_solicitado_id !== (data?.asignado_a ?? null)) && (
                      <div className="text-xs text-amber-700 mt-1">{pendingLabel}</div>
                    )}
                  </>
                ) : (
                  <div className="text-sm text-gray-500">
                    {mailEnviado && (<div className="text-xs text-emerald-700">Se envió el mail</div>)}
                    {!mailEnviado && mailFallo && (
                      <div>
                        <div className="text-xs text-amber-700">La solicitud quedó registrada. No se pudo enviar el aviso por email.</div>
                        {emailDebug && (
                          <div className="mt-1 text-[11px] text-gray-600">
                            <div>Destino: {(emailDebug.recipients || []).join(", ") || "-"}</div>
                            <div>Backend: {emailDebug.backend || "-"}</div>
                            <div>SMTP: {emailDebug.host || "-"}:{String(emailDebug.port || "")} TLS:{String(emailDebug.use_tls ?? "")} SSL:{String(emailDebug.use_ssl ?? "")}</div>
                            {emailDebug.error && (<div>Error: {emailDebug.error}</div>)}
                          </div>
                        )}
                      </div>
                    )}
                    <div>No tiene permiso para reasignar técnicos.</div>
                    {isTech && !isEntregadoOBaja && Number(userId || 0) > 0 && (
                      <div className="mt-2">
                        {data?.asignado_a === userId ? (
                          <div className="text-xs text-gray-600">Ya estás asignado a este ingreso.</div>
                        ) : hasOwnAssignmentRequest ? (
                          <div className="text-xs text-amber-700">Solicitud de asignación enviada</div>
                        ) : hasOtherAssignmentRequest ? (
                          <div className="text-xs text-gray-600">{otherTechLabel}</div>
                        ) : (
                          <button hidden={mailEnviado || solicitudAsignacionEnviada}
                            className="bg-neutral-800 text-white px-3 py-2 rounded disabled:opacity-60"
                            disabled={solicitando || solicitudAsignacionEnviada}
                            onClick={async () => {
                              try {
                                setSolicitando(true);
                                setMailEnviado(false);
                                setMailFallo(false);
                                const r = await postSolicitarAsignacion(id);
                                if (r && r.ok) {
                                  setSolicitudAsignacionEnviada(true);
                                }
                                setMailEnviado(!!(r && r.email_sent));
                                if (r && r.ok && !r.already_pending && r.email_sent === false) {
                                  setMailFallo(true);
                                }
                                setEmailDebug(r && r.email_debug ? r.email_debug : null);
                                setErr("");
                                await refreshIngreso();
                              } catch (e) {
                                setErr(e?.message || "No se pudo registrar la solicitud de asignación");
                              } finally {
                                setSolicitando(false);
                              }
                            }}
                            type="button"
                          >
                            {solicitando ? "Enviando..." : "Solicitar asignación"}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )}
                <div className="text-xs text-gray-500">
                  Actual: <b>{assignedNameHint || data.asignado_a_nombre || "-"}</b>
                </div>
              </div>
            </div>
            <div>
              <h2 className="font-semibold mb-2">Ubicación</h2>
              {canEditLocation ? (
                <>
                  <div className="flex flex-col items-start gap-2">
                    <select
                      className="border rounded p-2"
                      value={ubicacionId}
                      onChange={(e) => setUbicacionId(e.target.value)}
                      aria-label="Seleccionar ubicación"
                    >
                      <option value="" disabled>
                        Seleccione la ubicación.
                      </option>
                    {!ubicacionesSeleccionables.some((u) => String(u.id) === String(ubicacionId)) && ubicacionId && (
                      <option value={String(ubicacionId)} disabled>
                        {data?.ubicacion_nombre ? locationDisplayName(data.ubicacion_nombre) : "Sin ubicación"}
                      </option>
                    )}
                    {ubicacionesSeleccionables.map((u) => (
                      <option key={u.id} value={String(u.id)}>
                          {u.label || locationDisplayName(u.nombre)}
                      </option>
                    ))}
                    </select>
                    <button
                      className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60"
                      onClick={saveUbicacion}
                      disabled={savingUb || !ubDirty}
                      aria-busy={savingUb ? "true" : "false"}
                      type="button"
                    >
                      {savingUb ? "Guardando..." : "Guardar"}
                    </button>
                  </div>
                  <div className="text-xs text-gray-500">
                    Al confirmar un cambio entre ubicaciones físicas, se solicita el traslado de stock correspondiente en Bejerman.
                  </div>
                  {ubNotice && (
                    <div
                      className={`mt-2 rounded border p-2 text-xs ${
                        ubNotice.tone === "ok"
                          ? "border-emerald-300 bg-emerald-50 text-emerald-900"
                          : ubNotice.tone === "warn"
                            ? "border-amber-300 bg-amber-50 text-amber-900"
                            : "border-red-300 bg-red-50 text-red-900"
                      }`}
                    >
                      {ubNotice.text}
                    </div>
                  )}
                </>
              ) : (
                <div>{data?.ubicacion_nombre || "-"}</div>
              )}
            </div>
          </div>
          {/* Comentarios (debajo de Asignación y Ubicación) */}
          <div className="mt-4">
            <h3 className="font-medium mb-2">Comentarios</h3>
            {editBasics ? (
              <textarea
                className="border rounded p-2 w-full min-h-[160px]"
                value={formBasics?.comentarios ?? ""}
                onChange={(e) => setFormBasics((s) => ({ ...(s || {}), comentarios: e.target.value }))}
              />
            ) : (
              <div className="whitespace-pre-wrap">{data.comentarios || "-"}</div>
            )}
          </div>
          
        </div>
      </div>

      {/* Salida del equipo */}
      {release && (
        <div className="mt-4 rounded border border-slate-200 bg-slate-50 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h2 className="font-semibold">Salida del equipo</h2>
              <div className="mt-1 text-sm text-slate-700">{releaseFlow.statusText}</div>
            </div>
            <button
              className="btn inline-flex w-full items-center justify-center gap-2 sm:w-auto"
              onClick={() => onOpenReleaseModal?.(data)}
              type="button"
              title={releaseFlow.statusText}
            >
              <Printer className="h-4 w-4" aria-hidden="true" />
              {releaseFlow.primaryLabel}
            </button>
          </div>
          <div className="mt-3 grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
            <div>
              <div className="text-[11px] font-semibold uppercase text-gray-500">Estado</div>
              <div className="mt-1"><StatusChip value={data?.estado} /></div>
            </div>
            <div>
              <div className="text-[11px] font-semibold uppercase text-gray-500">Presupuesto</div>
              <div className="mt-1"><StatusChip value={data?.presupuesto_estado} /></div>
            </div>
            <div>
              <div className="text-[11px] font-semibold uppercase text-gray-500">Resolución</div>
              <div className="mt-1 text-gray-900">{data?.resolucion ? resolutionLabel(data.resolucion) : "Sin definir"}</div>
            </div>
          </div>
        </div>
      )}

      {/* Entrega + Alquiler */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
        <div className="border rounded p-4">
        <h2 className="font-semibold mb-2">Entrega</h2>
        {data?.derivacion_activa && (
          <div className="mb-3 rounded border border-cyan-200 bg-cyan-50 p-2 text-sm text-cyan-900">
            El equipo sigue derivado. Marcá la derivación como devuelta antes de entregarlo.
          </div>
        )}
        {puedeCerrarPorVenta && !isVentaPendienteEntrega && (
          <label className="mb-3 flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={ventaSalida}
              onChange={(e) => setVentaSalida(e.target.checked)}
            />
            <span>
              <span className="font-medium">El equipo salió vendido (RT, o RTN si fue un aporte)</span>
              <span className="block text-xs text-gray-600">
                La OS cierra como “Vendido entregado”. El RT o el RTN ya movieron el stock en
                Bejerman, así que NEXORA no genera ningún movimiento de depósito.
              </span>
            </span>
          </label>
        )}
        {mostrarFormEntrega ? (
          <>
            <div className="grid grid-cols-1 gap-3">
                <Row label={registrandoVenta ? "Remito de venta RT o RTN (requerido)" : "Remito salida (requerido)"}>{

                <div>
                  <input
                    className="border rounded p-2 w-full"
                    value={entrega.remito_salida}
                    onChange={(e) => setEntrega({ ...entrega, remito_salida: e.target.value })}
                  />
                  <BuscadorRemitoBejerman
                    flow={registrandoVenta ? "sale" : "service_release"}
                    ingresoId={id}
                    serial={equipoIdentificador}
                    onPick={(numero) => setEntrega((current) => ({ ...current, remito_salida: numero }))}
                    resumenCambios={(candidato) => {
                      const actual = String(entrega?.remito_salida || "").trim();
                      const nuevo = String(candidato?.remitoNumber || "").trim();
                      return actual === nuevo ? [] : [{ campo: "Remito salida", actual, nuevo }];
                    }}
                    confirmLabel="Usar este remito"
                    advertencia={
                      registrandoVenta
                        ? "Se completa el campo; la venta se registra con el botón de abajo."
                        : "Se completa el campo; la entrega se registra con el botón de abajo."
                    }
                  />
                </div>
                }
                </Row>
              <Row label="Factura (opcional)">{
                <input
                  className="border rounded p-2 w-full"
                  value={entrega.factura_numero}
                  onChange={(e) => setEntrega({ ...entrega, factura_numero: e.target.value })}
                />
              }</Row>

              <Row label="Fecha entrega">{
                <input
                  type="date"
                  className="border rounded p-2 w-full"
                  value={entrega.fecha_entrega}
                  onChange={(e) => setEntrega({ ...entrega, fecha_entrega: e.target.value })}
                  max={maxDateOnly}
                />
              }</Row>
              {/* Sólo cuando hay un equipo que entregar: en una parte de pago sin permuta
                  no sale nada, y pedir una serie trababa la entrega sin nada que verificar. */}
              {String(data?.resolucion || "") === "cambio" && swapEsperado && (
                <div>
                  <label className="text-sm">
                    Verificar serie (Cambio)
                    {swapEsperado ? (
                      <span className="ml-1 font-medium">- se entrega {swapEsperado}</span>
                    ) : null}
                  </label>
                  <input
                    className="border rounded p-2 w-full"
                    value={entrega.serial_confirm || ""}
                    onChange={(e) => setEntrega({ ...entrega, serial_confirm: e.target.value })}
                    placeholder="Ingrese la serie del equipo que se entrega"
                  />
                </div>
              )}
            </div>
            <div className="mt-3">
              <button
                className="bg-green-600 text-white px-4 py-2 rounded disabled:opacity-60"
                disabled={Boolean(data?.derivacion_activa || savingEntrega)}
                onClick={async () => {
                  try {
                    if (!entrega.remito_salida.trim()) {
                      setErr("El remito es requerido para entregar.");
                      return;
                    }
                    // Una venta sale con RT, o con RTN si fue un aporte (los RTN no se facturan).
                    // El backend exige lo mismo; se avisa acá antes de mandar un RSS como venta.
                    if (registrandoVenta && !/^\s*RTN?\b/i.test(entrega.remito_salida)) {
                      setErr("Para cerrar por venta el remito tiene que ser un RT o un RTN.");
                      return;
                    }
                    if (String(data?.resolucion || "") === "cambio" && swapEsperado) {
                      const confirmado = String(entrega?.serial_confirm || "").trim();
                      if (!confirmado) {
                        setErr("Debe verificar la Serie (Cambio) antes de entregar.");
                        return;
                      }
                      // Se avisa acá para no mandar al mostrador a leer un 409 del backend.
                      if (swapEsperado && !swapConfirmMatches(confirmado, swapEsperado)) {
                        setErr(`La serie no coincide con el equipo del cambio (${swapEsperado}).`);
                        return;
                      }
                    }
                    // La marca de venta viaja explícita: un RTN solo también es una entrega en alquiler.
                    await postEntregarIngreso(id, registrandoVenta ? { ...entrega, venta: true } : entrega);
                    await refreshIngreso();
                                setMailEnviado(true);
                  } catch (e) {
                    setErr(e?.message || "No se pudo marcar como entregado");
                  }
                }}
              >
                {isVentaPendienteEntrega || registrandoVenta ? "Marcar venta entregada" : "Marcar ENTREGADO"}
              </button>
            </div>
          </>
        ) : (
          <>
            {!editEntrega && (
              <div className="grid grid-cols-1 gap-3 text-sm">
                <div>
                  <div className="text-gray-600">Remito salida</div>
                  <div className="font-medium">{data.remito_salida || "-"}</div>
                </div>
                <div>
                  <div className="text-gray-600">Factura</div>
                  <div className="font-medium">{data.factura_numero || "-"}</div>
                </div>
                <div>
                  <div className="text-gray-600">Fecha entrega</div>
                  <div className="font-medium">{data.fecha_entrega ? formatDateTimeHelper(data.fecha_entrega) : "-"}</div>
                </div>
              </div>
            )}
            {canEditEntrega && !data?.derivacion_activa && !editEntrega && (
              <div className="mt-3">
                <button className="px-3 py-2 border rounded" type="button" onClick={() => setEditEntrega(true)}>
                  Editar entrega
                </button>
              </div>
            )}
            {canEditEntrega && editEntrega && (
              <>
                <div className="grid grid-cols-1 gap-3">
                  <div>
                    <label className="text-sm">Remito salida</label>
                    <input
                      className="border rounded p-2 w-full"
                      value={entrega.remito_salida}
                      onChange={(e) => setEntrega({ ...entrega, remito_salida: e.target.value })}
                    />
                  </div>
                  <div>
                    <label className="text-sm">Factura</label>
                    <input
                      className="border rounded p-2 w-full"
                      value={entrega.factura_numero}
                      onChange={(e) => setEntrega({ ...entrega, factura_numero: e.target.value })}
                    />
                  </div>
                  <div>
                    <label className="text-sm">Fecha entrega</label>
                    <input
                      type="date"
                      className="border rounded p-2 w-full"
                      value={entrega.fecha_entrega}
                      onChange={(e) => setEntrega({ ...entrega, fecha_entrega: e.target.value })}
                      max={maxDateOnly}
                    />
                  </div>
                </div>
                <div className="mt-3 flex gap-2">
                  <button
                    className="bg-blue-600 text-white px-4 py-2 rounded disabled:opacity-60"
                    disabled={savingEntrega || data?.derivacion_activa}
                    type="button"
                    onClick={async () => {
                      try {
                        setSavingEntrega(true);
                        const payload = {
                          remito_salida: (entrega.remito_salida || "").trim(),
                          factura_numero: (entrega.factura_numero || "").trim(),
                          fecha_entrega: entrega.fecha_entrega || null,
                        };
                        await patchIngreso(id, payload);
                        await refreshIngreso();
                                setMailEnviado(true);
                        setEditEntrega(false);
                        setErr("");
                      } catch (e) {
                        setErr(e?.message || "No se pudo guardar entrega");
                      } finally {
                        setSavingEntrega(false);
                      }
                    }}
                  >
                    Guardar
                  </button>
                  <button
                    className="px-3 py-2 border rounded"
                    type="button"
                    onClick={() => {
                      setEditEntrega(false);
                      setEntrega({
                        remito_salida: data?.remito_salida || "",
                        factura_numero: data?.factura_numero || "",
                        fecha_entrega: toDateInputStr(data?.fecha_entrega),
                      });
                    }}
                  >
                    Cancelar
                  </button>
                </div>
              </>
            )}
          </>
        )}
        </div>

      {/* Alquiler */}
      <div className="border rounded p-4">
        <h2 className="font-semibold mb-2">Alquiler</h2>
        {alquilerBloqueadoPorEntrega && (
          <div className="text-xs text-amber-700 mb-2">
            Equipo entregado con RTA: el alquiler y su remito quedan sólo para consulta.
          </div>
        )}
        <Row label="¿Se alquiló?">
          <input
            type="checkbox"
            checked={!!data.alquilado}
            disabled={!alquilerEditable || !alquilerPuedeDestildarse}
            onChange={async (e) => {
              if (!alquilerEditable) return;
              const checked = e.target.checked;
              if (!checked && data?.alquilado && !alquilerPuedeDestildarse) return;
              try {
                if (checked) {
                  const target = (ubicaciones || []).find((u) => (u?.nombre || "").trim().toLowerCase() === "alquilado");
                  if (target && target.id != null) {
                    setUbicacionId(String(target.id));
                    await patch({ alquilado: true, ubicacion_id: Number(target.id) });
                    return;
                  }
                }
                await patch({ alquilado: checked });
              } catch (err) {
                setErr && setErr(err?.message || "No se pudo actualizar el estado de alquiler");
              }
            }}
          />
        </Row>
        <Row label="¿A quién?">
          <div>
            <input
              className="border rounded p-1 w-80"
              list={clientesPerm ? "alquiler_clientes_rs" : undefined}
              value={alquilerAInput}
              onChange={(e) => { if (alquilerEditable) setAlquilerAInput(e.target.value); }}
              onBlur={() => commitAlquilerText("alquiler_a", alquilerAInput)}
              onKeyDown={blurOnEnter}
              disabled={!alquilerEditable}
              placeholder="Elija de la lista"
            />
            {clientesPerm && (
              <datalist id="alquiler_clientes_rs">
                {(clientes || []).map((c) => (
                  <option key={`alq-rs-${c.id}`} value={c.razon_social} label={[c.alias_interno, c.cod_empresa].filter(Boolean).join(" - ")} />
                ))}
                {(clientes || []).filter((c) => c.alias_interno).map((c) => (
                  <option key={`alq-alias-${c.id}`} value={c.alias_interno} label={c.razon_social} />
                ))}
              </datalist>
            )}
            {alquilerEditable && clientesPerm && (alquilerAInput || "").trim() && !alquilerMatch && (
              <div className="text-xs text-amber-700 mt-1">Seleccione un cliente válido de la lista.</div>
            )}
          </div>
        </Row>
        <Row label="Remito">
          <div className="w-full">
            <input
              className="border rounded p-1 w-60"
              value={alquilerRemitoInput}
              onChange={(e) => { if (alquilerEditable) setAlquilerRemitoInput(e.target.value); }}
              onBlur={() => commitAlquilerText("alquiler_remito", alquilerRemitoInput)}
              onKeyDown={blurOnEnter}
              disabled={!alquilerEditable}
            />
            {alquilerBloqueadoPorEntrega ? (
              <button
                type="button"
                className="mt-1 rounded border border-sky-300 px-2 py-1 text-xs text-sky-900 hover:bg-sky-50 disabled:opacity-60"
                onClick={verRta}
                disabled={rentalPdfLoading}
              >
                {rentalPdfLoading ? "Abriendo PDF..." : "Ver RTA"}
              </button>
            ) : alquilerEditable && (
              <BuscadorRemitoBejerman
                flow="rental"
                ingresoId={id}
                serial={equipoIdentificador}
                onPick={aplicarRemitoAlquilerEncontrado}
                resumenCambios={resumenAlquilerDesdeRemito}
                advertencia="Al confirmar, el equipo queda asociado a este remito de alquiler."
              />
            )}
          </div>
        </Row>
        <Row label="Fecha">
          <input
            type="date"
            className="border rounded p-1"
            value={(data.alquiler_fecha || "").slice(0, 10)}
            onChange={(e) => { if (alquilerEditable) patch({ alquiler_fecha: e.target.value || null }); }}
            disabled={!alquilerEditable}
          />
        </Row>
        {data.alquilado && (
          <div className="mt-3 border-t pt-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">Estado en Bejerman</div>
              <button
                type="button"
                className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-60"
                onClick={cargarEstadoAlquilerRemito}
                disabled={rentalRemitoLoading}
              >
                {rentalRemitoLoading ? "Consultando..." : "Actualizar"}
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className={`rounded-full px-2 py-0.5 font-semibold ${rentalRemitoStatusClass}`}>
                {rentalRemitoLoading && !rentalRemitoState ? "Consultando Bejerman" : rentalRemitoStatusLabel}
              </span>
              {rentalRemitoState?.document?.number && (
                <span className="text-gray-600">
                  Remito: <span className="font-medium text-gray-900">{rentalRemitoState.remitoNumber}</span>
                </span>
              )}
              {rentalRemitoState?.stock && (
                <span className="text-gray-600">
                  STL: <span className="font-medium text-gray-900">{rentalRemitoState.stock.stlQuantity ?? 0}</span>
                </span>
              )}
            </div>
            {rentalRemitoState?.message && !rentalRemitoError && (
              <div className="mt-1 text-xs text-gray-700">{rentalRemitoState.message}</div>
            )}
            {rentalRemitoState?.blockedReason && !rentalRemitoError && (
              <div className="mt-1 text-xs text-amber-700">{rentalRemitoState.blockedReason}</div>
            )}
            {rentalRemitoState?.customerMismatch && (
              <div className="mt-1 text-xs text-amber-700">
                El cliente en Bejerman ({rentalRemitoState.remitoCustomer?.name || "?"}) no coincide con "{rentalRemitoState.customer?.name || alquilerAInput}".
              </div>
            )}
            {rentalRemitoError && <div className="mt-1 text-xs text-red-700">{rentalRemitoError}</div>}
            {canForceBejermanFix && rentalRemitoState?.action && (
              <button
                type="button"
                className="mt-2 rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-60"
                onClick={forzarAccionAlquilerRemito}
                disabled={rentalActionBusy}
              >
                {rentalActionBusy ? "Ejecutando..." : rentalRemitoState.action.label}
              </button>
            )}
          </div>
        )}
        {data.alquilado && (
          <div className="mt-3 border-t pt-3">
            <div className="text-xs uppercase text-gray-500 mb-1">Accesorios de alquiler</div>
            {Array.isArray(data.alquiler_accesorios_items) && data.alquiler_accesorios_items.length > 0 ? (
              <ul className="list-disc list-inside text-sm">
                {data.alquiler_accesorios_items.map((it) => (
                  <li key={it.id} className="flex items-center justify-between gap-2">
                    <span>
                      {it.accesorio_nombre}
                      {it.referencia ? ` (ref: ${it.referencia})` : ""}
                    </span>
                    {alquilerEditable && !isEntregadoOBaja && (
                      <button
                        className="text-red-600 text-xs"
                        onClick={() => removeAccesorioAlquiler(it.id)}
                        disabled={deletingAccAlqId === it.id}
                        type="button"
                      >
                        {deletingAccAlqId === it.id ? "Quitando..." : "Quitar"}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-sm text-gray-500">Sin accesorios de alquiler.</div>
            )}
            {alquilerEditable && !isEntregadoOBaja && (
              <div className="mt-2 flex flex-wrap items-end gap-2">
                <input
                  className="border rounded p-2 min-w-[240px]"
                  list="accesorios_catalogo"
                  placeholder="Descripción (elija de la lista)"
                  value={nuevoAccAlq.descripcion}
                  onChange={(e) => setNuevoAccAlq((s) => ({ ...s, descripcion: e.target.value }))}
                />
                <datalist id="accesorios_catalogo">
                  {accesCatalogo.map((a) => (
                    <option key={a.id} value={a.nombre} />
                  ))}
                </datalist>
                <input
                  className="border rounded p-2 w-40"
                  placeholder="Nro de referencia (opcional)"
                  value={nuevoAccAlq.referencia}
                  onChange={(e) => setNuevoAccAlq((s) => ({ ...s, referencia: e.target.value }))}
                />
                <button
                  className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60"
                  onClick={addAccesorioAlquiler}
                  disabled={addingAccAlq || !(nuevoAccAlq.descripcion || "").trim()}
                  type="button"
                >
                  {addingAccAlq ? "Agregando..." : "Agregar"}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
      </div>
      {ubConfirmOpen && (
        <ResponsiveModalOverlay align="center" onClick={() => (savingUb ? null : setUbConfirmOpen(false))}>
          <ResponsiveModalPanel className="max-w-lg" onClick={(event) => event.stopPropagation()}>
            <div className="border-b px-4 py-3">
              <h2 className="text-base font-semibold text-gray-900">Confirmar cambio de ubicación</h2>
              <p className="text-xs text-gray-600">
                {locationDisplayName(ubicacionOrigenNombre) || "Sin ubicación"} → {locationDisplayName(ubicacionDestinoNombre)}
              </p>
            </div>
            <div className="space-y-3 px-4 py-3 text-sm text-gray-700">
              {cambiaDepositoBejerman ? (
                <>
                  <p>
                    Además de actualizar NEXORA, se encolará una transferencia de stock en Bejerman de{" "}
                    <span className="font-semibold">{depositoOrigenBejerman}</span> a{" "}
                    <span className="font-semibold">{depositoDestinoBejerman}</span>.
                  </p>
                  <p className="text-xs text-gray-600">
                    El movimiento se emite y verifica por la cola de sincronización. Si Bejerman no encuentra stock en el
                    depósito de origen, queda bloqueado sin crear stock en el destino.
                  </p>
                </>
              ) : (
                <div className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
                  Esta combinación no tiene dos depósitos físicos distintos asociados. Se actualizará sólo la ubicación de
                  NEXORA; no se emitirá un movimiento en Bejerman.
                </div>
              )}
              {ubicacionDestinoEsEstanteria && (
                <div className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
                  {isMgOwned(data)
                    ? "La estantería de alquiler requiere tanto esta ubicación como stock en STL."
                    : "Si el equipo no pertenece al parque de alquiler, NEXORA guarda la ubicación pero no solicita el pase a STL."}
                </div>
              )}
              {isSepidLocationName(ubicacionDestinoNombre) && (
                <div className="rounded border border-sky-300 bg-sky-50 p-2 text-xs text-sky-900">
                  Sepid (VAL) es el depósito de los equipos propios disponibles para venta, incluidos los tomados como parte
                  de pago que no llevan número MG.
                </div>
              )}
            </div>
            <div className="flex justify-end gap-2 border-t px-4 py-3">
              <button
                type="button"
                className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                onClick={() => setUbConfirmOpen(false)}
                disabled={savingUb}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="rounded bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
                onClick={guardarUbicacion}
                disabled={savingUb}
              >
                {savingUb
                  ? "Guardando..."
                  : cambiaDepositoBejerman
                    ? "Confirmar y solicitar traslado"
                    : "Confirmar ubicación"}
              </button>
            </div>
          </ResponsiveModalPanel>
        </ResponsiveModalOverlay>
      )}
    </>
  );
}
