import Row from "../../../components/Row";
import StatusChip from "../../../components/StatusChip";
import { useMemo, useState, useEffect, useCallback } from "react";
import {
  formatDateOnly as formatDateOnlyHelper,
  formatDateTime as formatDateTimeHelper,
  hasMgCode,
  isMgOwned,
  isSaleTicketState,
  propiedadEquipoLabelOf,
  resolveFechaIngreso,
} from "../../../lib/ui-helpers";
import { resolutionLabel } from "../../../lib/constants";
import { isJefe } from "../../../lib/authz";
import { documentNameFromRis, isRisRegistered, risRemitoFrom } from "../../../lib/ris-print";
import { getReleaseFlow } from "../../../lib/release-flow";
import { Printer } from "lucide-react";
import {
  postEntregarIngreso,
  patchIngreso,
  checkGarantiaFabrica,
  patchIngresoTecnico,
  postSolicitarAsignacion,
  getAccesoriosCatalogo,
  postAccesorioAlquilerIngreso,
  deleteAccesorioAlquilerIngreso,
} from "../../../lib/api";

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
    canManageReleaseResolution,
    canShowRisAction,
    ingressDocumentName: ingressDocumentNameProp,
    risBusy,
    onEmitRis,
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
  const hasRisRemito = Boolean(risRemito);
  const ingressDocumentName = documentNameFromRis(data, ingressDocumentNameProp || "RIS");
  const releaseFlow = useMemo(
    () => getReleaseFlow(data, { canManageResolution: canManageReleaseResolution }),
    [data, canManageReleaseResolution],
  );

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
  const propiedadClass = isOwnedByMgCustomer
    ? "bg-emerald-100 text-emerald-800"
    : mgHistorico
      ? "bg-amber-100 text-amber-800"
    : "bg-gray-100 text-gray-700";
  const mgHistoricoHelp = "Equipo vendido: este MG queda solo como trazabilidad y ya no indica stock propio.";
  const isEntregadoOBaja = ["entregado", "baja"].includes(estadoLower) || isSaleTicketState(estadoLower);
  const alquilerEditable = Boolean(canEditAlquiler && !mgVendido);
  const alquilerBloqueadoPorEntrega = Boolean(
    data?.alquilado
    && String(data?.alquiler_remito || "").trim()
    && (data?.fecha_entrega || estadoLower === "entregado")
  );
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
    if (estadoLower === "derivado") return "Derivación externa";
    if (["reparar", "controlado_sin_defecto", "reparado", "liberado"].includes(estadoLower)) return "Reparación / Salida";
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
    { label: "Diagnóstico", value: fechasHitos.fecha_diagnosticado },
    { label: "Reparación", value: fechasHitos.fecha_reparado },
    { label: "Liberación", value: fechasHitos.fecha_liberacion },
    { label: "Servicio", value: firstDateValue(fechasHitos.fecha_servicio, data?.fecha_servicio) },
    { label: "Entrega", value: firstDateValue(fechasHitos.fecha_entrega, data?.fecha_entrega) },
    ...(data?.alquilado || fechaAlquiler ? [{ label: "Alquiler", value: fechaAlquiler }] : []),
    ...(data?.mg_inactivo_venta || fechaVentaMg ? [{ label: "Venta MG", value: fechaVentaMg }] : []),
    ...(data?.baja_solicitada_id || fechaBajaSolicitada ? [{ label: "Solicitud baja", value: fechaBajaSolicitada }] : []),
  ];
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

  async function saveUbicacion() {
    if (!canEditLocation || !ubDirty) return;
    try {
      setSavingUb(true);
      await patch({ ubicacion_id: _selUb });
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo actualizar la ubicación.");
    } finally {
      setSavingUb(false);
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
        if (typeof r?.within_365_days !== "boolean") return;
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
                    value={clienteRsInput}
                    onChange={(e) => {
                      const v = e.target.value;
                      const selected = (clientes || []).find((c) => normClient(c?.razon_social) === normClient(v) || normClient(c?.alias_interno) === normClient(v));
                      const nextRs = selected?.razon_social || v;
                      setClienteRsInput(nextRs);
                      const nextCod = selected?.cod_empresa || "";
                      setClienteCodInput(nextCod);
                      syncClienteFromInputs(nextRs, nextCod);
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
            <Row label={"N° de remito"}>
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
                        ? `Remito registrado: ${risRemito || data?.ris?.status || "pendiente"}`
                        : `${ingressDocumentName}: ${data?.ris?.remito_number || data?.ris?.status || "pendiente"}`}
                    </span>
                  )}
                  {!isRegisteredRis && canShowRisAction && typeof onEmitRis === "function" && (
                    <button
                      type="button"
                      onClick={onEmitRis}
                      disabled={risBusy}
                      className="mt-1 inline-flex items-center gap-1 rounded border border-sky-200 px-2 py-1 text-xs font-medium text-sky-700 hover:bg-sky-50 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <Printer className="h-3.5 w-3.5" aria-hidden="true" />
                      {risBusy ? `Preparando ${ingressDocumentName}...` : hasRisRemito ? `Ver ${ingressDocumentName}` : `Emitir ${ingressDocumentName}`}
                    </button>
                  )}
                </span>
              )}
            </Row>
            <Row label={"Faja de garantía"}>
              <span>{data?.etiq_garantia_ok ? "OK" : "Abiertas"}</span>
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
                      {ubicaciones.map((u) => (
                        <option key={u.id} value={String(u.id)}>
                          {u.nombre}
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
                  <div className="text-xs text-gray-500">La ubicación puede modificarse desde aquí.</div>
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
        {data.estado === "liberado" || isVentaPendienteEntrega ? (
          <>
            <div className="grid grid-cols-1 gap-3">
                <Row label="Remito salida (requerido)">{

                <input
                  className="border rounded p-2 w-full"
                  value={entrega.remito_salida}
                  onChange={(e) => setEntrega({ ...entrega, remito_salida: e.target.value })}
                />
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
              {String(data?.resolucion || "") === "cambio" && (
                <div>
                  <label className="text-sm">Verificar serie (Cambio)</label>
                  <input
                    className="border rounded p-2 w-full"
                    value={entrega.serial_confirm || ""}
                    onChange={(e) => setEntrega({ ...entrega, serial_confirm: e.target.value })}
                    placeholder="Ingrese la serie nueva para confirmar"
                  />
                </div>
              )}
            </div>
            <div className="mt-3">
              <button
                className="bg-green-600 text-white px-4 py-2 rounded"
                onClick={async () => {
                  try {
                    if (!entrega.remito_salida.trim()) {
                      setErr("El remito es requerido para entregar.");
                      return;
                    }
                    if (String(data?.resolucion || "") === "cambio") {
                      if (!String(entrega?.serial_confirm || "").trim()) {
                        setErr("Debe verificar la Serie (Cambio) antes de entregar.");
                        return;
                      }
                    }
                    await postEntregarIngreso(id, entrega);
                    await refreshIngreso();
                                setMailEnviado(true);
                  } catch (e) {
                    setErr(e?.message || "No se pudo marcar como entregado");
                  }
                }}
              >
                {isVentaPendienteEntrega ? "Marcar venta entregada" : "Marcar ENTREGADO"}
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
            {canEditEntrega && !editEntrega && (
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
                    disabled={savingEntrega}
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
            No se puede destildar: el equipo ya fue entregado con remito de alquiler.
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
          <input
            className="border rounded p-1 w-60"
            value={alquilerRemitoInput}
            onChange={(e) => { if (alquilerEditable) setAlquilerRemitoInput(e.target.value); }}
            onBlur={() => commitAlquilerText("alquiler_remito", alquilerRemitoInput)}
            onKeyDown={blurOnEnter}
            disabled={!alquilerEditable}
          />
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
    </>
  );
}
