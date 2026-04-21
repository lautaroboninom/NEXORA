import { useEffect, useRef, useState } from "react";
import { getBlob } from "../../../lib/api";
import {
  getQuote,
  postQuoteItem,
  patchQuoteItem,
  deleteQuoteItem,
  patchQuoteResumen,
  postQuoteEmitir,
  postQuoteAprobar,
  postQuoteAnular,
  postQuoteNoAplica,
  postQuoteQuitarNoAplica,
  getRepuestosCatalogo,
} from "../../../lib/api";

const DEFAULT_FORMA_PAGO = "30 F.F.";
const DEFAULT_PLAZO_ENTREGA_TXT = "< 5 D\u00cdAS H\u00c1BILES";
const DEFAULT_GARANTIA_TXT = "90 D\u00cdAS";
const DEFAULT_MANT_OFERTA_TXT = "7 D\u00cdAS";

function buildEquipoHints(data) {
  const marca = String(data?.marca || "").trim();
  const modelo = String(data?.modelo || "").trim();
  const variante = String(data?.equipo_variante || "").trim();
  const seen = new Set();
  const hints = [
    [marca, modelo, variante],
    [marca, modelo],
    [modelo, variante],
    [modelo],
  ]
    .map((parts) => parts.filter(Boolean).join(" | ").trim())
    .filter(Boolean)
    .filter((value) => {
      const key = value.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  return hints;
}

export default function PresupuestoTab({ id, data, canManagePresupuesto, canSeeCosts, money, isCotizacion, refreshIngreso, setErr }) {
  const isAprobado = data.presupuesto_estado === "aprobado";
  const tituloDoc = isCotizacion ? "cotización" : "presupuesto";
  const tituloDocCap = isCotizacion ? "Cotización" : "Presupuesto";
  const garantiaTrabajos = (data?.garantia_reparacion_trabajos || "").trim();

  const [qErr, setQErr] = useState("");
  const [qLoading, setQLoading] = useState(false);
  const [quote, setQuote] = useState(null);
  const [itemDrafts, setItemDrafts] = useState({});
  const [repOptions, setRepOptions] = useState([]);
  const [repQuery, setRepQuery] = useState("");
  const [repListOpen, setRepListOpen] = useState(false);
  const [repActiveKey, setRepActiveKey] = useState(null);
  const [repHighlight, setRepHighlight] = useState(0);
  const repListRef = useRef(null);
  const repAnchorRef = useRef(null);
  const repItemRefs = useRef([]);

  const [autorizadoPor, setAutorizadoPor] = useState("Cliente");
  const [formaPago, setFormaPago] = useState(DEFAULT_FORMA_PAGO);
  const [plazoEntregaTxt, setPlazoEntregaTxt] = useState(DEFAULT_PLAZO_ENTREGA_TXT);
  const [garantiaTxt, setGarantiaTxt] = useState(DEFAULT_GARANTIA_TXT);
  const [mantOfertaTxt, setMantOfertaTxt] = useState(DEFAULT_MANT_OFERTA_TXT);
  const [emitiendo, setEmitiendo] = useState(false);
  const [aprobando, setAprobando] = useState(false);
  const [anulando, setAnulando] = useState(false);

  const [nuevoRep, setNuevoRep] = useState({ repuesto_id: "", repuesto_codigo: "", descripcion: "", qty: "1", precio_u: "" });
  const [manoObraStr, setManoObraStr] = useState("");
  const equipoHints = buildEquipoHints(data);

  async function loadQuote() {
    try {
      setQErr("");
      setQLoading(true);
      const q = await getQuote(id);
      setQuote(q);
      setManoObraStr(String(q?.mano_obra ?? "0"));
      setAutorizadoPor(q?.autorizado_por ?? "Cliente");
      setFormaPago(q?.forma_pago ?? DEFAULT_FORMA_PAGO);
      setPlazoEntregaTxt(q?.plazo_entrega_txt ?? DEFAULT_PLAZO_ENTREGA_TXT);
      setGarantiaTxt(q?.garantia_txt ?? DEFAULT_GARANTIA_TXT);
      setMantOfertaTxt(q?.mant_oferta_txt ?? DEFAULT_MANT_OFERTA_TXT);
    } catch (e) {
      setQErr(e?.message || `No se pudo cargar la ${tituloDoc}`);
      setQuote(null);
    } finally {
      setQLoading(false);
    }
  }

  useEffect(() => { loadQuote(); }, [id]);

  useEffect(() => {
    const validIds = new Set((quote?.items || []).map((it) => String(it?.id)));
    setItemDrafts((prev) => {
      let changed = false;
      const next = {};
      Object.entries(prev || {}).forEach(([key, value]) => {
        if (validIds.has(String(key))) {
          next[key] = value;
        } else {
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [quote]);

  useEffect(() => {
    let alive = true;
    const handle = setTimeout(() => {
      getRepuestosCatalogo({ q: repQuery, limit: 50, equipo_hint: equipoHints })
        .then((rows) => { if (alive) setRepOptions(rows || []); })
        .catch(() => {});
    }, 200);
    return () => { alive = false; clearTimeout(handle); };
  }, [repQuery, equipoHints.join("||")]);

  async function abrirPdf() {
    try {
      setQErr("");
      const blob = await getBlob(`/api/quotes/${id}/pdf/`);
      if (!(blob instanceof Blob)) throw new Error("La respuesta del API no fue un Blob.");
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      setQErr(e?.message || `No se pudo abrir el PDF de la ${tituloDoc}`);
    }
  }

  function normalizeRepuestoCodigo(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const sep = " - ";
    if (raw.includes(sep)) return raw.split(sep)[0].trim();
    return raw;
  }

  function findRepuestoByCode(code) {
    if (!code) return null;
    const target = String(code).trim().toUpperCase();
    return repOptions.find((r) => String(r.codigo || "").trim().toUpperCase() === target) || null;
  }

  function openRepList(key, raw, anchorEl) {
    setRepActiveKey(key);
    setRepListOpen(true);
    setRepHighlight(0);
    if (anchorEl) repAnchorRef.current = anchorEl;
    if (typeof raw === "string") setRepQuery(raw);
  }

  function closeRepList() {
    setRepListOpen(false);
    setRepActiveKey(null);
    repAnchorRef.current = null;
  }

  function updateItemDraft(id, patch) {
    setItemDrafts((prev) => ({
      ...prev,
      [id]: {
        ...(prev[id] || {}),
        ...patch,
      },
    }));
  }

  function clearItemDraft(id) {
    setItemDrafts((prev) => {
      if (!Object.prototype.hasOwnProperty.call(prev, id)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  function getItemValue(it, field) {
    const draft = itemDrafts[it.id];
    if (draft && Object.prototype.hasOwnProperty.call(draft, field)) {
      return draft[field];
    }
    return it?.[field] ?? "";
  }

  async function commitItemDraft(it) {
    const draft = itemDrafts[it.id];
    if (!draft) return;

    const payload = {};

    if (Object.prototype.hasOwnProperty.call(draft, "repuesto_codigo")) {
      const code = normalizeRepuestoCodigo(draft.repuesto_codigo || "");
      if (String(it.repuesto_codigo || "") !== code) {
        payload.repuesto_codigo = code || null;
      }
    }
    if (Object.prototype.hasOwnProperty.call(draft, "descripcion")) {
      const descripcion = String(draft.descripcion || "");
      if (String(it.descripcion || "") !== descripcion) {
        payload.descripcion = descripcion;
      }
    }
    if (Object.prototype.hasOwnProperty.call(draft, "qty")) {
      const qty = Number(draft.qty || 0);
      if (Number.isNaN(qty)) {
        setQErr("Cantidad inválida");
        return;
      }
      if (Number(it.qty || 0) !== qty) {
        payload.qty = qty;
      }
    }
    if (Object.prototype.hasOwnProperty.call(draft, "precio_u")) {
      const precio = Number(draft.precio_u || 0);
      if (Number.isNaN(precio) || precio < 0) {
        setQErr("Precio inválido");
        return;
      }
      if (Number(it.precio_u || 0) !== precio) {
        payload.precio_u = precio;
      }
    }

    if (!Object.keys(payload).length) {
      clearItemDraft(it.id);
      return;
    }

    try {
      setQErr("");
      const updated = await patchQuoteItem(id, it.id, payload);
      setQuote(updated);
      clearItemDraft(it.id);
    } catch (e) {
      setQErr(e?.message || "No se pudo actualizar el ítem");
    }
  }

  async function selectRepuestoForItem(it, rep) {
    const patch = { repuesto_codigo: rep?.codigo || "" };
    if (rep?.id) patch.repuesto_id = rep.id;
    if (rep?.precio_venta != null) patch.precio_u = Number(rep.precio_venta);
    try {
      setQErr("");
      const updated = await patchQuoteItem(id, it.id, patch);
      setQuote(updated);
      clearItemDraft(it.id);
    } catch (e) {
      setQErr(e?.message || "No se pudo actualizar el repuesto");
    }
    setRepQuery(rep?.codigo || "");
    closeRepList();
  }

  function selectRepuestoForNew(rep) {
    setNuevoRep((s) => ({
      ...s,
      repuesto_codigo: rep?.codigo || "",
      repuesto_id: rep?.id ? String(rep.id) : "",
      descripcion: rep?.nombre ? rep.nombre : s.descripcion,
      precio_u: rep?.precio_venta != null ? String(rep.precio_venta) : s.precio_u,
    }));
    setRepQuery(rep?.codigo || "");
    closeRepList();
  }

  function handleRepKeyDown(e, key, pick, raw) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!repListOpen || repActiveKey !== key) {
        openRepList(key, typeof raw === "string" ? raw : "", e.currentTarget);
        return;
      }
      if (!repOptions.length) return;
      const delta = e.key === "ArrowDown" ? 1 : -1;
      const next = (repHighlight + delta + repOptions.length) % repOptions.length;
      setRepHighlight(next);
      return;
    }
    if (e.key === "Enter") {
      if (repListOpen && repActiveKey === key && repOptions.length) {
        e.preventDefault();
        const idx = Math.max(0, Math.min(repHighlight, repOptions.length - 1));
        pick(repOptions[idx]);
      }
      return;
    }
    if (e.key === "Escape") {
      if (repListOpen) {
        e.preventDefault();
        closeRepList();
      }
    }
  }

  function RepuestosList({ onPick }) {
    return (
      <div
        ref={repListRef}
        role="listbox"
        className="absolute z-30 mt-1 w-full min-w-[18rem] max-h-56 overflow-auto rounded border bg-white shadow"
        style={{ overscrollBehavior: "contain" }}
      >
        {(repOptions || []).length ? (
          repOptions.map((r, idx) => (
            <button
              key={r.id || r.codigo}
              type="button"
              role="option"
              aria-selected={idx === repHighlight}
              className={`w-full text-left px-2 py-1 hover:bg-gray-100 ${idx === repHighlight ? "bg-gray-100" : ""}`}
              ref={(el) => { repItemRefs.current[idx] = el; }}
              tabIndex={-1}
              onMouseDown={(e) => {
                e.preventDefault();
                onPick(r);
              }}
              onMouseEnter={() => setRepHighlight(idx)}
            >
              <div className="text-xs text-gray-500">{r.codigo}</div>
              <div className="text-sm">{r.nombre}</div>
              {r.precio_venta != null ? (
                <div className="text-xs text-gray-400">Precio sugerido: {money(r.precio_venta)}</div>
              ) : null}
            </button>
          ))
        ) : (
          <div className="px-2 py-1 text-xs text-gray-400">Sin resultados</div>
        )}
      </div>
    );
  }

  useEffect(() => {
    if (!repListOpen) return;
    if (!repOptions.length) {
      setRepHighlight(0);
      return;
    }
    if (repHighlight < 0 || repHighlight >= repOptions.length) {
      setRepHighlight(0);
    }
  }, [repListOpen, repOptions, repHighlight]);

  useEffect(() => {
    if (!repListOpen || !repOptions.length) return;
    const listEl = repListRef.current;
    const el = repItemRefs.current[repHighlight];
    if (!listEl || !el) return;
    const itemTop = el.offsetTop;
    const itemBottom = itemTop + el.offsetHeight;
    const viewTop = listEl.scrollTop;
    const viewBottom = viewTop + listEl.clientHeight;
    if (itemTop < viewTop) {
      listEl.scrollTop = itemTop;
      return;
    }
    if (itemBottom > viewBottom) {
      listEl.scrollTop = itemBottom - listEl.clientHeight;
    }
  }, [repListOpen, repHighlight, repOptions.length]);

  useEffect(() => {
    if (!repListOpen) return;
    const handlePointerDown = (ev) => {
      const listEl = repListRef.current;
      const anchorEl = repAnchorRef.current;
      const target = ev.target;
      if (listEl && listEl.contains(target)) return;
      if (anchorEl && anchorEl.contains(target)) return;
      closeRepList();
    };
    const handleFocusIn = (ev) => {
      const listEl = repListRef.current;
      const anchorEl = repAnchorRef.current;
      const target = ev.target;
      if (listEl && listEl.contains(target)) return;
      if (anchorEl && anchorEl.contains(target)) return;
      closeRepList();
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("focusin", handleFocusIn);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("focusin", handleFocusIn);
    };
  }, [repListOpen]);

  async function emitirPresupuesto() {
    try {
      setEmitiendo(true);
      const r = await postQuoteEmitir(id, {
        autorizado_por: autorizadoPor,
        forma_pago: formaPago,
        plazo_entrega_txt: plazoEntregaTxt,
        garantia_txt: garantiaTxt,
        mant_oferta_txt: mantOfertaTxt,
      });
      setQuote(r);
      if (typeof refreshIngreso === "function") await refreshIngreso();
      if (r?.pdf_url) await abrirPdf();
    } catch (e) {
      setQErr(e?.message || `No se pudo emitir la ${tituloDoc}`);
    } finally {
      setEmitiendo(false);
    }
  }

  async function anularPresupuesto() {
    if (!confirm(`Anular la ${tituloDoc} actual? Aplica también si está aprobada. Podrás editar y reemitir luego.`)) return;
    try {
      setAnulando(true);
      setQErr("");
      const r = await postQuoteAnular(id);
      setQuote(r);
      if (typeof refreshIngreso === "function") await refreshIngreso();
    } catch (e) {
      setQErr(e?.message || `No se pudo anular la ${tituloDoc}`);
    } finally {
      setAnulando(false);
    }
  }

  async function aprobarPresupuesto() {
    try {
      setAprobando(true);
      setQErr("");
      const shouldPrint = (data?.estado || "").toLowerCase() === "reparado" &&
        window.confirm("Este equipo ya está reparado, imprimir remito de salida?");

      const r = await postQuoteAprobar(id);
      setQuote(r);
      if (shouldPrint && typeof refreshIngreso === "function") {
        try {
          const blob = await getBlob(`/api/ingresos/${id}/remito/`);
          if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");
          const url = URL.createObjectURL(blob);
          window.open(url, "_blank", "noopener");
          setTimeout(() => URL.revokeObjectURL(url), 60_000);
          await refreshIngreso();
        } catch (e) {
          setQErr(e?.message || "No se pudo imprimir el remito de salida");
        }
      }
      if (typeof refreshIngreso === "function") await refreshIngreso();
    } catch (e) {
      setQErr(e?.message || `No se pudo aprobar la ${tituloDoc}`);
    } finally {
      setAprobando(false);
    }
  }

  async function marcarNoAplica() {
    try {
      setQErr("");
      setEmitiendo(true);
      const r = await postQuoteNoAplica(id);
      setQuote(r);
      if (typeof refreshIngreso === "function") await refreshIngreso();
    } catch (e) {
      setQErr(e?.message || "No se pudo marcar 'No aplica'");
    } finally {
      setEmitiendo(false);
    }
  }

  async function quitarNoAplica() {
    try {
      setQErr("");
      setEmitiendo(true);
      const r = await postQuoteQuitarNoAplica(id);
      setQuote(r);
      if (typeof refreshIngreso === "function") await refreshIngreso();
    } catch (e) {
      setQErr(e?.message || "No se pudo quitar 'No aplica'");
    } finally {
      setEmitiendo(false);
    }
  }

  async function addRepuesto() {
    const qty = Number(nuevoRep.qty || 0);
    const puRaw = nuevoRep.precio_u;
    const pu = (puRaw == null || puRaw === "") ? null : Number(puRaw);
    if (!nuevoRep.descripcion.trim()) { setQErr("Descripción requerida"); return; }
    //if (qty <= 0) { setQErr("Cantidad > 0"); return; }
    if (pu != null && pu < 0) { setQErr("Precio inválido"); return; }
    const repCodigo = normalizeRepuestoCodigo(nuevoRep.repuesto_codigo || "");
    const updated = await postQuoteItem(id, {
      tipo: "repuesto",
      repuesto_id: nuevoRep.repuesto_id ? Number(nuevoRep.repuesto_id) : null,
      repuesto_codigo: repCodigo || null,
      descripcion: nuevoRep.descripcion.trim(),
      qty, precio_u: pu,
    });
    setNuevoRep({ repuesto_id: "", repuesto_codigo: "", descripcion: "", qty: "1", precio_u: "" });
    setQuote(updated);
  }

  async function handleRemoveItem(it) {
    if (!confirm("Eliminar renglón?")) return;
    try {
      const updated = await deleteQuoteItem(id, it.id);
      setQuote(updated);
      clearItemDraft(it.id);
    } catch (e) {
      setQErr(e?.message || "No se pudo eliminar el renglón");
    }
  }
  async function saveManoObra() {
    const mo = Number(manoObraStr || 0);
    if (mo < 0) { setQErr("Mano de obra inválida"); return; }
    const updated = await patchQuoteResumen(id, { mano_obra: mo });
    setQuote(updated);
    setManoObraStr(String(updated?.mano_obra ?? mo));
  }

  return (
    <div className="border rounded p-4">

      <div className="border rounded p-3 mb-4 bg-gray-50">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <div className="text-sm text-gray-600">Diagnóstico</div>
            <div className="whitespace-pre-wrap">{(data?.descripcion_problema || "-")}</div>
          </div>
          <div>
            <div className="text-sm text-gray-600">Trabajos realizados</div>
            <div className="whitespace-pre-wrap">{(data?.trabajos_realizados || "-")}</div>
          </div>
        </div>
      </div>

      {(data?.garantia || data?.garantia_reparacion) && (
        <div className="bg-yellow-100 border border-yellow-300 text-yellow-800 p-2 rounded mb-3" role="status" aria-label="Aviso de garantía">
          <span className="font-medium">Aviso:</span>
          <span> Equipo en {data?.garantia ? "garantía de fábrica" : ""}{data?.garantia && data?.garantia_reparacion ? " y " : ""}{data?.garantia_reparacion ? "garantía de reparación" : ""}.</span>
          {data?.faja_garantia ? (
            <span className="ml-2 text-xs text-yellow-700">Faja: {data.faja_garantia}</span>
          ) : null}
          {data?.garantia_reparacion ? (
            <div className="mt-1 text-sm text-yellow-900">
              <span className="font-medium">Trabajos realizados (último servicio):</span>{" "}
              <span className="whitespace-pre-wrap">{garantiaTrabajos || "-"}</span>
            </div>
          ) : null}
        </div>
      )}

      {qErr && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">{qErr}</div>
      )}

      <div className="flex flex-wrap gap-3 items-end mb-4">
        <label className="block">
          <div className="text-sm text-gray-600">Autorizado por</div>
          <input className="border rounded p-2" value={autorizadoPor} onChange={(e) => setAutorizadoPor(e.target.value)} />
        </label>
        <label className="block">
          <div className="text-sm text-gray-600">Forma de pago</div>
          <input className="border rounded p-2" value={formaPago} onChange={(e) => setFormaPago(e.target.value)} />
        </label>
        <label className="block">
          <div className="text-sm text-gray-600">Plazo de entrega</div>
          <input className="border rounded p-2" value={plazoEntregaTxt} onChange={(e) => setPlazoEntregaTxt(e.target.value)} />
        </label>
        <label className="block">
          <div className="text-sm text-gray-600">Garantía</div>
          <input className="border rounded p-2" value={garantiaTxt} onChange={(e) => setGarantiaTxt(e.target.value)} />
        </label>
        <label className="block">
          <div className="text-sm text-gray-600">Mant. de oferta</div>
          <input className="border rounded p-2" value={mantOfertaTxt} onChange={(e) => setMantOfertaTxt(e.target.value)} />
        </label>
        {canManagePresupuesto && data.presupuesto_estado === "pendiente" && (
          <button className="bg-blue-600 text-white px-3 py-2 rounded disabled:opacity-60" onClick={emitirPresupuesto} disabled={emitiendo}>
            {emitiendo ? "Emitiendo..." : `Emitir ${tituloDoc}`}
          </button>
        )}
        {["presupuestado", "aprobado"].includes(data.presupuesto_estado) && (
          <button className="underline text-blue-700" onClick={abrirPdf} type="button">
            {`Ver/Descargar PDF de ${tituloDocCap}`}
          </button>
        )}
        {canManagePresupuesto && data.presupuesto_estado === "presupuestado" && (
          <button className="bg-emerald-600 text-white px-3 py-2 rounded disabled:opacity-60" onClick={aprobarPresupuesto} disabled={aprobando} type="button">
            {aprobando ? "Aprobando..." : `Aprobar ${tituloDoc}`}
          </button>
        )}
        {canManagePresupuesto && ["presupuestado", "aprobado"].includes(data.presupuesto_estado) && (
          <button className="bg-red-600 text-white px-3 py-2 rounded disabled:opacity-60" onClick={anularPresupuesto} disabled={anulando} type="button">
            {anulando ? "Anulando..." : `Anular ${tituloDoc}`}
          </button>
        )}
        {canManagePresupuesto && data.presupuesto_estado === "pendiente" && (
          <button className="bg-neutral-600 text-white px-3 py-2 rounded disabled:opacity-60" onClick={marcarNoAplica} disabled={emitiendo} type="button">
            {emitiendo ? "Marcando..." : "No aplica"}
          </button>
        )}
        {canManagePresupuesto && data.presupuesto_estado === "no_aplica" && (
          <button className="bg-neutral-500 text-white px-3 py-2 rounded disabled:opacity-60" onClick={quitarNoAplica} disabled={emitiendo} type="button">
            {emitiendo ? "Marcando..." : "Quitar 'No aplica'"}
          </button>
        )}
      </div>

      {qLoading || !quote ? (
        <div>Cargando...</div>
      ) : (
        <>
          {isAprobado && (
            <div className="mb-3 text-sm text-emerald-700">{`${tituloDocCap} aprobada - los ítems y valores ya no son editables.`}</div>
          )}

          <h3 className="font-medium mb-2">Repuestos</h3>
          <table className="min-w-full text-sm mb-3">
            <thead>
              <tr className="text-left">
                <th className="p-2 w-32">Codigo</th>
                <th className="p-2">Descripción</th>
                <th className="p-2 w-24">Cantidad</th>
                {canSeeCosts ? <th className="p-2 w-36">Costo unit.</th> : null}
                <th className="p-2 w-36">Precio unit.</th>
                <th className="p-2 w-36 text-right">Precio total</th>
                <th className="p-2 w-20"></th>
              </tr>
            </thead>
            <tbody>
              {quote.items
                .filter((it) => it.tipo === "repuesto")
                .map((it) => (
                  <tr key={it.id} className="border-t">
                    <td className="p-2">
                      <div className="relative">
                        <input
                          className="border rounded p-1 w-28"
                          value={getItemValue(it, "repuesto_codigo")}
                          onFocus={(e) => openRepList(`code-${it.id}`, getItemValue(it, "repuesto_codigo"), e.currentTarget)}
                          onKeyDown={(e) => {
                            handleRepKeyDown(e, `code-${it.id}`, (rep) => selectRepuestoForItem(it, rep), e.currentTarget.value);
                            if (e.key === "Enter" && (!repListOpen || repActiveKey !== `code-${it.id}` || !repOptions.length)) {
                              e.preventDefault();
                              void commitItemDraft(it);
                            }
                          }}
                          onChange={(e) => {
                            const raw = e.target.value;
                            openRepList(`code-${it.id}`, raw, e.currentTarget);
                            updateItemDraft(it.id, { repuesto_codigo: raw });
                          }}
                          onBlur={() => { void commitItemDraft(it); }}
                          disabled={isAprobado}
                        />
                        {repListOpen && repActiveKey === `code-${it.id}` ? (
                          <RepuestosList onPick={(rep) => selectRepuestoForItem(it, rep)} />
                        ) : null}
                      </div>
                    </td>
                    <td className="p-2">
                      <div className="relative">
                        <input
                          className="border rounded p-1 w-full"
                          value={getItemValue(it, "descripcion")}
                          onFocus={(e) => openRepList(`desc-${it.id}`, getItemValue(it, "descripcion"), e.currentTarget)}
                          onKeyDown={(e) => {
                            handleRepKeyDown(e, `desc-${it.id}`, (rep) => selectRepuestoForItem(it, rep), e.currentTarget.value);
                            if (e.key === "Enter" && (!repListOpen || repActiveKey !== `desc-${it.id}` || !repOptions.length)) {
                              e.preventDefault();
                              void commitItemDraft(it);
                            }
                          }}
                          onChange={(e) => {
                            const raw = e.target.value;
                            openRepList(`desc-${it.id}`, raw, e.currentTarget);
                            updateItemDraft(it.id, { descripcion: raw });
                          }}
                          onBlur={() => { void commitItemDraft(it); }}
                          disabled={isAprobado}
                        />
                        {repListOpen && repActiveKey === `desc-${it.id}` ? (
                          <RepuestosList onPick={(rep) => selectRepuestoForItem(it, rep)} />
                        ) : null}
                      </div>
                    </td>
                    <td className="p-2">
                      <input
                        type="number"
                        step="0.01"
                        className="border rounded p-1 w-24 text-right"
                        value={getItemValue(it, "qty")}
                        onChange={(e) => updateItemDraft(it.id, { qty: e.target.value })}
                        onBlur={() => { void commitItemDraft(it); }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void commitItemDraft(it);
                          }
                        }}
                        disabled={isAprobado}
                      />
                    </td>
                    {canSeeCosts ? (
                      <td className="p-2">
                        {it.costo_u_neto != null ? money(it.costo_u_neto) : "-"}
                      </td>
                    ) : null}
                    <td className="p-2">
                      <input
                        type="number"
                        step="0.01"
                        className="border rounded p-1 w-32 text-right"
                        value={getItemValue(it, "precio_u")}
                        onChange={(e) => updateItemDraft(it.id, { precio_u: e.target.value })}
                        onBlur={() => { void commitItemDraft(it); }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void commitItemDraft(it);
                          }
                        }}
                        disabled={isAprobado}
                      />
                    </td>
                    <td className="p-2 text-right">{money(it.subtotal)}</td>
                    <td className="p-2">
                      <button className="text-red-600 hover:underline" onClick={() => handleRemoveItem(it)} type="button" disabled={isAprobado}>
                        borrar
                      </button>
                    </td>
                  </tr>
                ))}

              <tr className="border-t bg-gray-50">
                <td className="p-2">
                  <div className="relative">
                    <input
                      className="border rounded p-1 w-28"
                      placeholder="Código"
                      value={nuevoRep.repuesto_codigo}
                      onFocus={(e) => openRepList("code-new", nuevoRep.repuesto_codigo || "", e.currentTarget)}
                      onKeyDown={(e) => handleRepKeyDown(e, "code-new", selectRepuestoForNew, e.currentTarget.value)}
                      onChange={(e) => {
                        const raw = e.target.value;
                        const code = normalizeRepuestoCodigo(raw);
                        openRepList("code-new", raw, e.currentTarget);
                        const found = findRepuestoByCode(code);
                        setNuevoRep((s) => ({
                          ...s,
                          repuesto_codigo: code,
                          repuesto_id: found?.id ? String(found.id) : "",
                          descripcion: found?.nombre ? found.nombre : s.descripcion,
                          precio_u: found?.precio_venta != null ? String(found.precio_venta) : s.precio_u,
                        }));
                      }}
                      disabled={isAprobado}
                    />
                    {repListOpen && repActiveKey === "code-new" ? (
                      <RepuestosList onPick={selectRepuestoForNew} />
                    ) : null}
                  </div>
                </td>
                <td className="p-2">
                  <div className="relative">
                    <input
                      className="border rounded p-1 w-full"
                      placeholder="Descripción del repuesto"
                      value={nuevoRep.descripcion}
                      onFocus={(e) => openRepList("desc-new", nuevoRep.descripcion || "", e.currentTarget)}
                      onKeyDown={(e) => handleRepKeyDown(e, "desc-new", selectRepuestoForNew, e.currentTarget.value)}
                      onChange={(e) => {
                        const raw = e.target.value;
                        openRepList("desc-new", raw, e.currentTarget);
                        setNuevoRep((s) => ({ ...s, descripcion: raw }));
                      }}
                      disabled={isAprobado}
                    />
                    {repListOpen && repActiveKey === "desc-new" ? (
                      <RepuestosList onPick={selectRepuestoForNew} />
                    ) : null}
                  </div>
                </td>
                <td className="p-2">
                  <input type="number" step="0.01" min="0" className="border rounded p-1 w-24 text-right" value={nuevoRep.qty} onChange={(e) => setNuevoRep((s) => ({ ...s, qty: e.target.value }))} disabled={isAprobado} />
                </td>
                {canSeeCosts ? <td className="p-2">-</td> : null}
                <td className="p-2">
                  <input type="number" step="0.01" className="border rounded p-1 w-32 text-right" placeholder="0.00" value={nuevoRep.precio_u} onChange={(e) => setNuevoRep((s) => ({ ...s, precio_u: e.target.value }))} disabled={isAprobado} />
                </td>
                <td className="p-2 text-right"></td>
                <td className="p-2">
                  <button className="bg-blue-600 text-white px-2 py-1 rounded" onClick={addRepuesto} type="button" disabled={isAprobado}>
                    agregar
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <div className="flex items-end gap-3 mb-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">Mano de obra</label>
              <input type="number" step="0.01" min="0" className="border rounded p-2 w-48 text-right" value={manoObraStr} onChange={(e) => setManoObraStr(e.target.value)} disabled={isAprobado} />
            </div>
            <button className="bg-blue-600 text-white px-3 py-2 rounded" onClick={saveManoObra} type="button" disabled={isAprobado}>
              Guardar
            </button>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            <div className="border rounded p-3">
              <div className="text-gray-600 text-sm">Total repuestos</div>
              <div className="text-lg font-semibold">{money(quote.tot_repuestos)}</div>
            </div>
            <div className="border rounded p-3">
              <div className="text-gray-600 text-sm">Mano de obra</div>
              <div className="text-lg font-semibold">{money(quote.mano_obra)}</div>
            </div>
            <div className="border rounded p-3">
              <div className="text-gray-600 text-sm">IVA 21%</div>
              <div className="text-lg font-semibold">{money(quote.iva_21)}</div>
            </div>
            <div className="border rounded p-3">
              <div className="text-gray-600 text-sm">Total</div>
              <div className="text-lg font-semibold">{money(quote.subtotal)}</div>
            </div>
            <div className="border rounded p-3">
              <div className="text-gray-600 text-sm">Costo cliente (con IVA)</div>
              <div className="text-xl font-bold">{money(quote.total)}</div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

