import Row from "../../../components/Row";
import StatusChip from "../../../components/StatusChip";
import { useMemo, useState, useEffect, useCallback } from "react";
import { formatDateTime as formatDateTimeHelper, resolveFechaIngreso } from "../../../lib/ui-helpers";
import { resolutionLabel } from "../../../lib/constants";
import { isJefe } from "../../../lib/authz";
import {
  getBlob,
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
    toDatetimeLocalStr,
  } = props;

  const canUncheckAlquilado = isJefe(user);

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
  const [assignedNameHint, setAssignedNameHint] = useState(null);
  const techDirty = Boolean(
    canAssignTecnico && Number(selTecnicoId ?? -1) !== Number(data?.asignado_a ?? -1)
  );
  useEffect(() => {
    console.log('[PrincipalTab] data asignado_a changed', { asignado_a: data?.asignado_a, asignado_a_nombre: data?.asignado_a_nombre });
  }, [data?.asignado_a, data?.asignado_a_nombre]);
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
  const isEntregadoOBaja = ["entregado", "baja"].includes(estadoLower);
  const alquilerEditable = Boolean(canEditAlquiler);
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

  // Cliente: validacion contra catalogo (igual que en NuevoIngreso)
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
  const hasClienteCodCatalog = useMemo(
    () => (clientes || []).some((c) => String(c?.cod_empresa || "").trim() !== ""),
    [clientes],
  );
  const rsMatch = useMemo(() => {
    if (!clienteRsInput) return null;
    const needle = normClient(clienteRsInput);
    if (!needle) return null;
    return (clientes || []).find((c) => normClient(c?.razon_social) === needle) || null;
  }, [clienteRsInput, clientes, normClient]);
  const codMatch = useMemo(() => {
    if (!clienteCodInput || !hasClienteCodCatalog) return null;
    const needle = normClient(clienteCodInput);
    if (!needle) return null;
    return (clientes || []).find((c) => normClient(c?.cod_empresa) === needle) || null;
  }, [clienteCodInput, clientes, hasClienteCodCatalog, normClient]);
  const clienteMismatch = useMemo(() => {
    if (!hasClienteCodCatalog) return false;
    return !!(rsMatch && codMatch && rsMatch.id !== codMatch.id);
  }, [rsMatch, codMatch, hasClienteCodCatalog]);
  const alquilerMatch = useMemo(() => {
    const val = normClient(data?.alquiler_a);
    if (!val) return null;
    return (clientes || []).find((c) => normClient(c?.razon_social) === val) || null;
  }, [data?.alquiler_a, clientes, normClient]);

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
      if (!d) { setErr && setErr("Escribí una descripción"); return; }
      const acc = (accesCatalogo || []).find(a => (a?.nombre || "").trim().toLowerCase() === d);
      if (!acc) { setErr && setErr("Elegí una descripción válida de la lista"); return; }
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
        const enGarantia = !!r.within_365_days;
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
                      setClienteRsInput(v);
                      const c = syncClienteFromInputs(v, clienteCodInput);
                      if (
                        hasClienteCodCatalog
                        && c
                        && normClient(clienteCodInput) !== normClient(c.cod_empresa)
                      ) {
                        setClienteCodInput(c.cod_empresa || "");
                      }
                    }}
                    placeholder="Elegí de la lista"
                  />
                  {clientesPerm && (
                    <datalist id="service_clientes_rs">
                      {(clientes || []).map((c) => (
                        <option key={c.id} value={c.razon_social} />
                      ))}
                    </datalist>
                  )}
                  {clientesPerm && clienteRsInput && !rsMatch && (
                    <div className="text-xs text-amber-700 mt-1">Selecciona una razón social válida de la lista.</div>
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
                    className="border rounded p-1 w-40"
                    list={clientesPerm && hasClienteCodCatalog ? "service_clientes_cod" : undefined}
                    value={clienteCodInput}
                    onChange={(e) => {
                      const v = e.target.value;
                      setClienteCodInput(v);
                      const c = syncClienteFromInputs(clienteRsInput, v);
                      if (c && normClient(clienteRsInput) !== normClient(c.razon_social)) {
                        setClienteRsInput(c.razon_social || "");
                      }
                    }}
                    placeholder="Elegí de la lista"
                  />
                  {clientesPerm && hasClienteCodCatalog && (
                    <datalist id="service_clientes_cod">
                      {(clientes || []).map((c) => (
                        <option key={c.id} value={c.cod_empresa} />
                      ))}
                    </datalist>
                  )}
                  {clientesPerm && hasClienteCodCatalog && clienteCodInput && !codMatch && (
                    <div className="text-xs text-amber-700 mt-1">Selecciona un código válido de la lista.</div>
                  )}
                  {clientesPerm && hasClienteCodCatalog && clienteMismatch && (
                    <div className="text-xs text-amber-700 mt-1">La razón social y el código no coinciden.</div>
                  )}
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

          <h2 className="font-semibold mt-4 mb-2">Equipo</h2>
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
                <input
                  className="border rounded p-1 w-60"
                  value={formBasics?.numero_interno || ""}
                  onChange={(e) => setFormBasics((s) => ({ ...s, numero_interno: e.target.value }))}
                />
              ) : (
                <span>{data.numero_interno || ""}</span>
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
                <span>{data.remito_ingreso || "-"}</span>
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
            <Row label="Fecha ingreso">{formatDateTimeHelper(resolveFechaIngreso(data))}</Row>
            <Row label="Fecha servicio">{data.fecha_servicio ? formatDateTimeHelper(data.fecha_servicio) : "-"}</Row>
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
                        <div className="text-xs text-amber-700">Solicitud registrada; no se pudo enviar el correo</div>
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
                    <div>No tenés permiso para reasignar técnicos.</div>
                    {isTech && !isEntregadoOBaja && Number(userId || 0) > 0 && (
                      <div className="mt-2">
                        {data?.asignado_a === userId ? (
                          <div className="text-xs text-gray-600">Ya estás asignado a este ingreso.</div>
                        ) : data?.tecnico_solicitado_id === userId ? (
                          <div className="text-xs text-amber-700">Solicitud de asignación enviada</div>
                        ) : data?.tecnico_solicitado_id ? (
                          <div className="text-xs text-gray-600">{otherTechLabel}</div>
                        ) : (
                          <button hidden={mailEnviado}
                            className="bg-neutral-800 text-white px-3 py-2 rounded disabled:opacity-60"
                            disabled={solicitando}
                            onClick={async () => {
                              try {
                                setSolicitando(true);
                                setMailEnviado(false);
                                setMailFallo(false);
                                const r = await postSolicitarAsignacion(id);
                                await refreshIngreso();
                                setMailEnviado(!!(r && r.email_sent));
                                if (r && r.ok && r.email_sent === false) {
                                  setMailFallo(true);
                                }
                                setEmailDebug(r && r.email_debug ? r.email_debug : null);
                                setErr("");
                              } catch (e) {
                                setErr(e?.message || "No se pudo solicitar la asignación");
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

      {/* Botón de orden de salida (liberar) */}
      {release && (Boolean(data?.resolucion) || data?.estado === "liberado") && (
        <button
          className="bg-neutral-800 text-white px-3 py-2 rounded mt-4"
          onClick={async () => {
            try {
              const blob = await getBlob(`/api/ingresos/${id}/remito/`);
              if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");
              const url = URL.createObjectURL(blob);
              window.open(url, "_blank", "noopener");
              setTimeout(() => URL.revokeObjectURL(url), 60_000);
              await refreshIngreso();
                                setMailEnviado(true);
            } catch (e) {
              setErr(e?.message || "No se pudo imprimir la orden de salida");
            }
          }}
          type="button"
        >
          Imprimir orden de salida (liberar)
        </button>
      )}

      {/* Entrega + Alquiler */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
        <div className="border rounded p-4">
        <h2 className="font-semibold mb-2">Entrega</h2>
        {data.estado === "liberado" ? (
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
                  type="datetime-local"
                  className="border rounded p-2 w-full"
                  value={entrega.fecha_entrega}
                  onChange={(e) => setEntrega({ ...entrega, fecha_entrega: e.target.value })}
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
                Marcar ENTREGADO
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
                      type="datetime-local"
                      className="border rounded p-2 w-full"
                      value={entrega.fecha_entrega}
                      onChange={(e) => setEntrega({ ...entrega, fecha_entrega: e.target.value })}
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
                        fecha_entrega: toDatetimeLocalStr(data?.fecha_entrega),
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
        <Row label="¿Se alquiló?">
          <input
            type="checkbox"
            checked={!!data.alquilado}
            disabled={!alquilerEditable || (!!data.alquilado && !canUncheckAlquilado)}
            onChange={async (e) => {
              if (!alquilerEditable) return;
              const checked = e.target.checked;
              if (!checked && data?.alquilado && !canUncheckAlquilado) return;
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
              value={data.alquiler_a || ""}
              onChange={(e) => { if (alquilerEditable) patch({ alquiler_a: e.target.value }); }}
              disabled={!alquilerEditable}
              placeholder="Elegí de la lista"
            />
            {clientesPerm && (
              <datalist id="alquiler_clientes_rs">
                {(clientes || []).map((c) => (
                  <option key={c.id} value={c.razon_social} />
                ))}
              </datalist>
            )}
            {alquilerEditable && clientesPerm && (data.alquiler_a || "").trim() && !alquilerMatch && (
              <div className="text-xs text-amber-700 mt-1">Selecciona un cliente válido de la lista.</div>
            )}
          </div>
        </Row>
        <Row label="Remito">
          <input className="border rounded p-1 w-60" value={data.alquiler_remito || ""} onChange={(e) => { if (alquilerEditable) patch({ alquiler_remito: e.target.value }); }} disabled={!alquilerEditable} />
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
                  placeholder="Descripción (elegí de la lista)"
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
