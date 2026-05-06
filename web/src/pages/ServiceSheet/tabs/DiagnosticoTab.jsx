import Row from "../../../components/Row";
import IngresoPhotos from "../../../components/IngresoPhotos";
import { RESOLUCION_OPTIONS, RESOLUCION, ESTADO } from "../../../lib/constants";
import { getBlob, postMarcarReparado, postCerrarReparacion, postAccesorioIngreso, deleteAccesorioIngreso, postMarcarControladoSinDefecto, postMarcarParaReparar, postHabilitarReparacionCotizacion } from "../../../lib/api";
import { useCallback, useEffect, useState } from "react";

export default function DiagnosticoTab({
  id,
  data,
  // accesorios
  canEditAcc,
  accesCatalogo,
  nuevoAcc,
  setNuevoAcc,
  // diagnostico/trabajos
  descripcion,
  setDescripcion,
  trabajos,
  setTrabajos,
  // fecha servicio
  fechaServStr,
  setFechaServStr,
  maxLocalNow,
  // resolucion
  canResolve,
  resolucion,
  setResolucion,
  canAutorizarReparar,
  isCotizacion,
  permiteReparacion,
  canHabilitarReparacionCotizacion,
  // permisos
  actAsTech,
  canEditDiag,
  canMarkReparado,
  // helpers
  patch,
  setErr,
  refreshIngreso,
  setToastMsg,
  setShowReparadoToast,
  savingDiag,
  // fotos
  canManagePhotos,
}) {
  const [addingAcc, setAddingAcc] = useState(false);
  const [deletingAccId, setDeletingAccId] = useState(null);
  const [savingAll, setSavingAll] = useState(false);
  const [savingResol, setSavingResol] = useState(false);
  const [marcandoReparar, setMarcandoReparar] = useState(false);
  const [habilitandoReparacion, setHabilitandoReparacion] = useState(false);
  const [serialCambio, setSerialCambio] = useState("");
  const [fajaGarantiaInput, setFajaGarantiaInput] = useState(data?.faja_garantia || "");
  const isEntregadoOBaja = ["entregado", "baja"].includes((data?.estado || "").toLowerCase());
  const estadoLower = (data?.estado || "").toLowerCase();
  const estadosBloqueadosDiag = new Set([
    ESTADO.REPARADO,
    ESTADO.LIBERADO,
    ESTADO.ENTREGADO,
    ESTADO.BAJA,
    ESTADO.ALQUILADO,
    ESTADO.CONTROLADO_SIN_DEFECTO,
  ].map((s) => String(s || "").toLowerCase()));
  const isEstadoBloqueadoDiag = estadosBloqueadosDiag.has(estadoLower);
  const estadosBloqueadosReparado = new Set([
    ESTADO.ENTREGADO,
    ESTADO.BAJA,
    ESTADO.ALQUILADO,
    ESTADO.CONTROLADO_SIN_DEFECTO,
  ].map((s) => String(s || "").toLowerCase()));
  const isEstadoBloqueadoReparado = estadosBloqueadosReparado.has(estadoLower);
  const reparacionBloqueadaCotizacion = !!isCotizacion && !permiteReparacion;
  const puedeReparar = !!canAutorizarReparar && estadoLower !== "reparar" && !isEstadoBloqueadoDiag && !reparacionBloqueadaCotizacion;
  const sinTecnicoAsignado = !data?.asignado_a;

  useEffect(() => {
    try {
      const v = (data?.serial_cambio || "").toString();
      setSerialCambio(v);
    } catch {}
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.id]);

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

  async function saveResolucionCambioAware() {
    try {
      if (!resolucion) { setErr("Seleccioná una resolución."); return; }
      if (String(resolucion) === RESOLUCION.CAMBIO) {
        const s = (serialCambio || "").trim();
        if (!s) { setErr("Ingrese la Serie (Cambio)."); return; }
      }
      setSavingResol(true);
      const payload = String(resolucion) === RESOLUCION.CAMBIO
        ? { resolucion, serial_cambio: (serialCambio || "").trim() }
        : { resolucion };
      await postCerrarReparacion(id, payload);
      await refreshIngreso();
      try {
        const blob = await getBlob(`/api/ingresos/${id}/remito/`);
        if (blob instanceof Blob) {
          const url = URL.createObjectURL(blob);
          window.open(url, "_blank", "noopener");
          setTimeout(() => URL.revokeObjectURL(url), 60_000);
        }
      } catch {}
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo guardar la resolución");
    } finally {
      setSavingResol(false);
    }
  }


  async function marcarControladoSinDefecto() {
    try {
      setSavingAll(true);
      const resp = await postMarcarControladoSinDefecto(id);
      await refreshIngreso();
      const movedMsg = resp && resp.auto_moved
        ? `Marcado como controlado sin defecto. Movido a ${resp.ubicacion_nombre || resp.auto_moved_to || "Estanteria de Alquiler"}`
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
      const d = (nuevoAcc?.descripcion || "").trim().toLowerCase();
      if (!d) { setErr("Escribí­ una descripción"); return; }
      const acc = (accesCatalogo || []).find(a => (a?.nombre || "").trim().toLowerCase() === d);
      if (!acc) { setErr("Elegí­ una descripción válida de la lista"); return; }
      setAddingAcc(true);
      await postAccesorioIngreso(id, {
        accesorio_id: Number(acc.id),
        referencia: (nuevoAcc?.referencia || "").trim() || null,
      });
      setNuevoAcc && setNuevoAcc({ descripcion: "", referencia: "" });
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

  // (El guardado de diagnóstico/trabajos es automático; no hay botón de guardar.)

  async function saveResolucion() {
    try {
      if (!resolucion) { setErr("Seleccione una resolución."); return; }
      setSavingResol(true);
      await postCerrarReparacion(id, { resolucion });
      await refreshIngreso();
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo guardar la resolución");
    } finally {
      setSavingResol(false);
    }
  }

  return (
    <div className="border rounded p-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
        <div className="border rounded p-3 bg-gray-50">
          <div className="text-xs uppercase text-gray-500 mb-1">Informe preliminar</div>
          <div className="whitespace-pre-wrap">{data.informe_preliminar || "-"}</div>
        </div>
        <div className="border rounded p-3 bg-gray-50">
          <div className="text-xs uppercase text-gray-500 mb-1">Accesorios</div>
          <div>
            {Array.isArray(data.accesorios_items) && data.accesorios_items.length > 0 ? (
              <ul className="list-disc list-inside text-sm">
                {data.accesorios_items.map((it) => (
                  <li key={it.id} className="flex items-center justify-between gap-2">
                    <span>
                      {it.accesorio_nombre}
                      {it.referencia ? ` (ref: ${it.referencia})` : ""}
                    </span>
                    {canEditAcc && (
                      <button
                        className="text-red-600 text-xs"
                        onClick={() => removeAccesorio(it.id)}
                        disabled={deletingAccId === it.id}
                        type="button"
                      >
                        {deletingAccId === it.id ? "Quitando..." : "Quitar"}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="whitespace-pre-wrap">{data.accesorios || "-"}</div>
            )}
          </div>

          {canEditAcc && (
            <div className="mt-3 border-t pt-3">
              <div className="text-xs uppercase text-gray-500 mb-2">Agregar accesorio</div>
              <div className="flex flex-wrap items-end gap-2">
                <input
                  className="border rounded p-2 min-w-[240px]"
                  list="accesorios_catalogo"
                  placeholder="Descripción (elegí­ de la lista)"
                  value={nuevoAcc.descripcion}
                  onChange={(e) => setNuevoAcc((s) => ({ ...s, descripcion: e.target.value }))}
                />
                <datalist id="accesorios_catalogo">
                  {accesCatalogo.map((a) => (
                    <option key={a.id} value={a.nombre} />
                  ))}
                </datalist>
                <input
                  className="border rounded p-2 w-40"
                  placeholder="Nro de referencia (opcional)"
                  value={nuevoAcc.referencia}
                  onChange={(e) => setNuevoAcc((s) => ({ ...s, referencia: e.target.value }))}
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
          )}
        </div>
      </div>

      <h2 className="font-semibold mb-2">Descripción del problema (diagnóstico)</h2>

      <div className="flex flex-wrap items-end gap-3 mb-3">
        <div>
          <label className="block text-sm text-gray-600 mb-1">Fecha de servicio</label>
          <input
            type="date"
            className="border rounded p-2"
            value={fechaServStr ? fechaServStr.slice(0, 10) : ""}
            onChange={(e) => {
              const v = e.currentTarget.value;
              setFechaServStr(v ? `${v}T00:00` : "");
            }}
            max={maxLocalNow ? maxLocalNow.slice(0, 10) : undefined}
            placeholder="YYYY-MM-DD"
            disabled={typeof canEditDiag === 'boolean' ? !canEditDiag : false}
          />
        </div>

        <div className="ml-auto flex items-end gap-2">
          {reparacionBloqueadaCotizacion && (
            <div className="max-w-xs rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Ingreso en cotización de equipo. La reparación está bloqueada hasta habilitarla.
            </div>
          )}

          {reparacionBloqueadaCotizacion && canHabilitarReparacionCotizacion && (
            <button
              className="bg-amber-700 text-white px-3 py-2 rounded disabled:opacity-60"
              disabled={habilitandoReparacion}
              onClick={habilitarReparacionCotizacion}
              type="button"
            >
              {habilitandoReparacion ? "Habilitando..." : "Habilitar reparación"}
            </button>
          )}

          {puedeReparar && (
            <button
              className="bg-amber-600 text-white px-3 py-2 rounded disabled:opacity-60"
              disabled={marcandoReparar || sinTecnicoAsignado || !puedeReparar}
              onClick={marcarParaReparar}
              title={sinTecnicoAsignado ? "Asigná un técnico para habilitar reparacion" : undefined}
              type="button"
            >
              {marcandoReparar ? "Avisando..." : "Reparar"}
            </button>
          )}

          {canResolve && !reparacionBloqueadaCotizacion && (
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
                  {RESOLUCION_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>

              {String(resolucion) === RESOLUCION.CAMBIO && (
                <div>
                  <label className="block text-sm text-gray-600 mb-1">Serie (Cambio)</label>
                  <input
                    className="border rounded p-2 w-64"
                    value={serialCambio}
                    onChange={(e) => setSerialCambio(e.target.value)}
                    placeholder="Ej.: MG 1234 o serie del equipo entregado"
                  />
                </div>
              )}

              <button
                className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60"
                disabled={savingResol || !resolucion}
                onClick={saveResolucionCambioAware}
                type="button"
              >
                {savingResol ? "Guardando..." : "Guardar resolución"}
              </button>
            </>
          )}

          {(typeof canMarkReparado === 'boolean' ? canMarkReparado : actAsTech) && !isEstadoBloqueadoReparado && (
            <>
              {!isEstadoBloqueadoDiag && (
                <button
                  className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60"
                  onClick={marcarControladoSinDefecto}
                  type="button"
                >
                  {savingAll ? "Guardando..." : "Controlado sin defecto"}
                </button>
              )}
              {!reparacionBloqueadaCotizacion && (
                <button
                  className="bg-emerald-600 text-white px-3 py-2 rounded"
                  onClick={async () => {
                    try {
                      const resp = await postMarcarReparado(id);
                      await refreshIngreso();
                      if (typeof setShowReparadoToast === 'function') {
                        setShowReparadoToast(true);
                        setTimeout(() => setShowReparadoToast(false), 2000);
                      }
                      if (resp && resp.auto_moved) {
                        const movedMsg = `Marcado como reparado. Movido a ${resp.ubicacion_nombre || resp.auto_moved_to || 'Estanteria de Alquiler'}`;
                        setToastMsg(movedMsg);
                        setTimeout(() => setToastMsg('') , 3000);
                      }
                    } catch (e) {
                      setErr(e?.message || 'No se pudo marcar como reparado');
                    }
                  }}
                  type="button"
                >
                  Marcar reparado
                </button>
              )}
            </>
          )}
        </div>
      </div>

      <textarea
        className="w-full border rounded p-2 min-h-[180px]"
        value={descripcion}
        onChange={(e) => setDescripcion(e.target.value)}
        disabled={typeof canEditDiag === 'boolean' ? !canEditDiag : false}
        placeholder="Ej.: Ingreso de agua; placa de control con óxido; válvula X no abre..."
      />

      <div className="border rounded p-4 mt-4">
        <h2 className="font-semibold mb-2">Trabajos a realizar/realizados</h2>
        <textarea
          className="w-full border rounded p-2 min-h-[200px]"
          value={trabajos}
          onChange={(e) => setTrabajos(e.target.value)}
          disabled={typeof canEditDiag === 'boolean' ? !canEditDiag : false}
          placeholder="Ej.: Cambio de turbina; limpieza y secado; resoldado de conector; calibración; pruebas OK."
        />
        <div className="mt-2 text-xs text-gray-500" aria-live="polite">
          {(savingDiag || savingAll) ? "Guardando..." : "Los cambios se guardan automáticamente"}
        </div>
      </div>

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
    </div>
  );
}

