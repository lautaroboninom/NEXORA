import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate, useSearchParams } from "react-router-dom";
import Tabs from "../components/Tabs";
import {
  getCatalogModelos,
  getCatalogTipos,
  getCatalogVariantes,
  getDevices,
  getMarcas,
  getMarcasPorTipo,
  getModelosByBrand,
  getTiposEquipo,
  getUbicaciones,
  getDeviceEditable,
  postDeviceDirectCreate,
  patchDeviceEditable,
  postDevicesMerge,
  postDevicePreventivoPlan,
  patchDevicePreventivoPlan,
  postDevicePreventivoRevision,
  getDevicePreventivoRepuestos,
  postDevicePreventivoRepuesto,
  patchDevicePreventivoRepuesto,
  deleteDevicePreventivoRepuesto,
  getPreventivosAgenda,
  getPreventivosClientes,
  getRepuestosCatalogo,
  postCustomerPreventivoPlan,
  patchCustomerPreventivoPlan,
  getCustomerPreventivoRevisiones,
  postCustomerPreventivoRevision,
  getPreventivoRevision,
  postPreventivoRevisionItem,
  patchPreventivoRevisionItem,
  postPreventivoRevisionCerrar,
  getClientes,
  postCliente,
  postDeviceMgVenta,
  postDeviceMgReactivar,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { tipoEquipoOf } from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";

const TAB_ITEMS = [
  { value: "equipos", label: "Equipos" },
  { value: "preventivos", label: "Mantenimientos preventivos" },
  { value: "instituciones", label: "Instituciones" },
];

const PERIODICIDAD_UNIDADES = [
  { value: "dias", label: "Días" },
  { value: "meses", label: "Meses" },
  { value: "anios", label: "Años" },
];

const ITEM_STATES = [
  { value: "pendiente", label: "Pendiente" },
  { value: "ok", label: "OK" },
  { value: "retirado", label: "Retirado" },
  { value: "no_controlado", label: "No controlado" },
];

function todayISO() {
  const now = new Date();
  const mm = `${now.getMonth() + 1}`.padStart(2, "0");
  const dd = `${now.getDate()}`.padStart(2, "0");
  return `${now.getFullYear()}-${mm}-${dd}`;
}

function fmtDate(v) {
  if (!v) return "-";
  const s = String(v).trim();
  const base = s.includes("T") ? s.slice(0, 10) : s;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(base);
  if (!m) return base;
  const [, yyyy, mm, dd] = m;
  return `${dd}-${mm}-${yyyy}`;
}

function estadoClass(estado) {
  if (estado === "vencido") return "bg-red-100 text-red-800";
  if (estado === "proximo") return "bg-amber-100 text-amber-800";
  if (estado === "sin_plan") return "bg-slate-100 text-slate-700";
  return "bg-emerald-100 text-emerald-800";
}

function estadoLabel(estado) {
  if (estado === "vencido") return "Vencido";
  if (estado === "proximo") return "Próximo";
  if (estado === "sin_plan") return "Sin plan";
  if (estado === "al_dia") return "Al día";
  return estado || "-";
}

function PreventivoBadge({ estado, dias }) {
  return (
    <span className={`px-2 py-1 text-xs rounded ${estadoClass(estado)}`}>
      {estadoLabel(estado)}
      {typeof dias === "number" ? ` (${dias})` : ""}
    </span>
  );
}

function PropiedadBadge({ row }) {
  const isMg = !!row?.es_propietario_mg;
  const mgInactivoVenta = !!row?.mg_inactivo_venta;
  const alquilado = !!row?.alquilado;
  const hasNumeroInterno = !!String(row?.numero_interno || "").trim();
  if (mgInactivoVenta) {
    return <span className="px-2 py-1 text-xs rounded bg-gray-100 text-gray-700">Cliente (Ex MG)</span>;
  }
  if (isMg) {
    if (hasNumeroInterno) return <span className="px-2 py-1 text-xs rounded bg-emerald-100 text-emerald-800">Propio (MG)</span>;
    if (alquilado) return <span className="px-2 py-1 text-xs rounded bg-blue-100 text-blue-800">Propio (alquilado)</span>;
    return <span className="px-2 py-1 text-xs rounded bg-emerald-100 text-emerald-800">Propio (MG/BIO)</span>;
  }
  return <span className="px-2 py-1 text-xs rounded bg-gray-100 text-gray-700">Cliente</span>;
}

function isOwnerDisplayCustomer(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (!normalized) return false;
  if (normalized === "particular") return true;
  return normalized.includes("mg") && normalized.includes("bio");
}

function deviceCustomerTitle(row) {
  const customer = String(row?.last_customer_nombre || row?.customer_nombre || "").trim();
  const owner = String(row?.propietario_nombre || "").trim();
  if (owner && isOwnerDisplayCustomer(customer)) return owner;
  return customer || owner || "-";
}

function deviceCustomerSubtitle(row) {
  const customer = String(row?.last_customer_nombre || row?.customer_nombre || "").trim();
  const owner = String(row?.propietario_nombre || "").trim();
  if (owner && customer && isOwnerDisplayCustomer(customer)) {
    return `Cliente base: ${customer}`;
  }
  if (owner && customer && owner.toLowerCase() !== customer.toLowerCase()) {
    return `Dueño: ${owner}`;
  }
  return "";
}

function deviceOwnerMeta(row) {
  const parts = [row?.propietario_contacto, row?.propietario_doc]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  return parts.join(" | ");
}

function deviceModelTitle(row) {
  const modelo = String(row?.modelo || "").trim();
  const variante = String(row?.variante || "").trim();
  return [modelo, variante].filter(Boolean).join(" ").trim() || "-";
}

function normalizeCustomerRows(rows = []) {
  const map = new Map();
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const id = Number(row?.customer_id ?? row?.id ?? 0);
    if (!Number.isFinite(id) || id <= 0) return;
    map.set(String(id), {
      id,
      razon_social: String(row?.razon_social || "").trim(),
      cod_empresa: String(row?.cod_empresa || "").trim(),
    });
  });
  return Array.from(map.values()).sort((a, b) =>
    String(a?.razon_social || "").localeCompare(String(b?.razon_social || ""))
  );
}

function mergeCustomerRows(...lists) {
  const map = new Map();
  lists.forEach((list) => {
    normalizeCustomerRows(list).forEach((row) => {
      map.set(String(row.id), row);
    });
  });
  return Array.from(map.values()).sort((a, b) =>
    String(a?.razon_social || "").localeCompare(String(b?.razon_social || ""))
  );
}

function EditModal({ deviceId, onClose, onSaved, canEdit, customers = [] }) {
  const [form, setForm] = useState({
    customer_id: "",
    tipo_equipo: "",
    marca_id: "",
    modelo_id: "",
    variante: "",
    numero_serie: "",
    numero_interno: "",
    ubicacion_id: "",
    alquilado: false,
    alquiler_customer_id: "",
    alquiler_a: "",
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [catalogErr, setCatalogErr] = useState("");
  const [tiposEquipo, setTiposEquipo] = useState([]);
  const [marcas, setMarcas] = useState([]);
  const [marcasPorTipo, setMarcasPorTipo] = useState([]);
  const [modelos, setModelos] = useState([]);
  const [varianteSugeridas, setVarianteSugeridas] = useState([]);
  const [ubicaciones, setUbicaciones] = useState([]);
  const [customerOptions, setCustomerOptions] = useState(() => normalizeCustomerRows(customers));
  const [marcaTxt, setMarcaTxt] = useState("");
  const [marcaId, setMarcaId] = useState(null);
  const [catTipoId, setCatTipoId] = useState(null);
  const [catModelos, setCatModelos] = useState([]);

  const tipoSel = (form.tipo_equipo || "").trim();

  useEffect(() => {
    setCustomerOptions((prev) => mergeCustomerRows(prev, customers));
  }, [customers]);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const rows = await getClientes();
        if (!active) return;
        setCustomerOptions((prev) => mergeCustomerRows(prev, rows));
      } catch {
        if (!active) return;
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!deviceId) return;
    let active = true;
    (async () => {
      try {
        setLoading(true);
        setErr("");
        setCatalogErr("");
        const [deviceRes, marcasRows, tiposRows, ubicRows] = await Promise.all([
          getDeviceEditable(deviceId),
          getMarcas(),
          getTiposEquipo(),
          getUbicaciones(),
        ]);
        if (!active) return;

        const dev = deviceRes?.device || null;
        if (!dev?.id) {
          throw new Error("No se pudo obtener el equipo a editar.");
        }

        setMarcas(Array.isArray(marcasRows) ? marcasRows : []);
        const tipoList = (Array.isArray(tiposRows) ? tiposRows : [])
          .map((t) => t?.nombre || t?.label || t?.name || t?.value || t)
          .map(String)
          .map((s) => s.trim())
          .filter(Boolean);
        setTiposEquipo(Array.from(new Set(tipoList)));
        setUbicaciones(Array.isArray(ubicRows) ? ubicRows : []);

        const alquilerA = String(dev?.alquiler_a || "").trim();
        const matchedAlquiler = mergeCustomerRows(customers, customerOptions).find(
          (c) => String(c?.razon_social || "").trim().toLowerCase() === alquilerA.toLowerCase()
        );

        setForm({
          customer_id: dev?.customer_id ? String(dev.customer_id) : "",
          tipo_equipo: String(dev?.tipo_equipo || ""),
          marca_id: dev?.marca_id ? String(dev.marca_id) : "",
          modelo_id: dev?.model_id ? String(dev.model_id) : "",
          variante: String(dev?.variante || ""),
          numero_serie: String(dev?.numero_serie || ""),
          numero_interno: String(dev?.numero_interno || ""),
          ubicacion_id: dev?.ubicacion_id ? String(dev.ubicacion_id) : "",
          alquilado: !!dev?.alquilado,
          alquiler_customer_id: matchedAlquiler?.id ? String(matchedAlquiler.id) : "",
          alquiler_a: alquilerA,
        });
        setMarcaId(dev?.marca_id ? Number(dev.marca_id) : null);
        setMarcaTxt(String(dev?.marca || ""));
      } catch (e) {
        if (!active) return;
        setErr(e?.message || "No se pudo cargar el equipo para edición.");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [deviceId]);

  useEffect(() => {
    if (!form.alquilado || form.alquiler_customer_id) return;
    const alquilerA = String(form.alquiler_a || "").trim().toLowerCase();
    if (!alquilerA) return;
    const match = (customerOptions || []).find(
      (c) => String(c?.razon_social || "").trim().toLowerCase() === alquilerA
    );
    if (match?.id) {
      setForm((prev) => ({ ...prev, alquiler_customer_id: String(match.id) }));
    }
  }, [form.alquilado, form.alquiler_a, form.alquiler_customer_id, customerOptions]);

  useEffect(() => {
    let active = true;
    if (!tipoSel) {
      setMarcasPorTipo([]);
      return () => {
        active = false;
      };
    }
    (async () => {
      try {
        const rows = await getMarcasPorTipo(tipoSel);
        if (!active) return;
        setMarcasPorTipo(Array.isArray(rows) ? rows : []);
      } catch {
        if (!active) return;
        setMarcasPorTipo([]);
      }
    })();
    return () => {
      active = false;
    };
  }, [tipoSel]);

  useEffect(() => {
    let active = true;
    setModelos([]);
    setCatTipoId(null);
    setCatModelos([]);
    setVarianteSugeridas([]);
    if (!marcaId) {
      return () => {
        active = false;
      };
    }

    (async () => {
      try {
        setCatalogErr("");
        const rows = await getModelosByBrand(marcaId);
        if (!active) return;
        const list = Array.isArray(rows) ? rows : [];
        const norm = (s) => (s || "").toString().trim().toUpperCase();
        const filtered = tipoSel ? list.filter((m) => norm(m?.tipo_equipo) === norm(tipoSel)) : list;
        setModelos(filtered);

        const tiposBrand = await getCatalogTipos(marcaId);
        if (!active) return;
        const match = (Array.isArray(tiposBrand) ? tiposBrand : []).find(
          (t) => (t?.name || "").trim().toUpperCase() === (tipoSel || "").trim().toUpperCase()
        );
        const tId = match?.id ?? null;
        setCatTipoId(tId);
        if (tId) {
          const mods = await getCatalogModelos(marcaId, tId);
          if (!active) return;
          setCatModelos(Array.isArray(mods) ? mods : []);
        } else {
          setCatModelos([]);
        }
      } catch (e) {
        if (!active) return;
        setCatalogErr(e?.message || "No se pudieron cargar modelos.");
        setModelos([]);
        setCatTipoId(null);
        setCatModelos([]);
        setVarianteSugeridas([]);
      }
    })();

    return () => {
      active = false;
    };
  }, [marcaId, tipoSel]);

  useEffect(() => {
    let active = true;
    const selectedModel = (modelos || []).find((x) => String(x.id) === String(form.modelo_id));
    if (!selectedModel || !marcaId || !catTipoId) {
      setVarianteSugeridas([]);
      return () => {
        active = false;
      };
    }

    const needle = (selectedModel?.nombre || "").trim().toUpperCase();
    const cmatch = (catModelos || []).filter((cm) => {
      const a = (cm?.name || "").trim().toUpperCase();
      const alias = (cm?.alias || "").trim().toUpperCase();
      return (
        a === needle
        || a.includes(needle)
        || needle.includes(a)
        || (alias && (alias === needle || needle.includes(alias) || alias.includes(needle)))
      );
    });
    if (cmatch.length !== 1) {
      setVarianteSugeridas([]);
      return () => {
        active = false;
      };
    }

    (async () => {
      try {
        const vars = await getCatalogVariantes(marcaId, catTipoId, cmatch[0].id);
        if (!active) return;
        const names = (Array.isArray(vars) ? vars : []).filter((v) => v?.name).map((v) => v.name);
        setVarianteSugeridas(names);
      } catch {
        if (!active) return;
        setVarianteSugeridas([]);
      }
    })();

    return () => {
      active = false;
    };
  }, [form.modelo_id, modelos, marcaId, catTipoId, catModelos]);

  const update = (key, value) =>
    setForm((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "alquilado" && !value) {
        next.alquiler_customer_id = "";
        next.alquiler_a = "";
      }
      if (key === "alquiler_customer_id") {
        const selected = (customerOptions || []).find((c) => String(c.id) === String(value));
        next.alquiler_a = selected?.razon_social || "";
      }
      return next;
    });

  const onMarcaInput = (value) => {
    setMarcaTxt(value);
    const pool = tipoSel ? (marcasPorTipo.length ? marcasPorTipo : marcas) : marcas;
    const match = (pool || []).find(
      (m) => (m?.nombre || "").toLowerCase() === String(value || "").trim().toLowerCase()
    );
    const nextMarcaId = match ? Number(match.id) : null;
    setMarcaId(nextMarcaId);
    setForm((prev) => {
      const prevMarcaId = prev.marca_id ? Number(prev.marca_id) : null;
      const changed = (prevMarcaId || null) !== (nextMarcaId || null);
      return {
        ...prev,
        marca_id: nextMarcaId ? String(nextMarcaId) : "",
        modelo_id: changed ? "" : prev.modelo_id,
        variante: changed ? "" : prev.variante,
      };
    });
  };

  if (!deviceId) return null;

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-3xl p-4 max-h-[90vh] overflow-y-auto">
        <div className="text-lg font-semibold mb-2">Editar datos del equipo</div>
        <div className="text-sm text-gray-600 mb-3">Equipo #{deviceId}</div>
        {err && <div className="bg-red-100 text-red-800 border border-red-300 rounded p-2 mb-3">{err}</div>}
        {catalogErr && <div className="bg-amber-100 border border-amber-300 text-amber-900 rounded p-2 mb-3">{catalogErr}</div>}

        {loading ? (
          <div className="text-sm text-gray-500 py-4">Cargando equipo...</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <label className="block md:col-span-2">
              <div className="text-sm text-gray-700 mb-1">Institución / Cliente *</div>
              <select
                className="border rounded p-2 w-full"
                value={form.customer_id}
                onChange={(e) => update("customer_id", e.target.value)}
                disabled={!canEdit || saving}
              >
                <option value="">Seleccione una institución</option>
                {(customerOptions || []).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.razon_social} {c.cod_empresa ? `(${c.cod_empresa})` : ""}
                  </option>
                ))}
              </select>
            </label>

            <label className="block md:col-span-2">
              <div className="text-sm text-gray-700 mb-1">Tipo de equipo</div>
              <select
                className="border rounded p-2 w-full"
                value={form.tipo_equipo}
                onChange={(e) => {
                  const value = e.target.value || "";
                  setForm((prev) => ({
                    ...prev,
                    tipo_equipo: value,
                    marca_id: "",
                    modelo_id: "",
                    variante: "",
                  }));
                  setMarcaTxt("");
                  setMarcaId(null);
                }}
                disabled={!canEdit || saving}
              >
                <option value="">-- Seleccionar --</option>
                {(tiposEquipo || []).map((t, i) => (
                  <option key={`${t}-${i}`} value={t}>{t}</option>
                ))}
              </select>
            </label>

            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Marca</div>
              <input
                list={`edit-device-marcas-list-${deviceId}`}
                className="border rounded p-2 w-full"
                value={marcaTxt}
                onChange={(e) => onMarcaInput(e.target.value)}
                placeholder="Marca"
                disabled={!canEdit || saving}
              />
              <datalist id={`edit-device-marcas-list-${deviceId}`}>
                {(tipoSel && marcasPorTipo.length ? marcasPorTipo : marcas).map((m) => (
                  <option key={m.id} value={m.nombre} />
                ))}
              </datalist>
              {marcaTxt && !marcaId && (
                <div className="text-xs text-red-600 mt-1">Elija una marca de las sugeridas.</div>
              )}
            </label>

            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Modelo</div>
              <select
                className="border rounded p-2 w-full"
                value={form.modelo_id}
                onChange={(e) => update("modelo_id", e.target.value)}
                disabled={!canEdit || !marcaId || saving}
              >
                <option value="">{!marcaId ? "Seleccione marca primero" : "Seleccione modelo"}</option>
                {(modelos || []).map((m) => (
                  <option key={m.id} value={m.id}>{m.nombre}</option>
                ))}
              </select>
            </label>

            <label className="block md:col-span-2">
              <div className="text-sm text-gray-700 mb-1">Variante / detalle</div>
              <input
                list={`edit-device-variantes-list-${deviceId}`}
                className="border rounded p-2 w-full"
                value={form.variante}
                onChange={(e) => update("variante", e.target.value)}
                disabled={!canEdit || saving}
              />
              <datalist id={`edit-device-variantes-list-${deviceId}`}>
                {(varianteSugeridas || []).map((v, i) => (
                  <option key={`${v}-${i}`} value={v} />
                ))}
              </datalist>
            </label>

            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Número de serie</div>
              <input
                className="border rounded p-2 w-full"
                value={form.numero_serie}
                onChange={(e) => update("numero_serie", e.target.value)}
                disabled={!canEdit || saving}
              />
            </label>

            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Número interno (MG)</div>
              <input
                className="border rounded p-2 w-full"
                value={form.numero_interno}
                onChange={(e) => update("numero_interno", e.target.value)}
                disabled={!canEdit || saving}
              />
            </label>

            <label className="block md:col-span-2">
              <div className="text-sm text-gray-700 mb-1">Ubicación</div>
              <select
                className="border rounded p-2 w-full"
                value={form.ubicacion_id}
                onChange={(e) => update("ubicacion_id", e.target.value)}
                disabled={!canEdit || saving}
              >
                <option value="">Sin ubicación</option>
                {(ubicaciones || []).map((u) => (
                  <option key={u.id} value={u.id}>{u.nombre}</option>
                ))}
              </select>
            </label>

            <label className="block md:col-span-2">
              <span className="inline-flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={!!form.alquilado}
                  onChange={(e) => update("alquilado", e.target.checked)}
                  disabled={!canEdit || saving}
                />
                Equipo alquilado
              </span>
            </label>

            {form.alquilado && (
              <label className="block md:col-span-2">
                <div className="text-sm text-gray-700 mb-1">Alquilado a (cliente) *</div>
                <select
                  className="border rounded p-2 w-full"
                  value={form.alquiler_customer_id}
                  onChange={(e) => update("alquiler_customer_id", e.target.value)}
                  disabled={!canEdit || saving}
                >
                  <option value="">Selecciona cliente</option>
                  {(customerOptions || []).map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.razon_social} {c.cod_empresa ? `(${c.cod_empresa})` : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>
            Cancelar
          </button>
          {canEdit && (
            <button
              className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
              disabled={saving || loading}
              onClick={async () => {
                setErr("");
                const customerId = Number(form.customer_id || 0);
                if (!customerId) {
                  setErr("Debe seleccionar una institución.");
                  return;
                }
                if (marcaTxt && !marcaId) {
                  setErr("Debes elegir una marca válida de las sugerencias.");
                  return;
                }
                let alquilerA = "";
                if (form.alquilado) {
                  const alquilerCustomer = (customerOptions || []).find(
                    (c) => String(c.id) === String(form.alquiler_customer_id)
                  );
                  if (!alquilerCustomer?.razon_social) {
                    setErr("Debes seleccionar a qué cliente está alquilado el equipo.");
                    return;
                  }
                  alquilerA = String(alquilerCustomer.razon_social || "").trim();
                }

                try {
                  setSaving(true);
                  await patchDeviceEditable(deviceId, {
                    customer_id: customerId,
                    tipo_equipo: (form.tipo_equipo || "").trim(),
                    marca_id: form.marca_id ? Number(form.marca_id) : null,
                    model_id: form.modelo_id ? Number(form.modelo_id) : null,
                    variante: (form.variante || "").trim(),
                    numero_serie: (form.numero_serie || "").trim(),
                    numero_interno: (form.numero_interno || "").trim(),
                    ubicacion_id: form.ubicacion_id ? Number(form.ubicacion_id) : null,
                    alquilado: !!form.alquilado,
                    alquiler_a: alquilerA,
                  });
                  if (onSaved) await onSaved();
                  onClose();
                } catch (e) {
                  const ctype = e?.data?.conflict_type;
                  if (ctype === "NS_DUPLICATE") setErr("El número de serie ya está asignado a otro equipo.");
                  else if (ctype === "MG_DUPLICATE") setErr("El número interno ya está asignado a otro equipo.");
                  else setErr(e?.message || "No se pudo guardar.");
                } finally {
                  setSaving(false);
                }
              }}
            >
              {saving ? "Guardando..." : "Guardar"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function MgVentaModal({ row, mode = "venta", onClose, onSaved }) {
  const isVenta = mode === "venta";
  const [factura, setFactura] = useState("");
  const [remito, setRemito] = useState("");
  const [fechaVenta, setFechaVenta] = useState(todayISO());
  const [observaciones, setObservaciones] = useState("");
  const [ventaCustomerId, setVentaCustomerId] = useState("");
  const [ventaNumeroAlternativo, setVentaNumeroAlternativo] = useState("");
  const [clientes, setClientes] = useState([]);
  const [clientesLoading, setClientesLoading] = useState(false);
  const [addCustomerOpen, setAddCustomerOpen] = useState(false);
  const [addCustomerSaving, setAddCustomerSaving] = useState(false);
  const [addCustomerErr, setAddCustomerErr] = useState("");
  const [addCustomerForm, setAddCustomerForm] = useState({
    razon_social: "",
    cod_empresa: "",
    telefono: "",
    telefono_2: "",
    email: "",
  });
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  if (!row) return null;

  useEffect(() => {
    if (!row) return;
    const defaultFecha = row?.mg_venta_fecha ? String(row.mg_venta_fecha).slice(0, 10) : todayISO();
    setFactura(row?.mg_venta_factura_numero || "");
    setRemito(row?.mg_venta_remito_numero || "");
    setFechaVenta(defaultFecha);
    setObservaciones("");
    setVentaCustomerId(row?.mg_venta_customer_id ? String(row.mg_venta_customer_id) : "");
    setVentaNumeroAlternativo(row?.mg_venta_numero_alternativo || "");
    setErr("");
    setAddCustomerOpen(false);
    setAddCustomerErr("");
  }, [row?.id, row?.mg_venta_fecha, row?.mg_venta_factura_numero, row?.mg_venta_remito_numero, row?.mg_venta_customer_id, row?.mg_venta_numero_alternativo]);

  useEffect(() => {
    if (!isVenta) return;
    let cancelled = false;
    (async () => {
      try {
        setClientesLoading(true);
        const rows = await getClientes();
        if (cancelled) return;
        setClientes(Array.isArray(rows) ? rows : []);
      } catch {
        if (cancelled) return;
        setClientes([]);
      } finally {
        if (!cancelled) setClientesLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [isVenta, row?.id]);

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-lg p-4">
        <div className="text-lg font-semibold mb-2">
          {isVenta ? "Marcar MG vendido" : "Reactivar MG"}
        </div>
        <div className="text-sm text-gray-600 mb-3">
          Equipo #{row.id} - <DeviceIdentifier row={row} />
        </div>
        {err && <div className="bg-red-100 text-red-800 border border-red-300 rounded p-2 mb-3">{err}</div>}

        {isVenta ? (
          <div className="space-y-3">
            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Vendido a (cliente)</div>
              <div className="flex gap-2">
                <select
                  className="border rounded p-2 w-full"
                  value={ventaCustomerId}
                  onChange={(e) => setVentaCustomerId(e.target.value)}
                  disabled={saving || clientesLoading}
                >
                  <option value="">{clientesLoading ? "Cargando clientes..." : "Seleccionar cliente"}</option>
                  {(clientes || []).map((c) => (
                    <option key={c.id} value={String(c.id)}>
                      {c.razon_social}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="px-3 py-2 rounded border whitespace-nowrap hover:bg-gray-50 disabled:opacity-60"
                  onClick={() => {
                    setAddCustomerErr("");
                    setAddCustomerForm({
                      razon_social: "",
                      cod_empresa: "",
                      telefono: "",
                      telefono_2: "",
                      email: "",
                    });
                    setAddCustomerOpen((v) => !v);
                  }}
                  disabled={saving || clientesLoading}
                >
                  Alta rápida
                </button>
              </div>
            </label>
            {addCustomerOpen && (
              <div className="border rounded p-3 bg-gray-50">
                {addCustomerErr && (
                  <div className="bg-red-100 text-red-800 border border-red-300 rounded p-2 mb-2 text-sm">
                    {addCustomerErr}
                  </div>
                )}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <input
                    className="border rounded p-2 md:col-span-2"
                    placeholder="Razón social"
                    value={addCustomerForm.razon_social}
                    onChange={(e) => setAddCustomerForm((s) => ({ ...s, razon_social: e.target.value }))}
                    disabled={addCustomerSaving}
                  />
                  <input
                    className="border rounded p-2 md:col-span-2"
                    placeholder="Código de empresa"
                    value={addCustomerForm.cod_empresa}
                    onChange={(e) => setAddCustomerForm((s) => ({ ...s, cod_empresa: e.target.value }))}
                    disabled={addCustomerSaving}
                  />
                  <input
                    className="border rounded p-2"
                    placeholder="Teléfono"
                    value={addCustomerForm.telefono}
                    onChange={(e) => setAddCustomerForm((s) => ({ ...s, telefono: e.target.value }))}
                    disabled={addCustomerSaving}
                  />
                  <input
                    className="border rounded p-2"
                    placeholder="Teléfono 2"
                    value={addCustomerForm.telefono_2}
                    onChange={(e) => setAddCustomerForm((s) => ({ ...s, telefono_2: e.target.value }))}
                    disabled={addCustomerSaving}
                  />
                  <input
                    className="border rounded p-2 md:col-span-2"
                    placeholder="Email"
                    value={addCustomerForm.email}
                    onChange={(e) => setAddCustomerForm((s) => ({ ...s, email: e.target.value }))}
                    disabled={addCustomerSaving}
                  />
                </div>
                <div className="mt-2 flex justify-end gap-2">
                  <button
                    type="button"
                    className="px-3 py-1.5 rounded border"
                    onClick={() => setAddCustomerOpen(false)}
                    disabled={addCustomerSaving}
                  >
                    Cancelar
                  </button>
                  <button
                    type="button"
                    className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
                    disabled={
                      addCustomerSaving
                      || !String(addCustomerForm.razon_social || "").trim()
                      || !String(addCustomerForm.cod_empresa || "").trim()
                    }
                    onClick={async () => {
                      const razon_social = String(addCustomerForm.razon_social || "").trim();
                      const cod_empresa = String(addCustomerForm.cod_empresa || "").trim();
                      if (!razon_social || !cod_empresa) {
                        setAddCustomerErr("Razón social y código de empresa son obligatorios.");
                        return;
                      }
                      try {
                        setAddCustomerSaving(true);
                        setAddCustomerErr("");
                        await postCliente({
                          razon_social,
                          cod_empresa,
                          telefono: String(addCustomerForm.telefono || "").trim() || null,
                          telefono_2: String(addCustomerForm.telefono_2 || "").trim() || null,
                          email: String(addCustomerForm.email || "").trim() || null,
                        });
                        const rows = await getClientes();
                        const list = Array.isArray(rows) ? rows : [];
                        setClientes(list);
                        const match = list.find(
                          (c) => String(c?.razon_social || "").trim().toLowerCase() === razon_social.toLowerCase()
                        );
                        if (match?.id) {
                          setVentaCustomerId(String(match.id));
                        }
                        setAddCustomerOpen(false);
                      } catch (e) {
                        setAddCustomerErr(e?.message || "No se pudo crear el cliente.");
                      } finally {
                        setAddCustomerSaving(false);
                      }
                    }}
                  >
                    {addCustomerSaving ? "Guardando..." : "Crear cliente"}
                  </button>
                </div>
              </div>
            )}
            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Factura de venta</div>
              <input
                type="text"
                className="border rounded p-2 w-full"
                value={factura}
                onChange={(e) => setFactura(e.target.value)}
                disabled={saving}
              />
            </label>
            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Remito de venta</div>
              <input
                type="text"
                className="border rounded p-2 w-full"
                value={remito}
                onChange={(e) => setRemito(e.target.value)}
                disabled={saving}
              />
            </label>
            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Número alternativo</div>
              <input
                type="text"
                className="border rounded p-2 w-full"
                value={ventaNumeroAlternativo}
                onChange={(e) => setVentaNumeroAlternativo(e.target.value)}
                disabled={saving}
              />
            </label>
            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Fecha de venta</div>
              <input
                type="date"
                className="border rounded p-2 w-full"
                value={fechaVenta}
                onChange={(e) => setFechaVenta(e.target.value)}
                disabled={saving}
              />
            </label>
            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Observaciones</div>
              <textarea
                className="border rounded p-2 w-full min-h-[90px]"
                value={observaciones}
                onChange={(e) => setObservaciones(e.target.value)}
                disabled={saving}
              />
            </label>
            <div className="text-xs text-gray-500">Debes informar al menos factura o remito.</div>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="text-sm text-gray-700">
              Esta acción vuelve el MG a estado operativo y conserva trazabilidad en el historial de eventos.
            </div>
            <div className="text-sm text-gray-600">
              <div>Vendido a: {row?.mg_venta_customer_nombre || "-"}</div>
              <div>Número alternativo: {row?.mg_venta_numero_alternativo || "-"}</div>
            </div>
            <label className="block">
              <div className="text-sm text-gray-700 mb-1">Observaciones</div>
              <textarea
                className="border rounded p-2 w-full min-h-[90px]"
                value={observaciones}
                onChange={(e) => setObservaciones(e.target.value)}
                disabled={saving}
              />
            </label>
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>
            Cancelar
          </button>
          <button
            className={`px-3 py-1.5 rounded text-white disabled:opacity-50 ${isVenta ? "bg-amber-600 hover:bg-amber-700" : "bg-emerald-600 hover:bg-emerald-700"}`}
            disabled={saving || (isVenta && (!(factura.trim() || remito.trim()) || !Number(ventaCustomerId || 0)))}
            onClick={async () => {
              setErr("");
              try {
                setSaving(true);
                if (isVenta) {
                  await postDeviceMgVenta(row.id, {
                    factura_numero: factura.trim() || null,
                    remito_numero: remito.trim() || null,
                    fecha_venta: fechaVenta || null,
                    observaciones: observaciones.trim() || null,
                    venta_customer_id: Number(ventaCustomerId || 0),
                    venta_numero_alternativo: ventaNumeroAlternativo.trim() || null,
                    source: "equipos",
                  });
                } else {
                  await postDeviceMgReactivar(row.id, {
                    observaciones: observaciones.trim() || null,
                    source: "equipos",
                  });
                }
                onSaved && onSaved();
                onClose();
              } catch (e) {
                setErr(e?.message || "No se pudo guardar el estado MG.");
              } finally {
                setSaving(false);
              }
            }}
          >
            {saving ? "Guardando..." : isVenta ? "Marcar vendido" : "Reactivar MG"}
          </button>
        </div>
      </div>
    </div>
  );
}

function PreventivoPlanModal({ title, initialPlan, onClose, onSubmit, saving = false, error = "" }) {
  const [form, setForm] = useState({
    periodicidad_valor: "",
    periodicidad_unidad: "meses",
    aviso_anticipacion_dias: "30",
    ultima_revision_fecha: "",
    proxima_revision_fecha: "",
    activa: true,
    observaciones: "",
  });

  useEffect(() => {
    setForm({
      periodicidad_valor: initialPlan?.periodicidad_valor != null ? String(initialPlan.periodicidad_valor) : "",
      periodicidad_unidad: initialPlan?.periodicidad_unidad || "meses",
      aviso_anticipacion_dias:
        initialPlan?.aviso_anticipacion_dias != null
          ? String(initialPlan.aviso_anticipacion_dias)
          : "30",
      ultima_revision_fecha: initialPlan?.ultima_revision_fecha || "",
      proxima_revision_fecha: initialPlan?.proxima_revision_fecha || "",
      activa: initialPlan?.activa == null ? true : !!initialPlan.activa,
      observaciones: initialPlan?.observaciones || "",
    });
  }, [initialPlan]);

  const update = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-xl p-4">
        <div className="text-lg font-semibold mb-2">{title}</div>
        {error && <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3">{error}</div>}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Periodicidad valor</div>
            <input
              type="number"
              min="1"
              className="border rounded p-2 w-full"
              value={form.periodicidad_valor}
              onChange={(e) => update("periodicidad_valor", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Unidad</div>
            <select
              className="border rounded p-2 w-full"
              value={form.periodicidad_unidad}
              onChange={(e) => update("periodicidad_unidad", e.target.value)}
              disabled={saving}
            >
              {PERIODICIDAD_UNIDADES.map((u) => (
                <option key={u.value} value={u.value}>{u.label}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Aviso (días)</div>
            <input
              type="number"
              min="0"
              className="border rounded p-2 w-full"
              value={form.aviso_anticipacion_dias}
              onChange={(e) => update("aviso_anticipacion_dias", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Última revisión</div>
            <input
              type="date"
              className="border rounded p-2 w-full"
              value={form.ultima_revision_fecha}
              onChange={(e) => update("ultima_revision_fecha", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Próxima revisión</div>
            <input
              type="date"
              className="border rounded p-2 w-full"
              value={form.proxima_revision_fecha}
              onChange={(e) => update("proxima_revision_fecha", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block md:flex md:items-end">
            <span className="inline-flex items-center gap-2 text-sm mt-7 md:mt-0">
              <input
                type="checkbox"
                checked={!!form.activa}
                onChange={(e) => update("activa", e.target.checked)}
                disabled={saving}
              />
              Plan activo
            </span>
          </label>
        </div>
        <label className="block mt-3">
          <div className="text-sm text-gray-700 mb-1">Observaciones</div>
          <textarea
            className="border rounded p-2 w-full min-h-20"
            value={form.observaciones}
            onChange={(e) => update("observaciones", e.target.value)}
            disabled={saving}
          />
        </label>
        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>Cancelar</button>
          <button
            className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            onClick={() => onSubmit({
              periodicidad_valor: Number(form.periodicidad_valor || 0),
              periodicidad_unidad: form.periodicidad_unidad,
              aviso_anticipacion_dias: Number(form.aviso_anticipacion_dias || 0),
              ultima_revision_fecha: form.ultima_revision_fecha || null,
              proxima_revision_fecha: form.proxima_revision_fecha || null,
              activa: !!form.activa,
              observaciones: form.observaciones || "",
            })}
            disabled={saving}
          >
            Guardar
          </button>
        </div>
      </div>
    </div>
  );
}

function DeviceRevisionModal({ row, onClose, onSubmit, saving = false, error = "" }) {
  const [form, setForm] = useState({
    fecha_realizada: todayISO(),
    estado_item: "ok",
    motivo_no_control: "",
    ubicacion_detalle: "",
    accesorios_cambiados: false,
    accesorios_detalle: "",
    notas: "",
    arrastrar_proxima: true,
    resumen: "",
  });

  useEffect(() => {
    setForm({
      fecha_realizada: todayISO(),
      estado_item: "ok",
      motivo_no_control: "",
      ubicacion_detalle: "",
      accesorios_cambiados: false,
      accesorios_detalle: "",
      notas: "",
      arrastrar_proxima: true,
      resumen: "",
    });
  }, [row?.id]);

  const update = (key, value) => {
    setForm((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "estado_item" && value === "retirado") next.arrastrar_proxima = false;
      return next;
    });
  };

  if (!row) return null;
  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-xl p-4">
        <div className="text-lg font-semibold mb-2">Registrar revisión de equipo</div>
        <div className="text-sm text-gray-600 mb-3">
          Equipo #{row.id} - {row.marca || "-"} {row.modelo || ""}
        </div>
        {error && <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3">{error}</div>}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Fecha realizada</div>
            <input type="date" className="border rounded p-2 w-full" value={form.fecha_realizada} onChange={(e) => update("fecha_realizada", e.target.value)} />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Estado</div>
            <select className="border rounded p-2 w-full" value={form.estado_item} onChange={(e) => update("estado_item", e.target.value)}>
              {ITEM_STATES.filter((s) => s.value !== "pendiente").map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </label>
          {form.estado_item === "no_controlado" && (
            <label className="block md:col-span-2">
              <div className="text-sm text-gray-700 mb-1">Motivo no control</div>
              <input
                type="text"
                className="border rounded p-2 w-full"
                value={form.motivo_no_control}
                onChange={(e) => update("motivo_no_control", e.target.value)}
              />
            </label>
          )}
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Ubicación</div>
            <input type="text" className="border rounded p-2 w-full" value={form.ubicacion_detalle} onChange={(e) => update("ubicacion_detalle", e.target.value)} />
          </label>
          <label className="block md:flex md:items-end">
            <span className="inline-flex items-center gap-2 text-sm mt-7 md:mt-0">
              <input type="checkbox" checked={!!form.accesorios_cambiados} onChange={(e) => update("accesorios_cambiados", e.target.checked)} />
              Accesorios cambiados
            </span>
          </label>
          {form.accesorios_cambiados && (
            <label className="block md:col-span-2">
              <div className="text-sm text-gray-700 mb-1">Detalle accesorios</div>
              <input
                type="text"
                className="border rounded p-2 w-full"
                value={form.accesorios_detalle}
                onChange={(e) => update("accesorios_detalle", e.target.value)}
              />
            </label>
          )}
          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Resumen</div>
            <input type="text" className="border rounded p-2 w-full" value={form.resumen} onChange={(e) => update("resumen", e.target.value)} />
          </label>
          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Notas</div>
            <textarea className="border rounded p-2 w-full min-h-16" value={form.notas} onChange={(e) => update("notas", e.target.value)} />
          </label>
          <label className="block md:col-span-2">
            <span className="inline-flex items-center gap-2 text-sm">
              <input type="checkbox" checked={!!form.arrastrar_proxima} onChange={(e) => update("arrastrar_proxima", e.target.checked)} />
              Arrastrar a próxima revisión
            </span>
          </label>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>Cancelar</button>
          <button
            className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            onClick={() => onSubmit(form)}
            disabled={saving}
          >
            Guardar revisión
          </button>
        </div>
      </div>
    </div>
  );
}

function PreventivoRepuestoModal({
  title,
  initialItem = null,
  catalogOptions = [],
  onClose,
  onSubmit,
  saving = false,
  error = "",
}) {
  const [form, setForm] = useState({
    catalogo_repuesto_id: initialItem?.catalogo_repuesto_id ? String(initialItem.catalogo_repuesto_id) : "",
    nombre_repuesto: initialItem?.nombre_repuesto || "",
    periodicidad_valor: initialItem?.periodicidad_valor != null ? String(initialItem.periodicidad_valor) : "1",
    periodicidad_unidad: initialItem?.periodicidad_unidad || "meses",
    aviso_anticipacion_dias: initialItem?.aviso_anticipacion_dias != null ? String(initialItem.aviso_anticipacion_dias) : "30",
    ultima_revision_fecha: initialItem?.ultima_revision_fecha || "",
    proxima_revision_fecha: initialItem?.proxima_revision_fecha || "",
  });

  useEffect(() => {
    setForm({
      catalogo_repuesto_id: initialItem?.catalogo_repuesto_id ? String(initialItem.catalogo_repuesto_id) : "",
      nombre_repuesto: initialItem?.nombre_repuesto || "",
      periodicidad_valor: initialItem?.periodicidad_valor != null ? String(initialItem.periodicidad_valor) : "1",
      periodicidad_unidad: initialItem?.periodicidad_unidad || "meses",
      aviso_anticipacion_dias: initialItem?.aviso_anticipacion_dias != null ? String(initialItem.aviso_anticipacion_dias) : "30",
      ultima_revision_fecha: initialItem?.ultima_revision_fecha || "",
      proxima_revision_fecha: initialItem?.proxima_revision_fecha || "",
    });
  }, [initialItem]);

  const setField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const selectedCatalog = catalogOptions.find((it) => String(it.id) === String(form.catalogo_repuesto_id));

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-2xl p-4">
        <div className="text-lg font-semibold mb-2">{title}</div>
        {error && <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3">{error}</div>}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Repuesto de catálogo (opcional)</div>
            <select
              className="border rounded p-2 w-full"
              value={form.catalogo_repuesto_id}
              onChange={(e) => {
                const nextId = e.target.value;
                const opt = catalogOptions.find((it) => String(it.id) === String(nextId));
                setForm((prev) => ({
                  ...prev,
                  catalogo_repuesto_id: nextId,
                  nombre_repuesto: prev.nombre_repuesto || (opt?.nombre || ""),
                }));
              }}
              disabled={saving || !!initialItem?.id}
            >
              <option value="">Texto libre</option>
              {catalogOptions.map((opt) => (
                <option key={opt.id} value={opt.id}>
                  {opt.codigo ? `${opt.codigo} - ` : ""}{opt.nombre}
                </option>
              ))}
            </select>
          </label>
          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Nombre del repuesto</div>
            <input
              type="text"
              className="border rounded p-2 w-full"
              value={form.nombre_repuesto}
              onChange={(e) => setField("nombre_repuesto", e.target.value)}
              placeholder={selectedCatalog?.nombre || "Ej: filtros, cooler, batería"}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Periodicidad valor</div>
            <input
              type="number"
              min="1"
              className="border rounded p-2 w-full"
              value={form.periodicidad_valor}
              onChange={(e) => setField("periodicidad_valor", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Unidad</div>
            <select
              className="border rounded p-2 w-full"
              value={form.periodicidad_unidad}
              onChange={(e) => setField("periodicidad_unidad", e.target.value)}
              disabled={saving}
            >
              {PERIODICIDAD_UNIDADES.map((u) => (
                <option key={u.value} value={u.value}>{u.label}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Aviso (días)</div>
            <input
              type="number"
              min="0"
              className="border rounded p-2 w-full"
              value={form.aviso_anticipacion_dias}
              onChange={(e) => setField("aviso_anticipacion_dias", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Última vez</div>
            <input
              type="date"
              className="border rounded p-2 w-full"
              value={form.ultima_revision_fecha}
              onChange={(e) => setField("ultima_revision_fecha", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Próxima</div>
            <input
              type="date"
              className="border rounded p-2 w-full"
              value={form.proxima_revision_fecha}
              onChange={(e) => setField("proxima_revision_fecha", e.target.value)}
              disabled={saving}
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>Cancelar</button>
          <button
            className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            disabled={saving}
            onClick={() =>
              onSubmit({
                catalogo_repuesto_id: form.catalogo_repuesto_id ? Number(form.catalogo_repuesto_id) : null,
                nombre_repuesto: form.nombre_repuesto || "",
                periodicidad_valor: Number(form.periodicidad_valor || 0),
                periodicidad_unidad: form.periodicidad_unidad,
                aviso_anticipacion_dias: Number(form.aviso_anticipacion_dias || 0),
                ultima_revision_fecha: form.ultima_revision_fecha || null,
                proxima_revision_fecha: form.proxima_revision_fecha || null,
              })
            }
          >
            Guardar repuesto
          </button>
        </div>
      </div>
    </div>
  );
}

function DeviceRepuestosRevisionModal({
  row,
  repuestos = [],
  onClose,
  onSubmit,
  saving = false,
  loading = false,
  error = "",
}) {
  const [fechaRealizada, setFechaRealizada] = useState(todayISO());
  const [resumen, setResumen] = useState("");
  const [selected, setSelected] = useState({});
  const [resetRecuento, setResetRecuento] = useState({});

  useEffect(() => {
    const next = {};
    (repuestos || []).forEach((it) => {
      if (it?.id != null) next[it.id] = true;
    });
    setSelected(next);
    setResetRecuento({});
    setFechaRealizada(todayISO());
    setResumen("");
  }, [row?.id, repuestos]);

  const selectedIds = Object.entries(selected)
    .filter(([, checked]) => !!checked)
    .map(([id]) => Number(id))
    .filter((id) => Number.isFinite(id) && id > 0);
  const resetRecuentoIds = Object.entries(resetRecuento)
    .filter(([id, checked]) => !!checked && !!selected[id])
    .map(([id]) => Number(id))
    .filter((id) => Number.isFinite(id) && id > 0);

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-3xl p-4">
        <div className="text-lg font-semibold mb-2">Registrar revisión por repuestos</div>
        <div className="text-sm text-gray-600 mb-3">
          Equipo #{row?.id} - {row?.marca || "-"} {row?.modelo || ""}
        </div>
        {error && <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3">{error}</div>}
        {loading ? (
          <div className="text-sm text-gray-500 py-3">Cargando repuestos...</div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
              <label className="block">
                <div className="text-sm text-gray-700 mb-1">Fecha realizada</div>
                <input type="date" className="border rounded p-2 w-full" value={fechaRealizada} onChange={(e) => setFechaRealizada(e.target.value)} />
              </label>
              <label className="block">
                <div className="text-sm text-gray-700 mb-1">Resumen</div>
                <input type="text" className="border rounded p-2 w-full" value={resumen} onChange={(e) => setResumen(e.target.value)} />
              </label>
            </div>
            <div className="border rounded max-h-72 overflow-auto mb-3">
              <div className="bg-amber-50 border-b border-amber-200 text-amber-900 text-sm p-3">
                Marcá "Reiniciar conteo" solo cuando también se reseteó el contador u horómetro del equipo.
                Si solo registraste el cambio del repuesto, el trabajo queda en el historial pero no se mueve el próximo vencimiento.
              </div>
              {(repuestos || []).length === 0 ? (
                <div className="text-sm text-gray-500 p-3">Este plan no tiene repuestos.</div>
              ) : (
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="text-left">
                      <th className="p-2 w-10"></th>
                      <th className="p-2">Repuesto</th>
                      <th className="p-2">Última</th>
                      <th className="p-2">Próxima</th>
                      <th className="p-2">Estado</th>
                      <th className="p-2">Reiniciar conteo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {repuestos.map((it) => (
                      <tr key={it.id} className="border-t">
                        <td className="p-2 text-center">
                          <input
                            type="checkbox"
                            checked={!!selected[it.id]}
                            onChange={(e) => {
                              const checked = e.target.checked;
                              setSelected((prev) => ({ ...prev, [it.id]: checked }));
                              if (!checked) {
                                setResetRecuento((prev) => ({ ...prev, [it.id]: false }));
                              }
                            }}
                          />
                        </td>
                        <td className="p-2">{it.nombre_repuesto || "-"}</td>
                        <td className="p-2">{fmtDate(it.ultima_revision_fecha)}</td>
                        <td className="p-2">{fmtDate(it.proxima_revision_fecha)}</td>
                        <td className="p-2"><PreventivoBadge estado={it.preventivo_estado} dias={it.preventivo_dias_restantes} /></td>
                        <td className="p-2">
                          <label className="inline-flex items-center gap-2 text-xs text-gray-700">
                            <input
                              type="checkbox"
                              checked={!!resetRecuento[it.id] && !!selected[it.id]}
                              disabled={!selected[it.id]}
                              onChange={(e) => setResetRecuento((prev) => ({ ...prev, [it.id]: e.target.checked }))}
                            />
                            Contador reseteado
                          </label>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
        <div className="flex justify-end gap-2">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>Cancelar</button>
          <button
            className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            disabled={saving || loading || selectedIds.length === 0}
            onClick={() =>
              onSubmit({
                fecha_realizada: fechaRealizada || null,
                resumen: resumen || "",
                repuesto_ids: selectedIds,
                reset_recuento_repuesto_ids: resetRecuentoIds,
              })
            }
          >
            Guardar revisión
          </button>
        </div>
      </div>
    </div>
  );
}

function AddInstitutionModal({ onClose, onSubmit, saving = false, error = "" }) {
  const [form, setForm] = useState({
    razon_social: "",
    cod_empresa: "",
    telefono: "",
    telefono_2: "",
    email: "",
  });

  const update = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-xl p-4">
        <div className="text-lg font-semibold mb-2">Agregar institución</div>
        {error && <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3">{error}</div>}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Razón social *</div>
            <input className="border rounded p-2 w-full" value={form.razon_social} onChange={(e) => update("razon_social", e.target.value)} />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Código de empresa *</div>
            <input className="border rounded p-2 w-full" value={form.cod_empresa} onChange={(e) => update("cod_empresa", e.target.value)} />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Teléfono</div>
            <input className="border rounded p-2 w-full" value={form.telefono} onChange={(e) => update("telefono", e.target.value)} />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Teléfono 2</div>
            <input className="border rounded p-2 w-full" value={form.telefono_2} onChange={(e) => update("telefono_2", e.target.value)} />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Email</div>
            <input className="border rounded p-2 w-full" value={form.email} onChange={(e) => update("email", e.target.value)} />
          </label>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>Cancelar</button>
          <button
            className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            onClick={() => onSubmit(form)}
            disabled={saving}
          >
            Crear institución
          </button>
        </div>
      </div>
    </div>
  );
}

function AddPreventivoEquipoModal({ onClose, onSelect, onCreateManaged }) {
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const runSearch = async () => {
    const term = (search || "").trim();
    if (!term) {
      setErr("Ingresa un criterio para buscar (N/S, MG, cliente, marca o modelo).");
      setRows([]);
      return;
    }
    try {
      setLoading(true);
      setErr("");
      const res = await getDevices({
        q: term,
        page: 1,
        page_size: 30,
        sort: "-id",
      });
      const items = Array.isArray(res) ? res : (res.items || []);
      setRows(items);
      if (!items.length) {
        setErr("No se encontraron equipos en la lista actual.");
      }
    } catch (e) {
      setErr(e?.message || "No se pudieron buscar equipos.");
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-5xl p-4">
        <div className="text-lg font-semibold mb-2">Agregar equipo a preventivos</div>
        <div className="text-sm text-gray-600 mb-3">
          Selecciona un equipo existente para configurarle plan preventivo.
        </div>

        {err && <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3">{err}</div>}

        <div className="flex flex-wrap items-center gap-2 mb-3">
          <input
            type="text"
            className="border rounded p-2 w-full max-w-xl"
            placeholder="Buscar por N/S, MG, cliente, marca, modelo..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") runSearch(); }}
          />
          <button className="btn" onClick={runSearch} disabled={loading}>
            Buscar
          </button>
          <button className="px-3 py-1.5 rounded border hover:bg-gray-50" onClick={onCreateManaged}>
            Crear equipo sin ingreso
          </button>
        </div>

        <div className="border rounded overflow-x-auto max-h-[50vh]">
          {loading ? (
            <div className="text-sm text-gray-500 p-3">Buscando...</div>
          ) : rows.length === 0 ? (
            <div className="text-sm text-gray-500 p-3">Sin resultados.</div>
          ) : (
            <table className="min-w-full text-sm">
              <thead>
                <tr className="text-left bg-gray-50">
                  <th className="p-2">ID</th>
                  <th className="p-2">Cliente</th>
                  <th className="p-2">Identificación</th>
                  <th className="p-2">Marca</th>
                  <th className="p-2">Modelo</th>
                  <th className="p-2">Acción</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="border-t">
                    <td className="p-2 font-mono text-xs">{row.id}</td>
                    <td className="p-2">
                      <div className="font-medium">{deviceCustomerTitle(row)}</div>
                      {deviceCustomerSubtitle(row) ? (
                        <div className="text-xs text-gray-500">{deviceCustomerSubtitle(row)}</div>
                      ) : null}
                    </td>
                    <td className="p-2"><DeviceIdentifier row={row} /></td>
                    <td className="p-2">{row.marca || "-"}</td>
                    <td className="p-2">
                      <div className="font-medium">{deviceModelTitle(row)}</div>
                      {tipoEquipoOf(row, "") ? (
                        <div className="text-xs text-gray-500">{tipoEquipoOf(row, "")}</div>
                      ) : null}
                    </td>
                    <td className="p-2">
                      <button
                        className="px-2 py-1 rounded border text-xs hover:bg-gray-50"
                        onClick={() => onSelect(row)}
                      >
                        Configurar plan
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose}>
            Cerrar
          </button>
        </div>
      </div>
    </div>
  );
}

function AddManagedDeviceModal({ onClose, onSubmit, customers = [], saving = false, error = "" }) {
  const [form, setForm] = useState({
    customer_id: "",
    tipo_equipo: "",
    marca_id: "",
    modelo_id: "",
    variante: "",
    numero_serie: "",
    numero_interno: "",
    alquilado: false,
    alquiler_customer_id: "",
    alquiler_a: "",
  });
  const [tiposEquipo, setTiposEquipo] = useState([]);
  const [marcas, setMarcas] = useState([]);
  const [marcasPorTipo, setMarcasPorTipo] = useState([]);
  const [modelos, setModelos] = useState([]);
  const [varianteSugeridas, setVarianteSugeridas] = useState([]);
  const [marcaTxt, setMarcaTxt] = useState("");
  const [marcaId, setMarcaId] = useState(null);
  const [catTipoId, setCatTipoId] = useState(null);
  const [catModelos, setCatModelos] = useState([]);
  const [catalogErr, setCatalogErr] = useState("");

  const update = (key, value) =>
    setForm((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "alquilado" && !value) {
        next.alquiler_customer_id = "";
        next.alquiler_a = "";
      }
      if (key === "alquiler_customer_id") {
        const selected = (customers || []).find((c) => String(c.customer_id) === String(value));
        next.alquiler_a = selected?.razon_social || "";
      }
      return next;
    });
  const tipoSel = (form.tipo_equipo || "").trim();

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        setCatalogErr("");
        const [marcasRows, tiposRows] = await Promise.all([getMarcas(), getTiposEquipo()]);
        if (!active) return;
        setMarcas(Array.isArray(marcasRows) ? marcasRows : []);
        const tipoList = (Array.isArray(tiposRows) ? tiposRows : [])
          .map((t) => t?.nombre || t?.label || t?.name || t?.value || t)
          .map(String)
          .map((s) => s.trim())
          .filter(Boolean);
        setTiposEquipo(Array.from(new Set(tipoList)));
      } catch (e) {
        if (!active) return;
        setCatalogErr(e?.message || "No se pudieron cargar catálogos de equipo.");
        setMarcas([]);
        setTiposEquipo([]);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setMarcaTxt("");
    setMarcaId(null);
    setModelos([]);
    setCatTipoId(null);
    setCatModelos([]);
    setVarianteSugeridas([]);
    setForm((prev) => ({
      ...prev,
      marca_id: "",
      modelo_id: "",
      variante: "",
    }));
    if (!tipoSel) {
      setMarcasPorTipo([]);
      return () => {
        active = false;
      };
    }
    (async () => {
      try {
        const rows = await getMarcasPorTipo(tipoSel);
        if (!active) return;
        setMarcasPorTipo(Array.isArray(rows) ? rows : []);
      } catch {
        if (!active) return;
        setMarcasPorTipo([]);
      }
    })();
    return () => {
      active = false;
    };
  }, [tipoSel]);

  useEffect(() => {
    let active = true;
    setForm((prev) => ({
      ...prev,
      marca_id: marcaId ? String(marcaId) : "",
      modelo_id: "",
      variante: "",
    }));
    setModelos([]);
    setCatTipoId(null);
    setCatModelos([]);
    setVarianteSugeridas([]);
    if (!marcaId) {
      return () => {
        active = false;
      };
    }
    (async () => {
      try {
        const rows = await getModelosByBrand(marcaId);
        if (!active) return;
        const list = Array.isArray(rows) ? rows : [];
        const norm = (s) => (s || "").toString().trim().toUpperCase();
        const filtered = tipoSel ? list.filter((m) => norm(m?.tipo_equipo) === norm(tipoSel)) : list;
        setModelos(filtered);

        const tiposBrand = await getCatalogTipos(marcaId);
        if (!active) return;
        const match = (Array.isArray(tiposBrand) ? tiposBrand : []).find(
          (t) => (t?.name || "").trim().toUpperCase() === (tipoSel || "").trim().toUpperCase()
        );
        const tId = match?.id ?? null;
        setCatTipoId(tId);
        if (tId) {
          const mods = await getCatalogModelos(marcaId, tId);
          if (!active) return;
          setCatModelos(Array.isArray(mods) ? mods : []);
        } else {
          setCatModelos([]);
        }
      } catch (e) {
        if (!active) return;
        setCatalogErr(e?.message || "No se pudieron cargar modelos.");
        setModelos([]);
        setCatTipoId(null);
        setCatModelos([]);
        setVarianteSugeridas([]);
      }
    })();
    return () => {
      active = false;
    };
  }, [marcaId, tipoSel]);

  useEffect(() => {
    let active = true;
    const selectedModel = (modelos || []).find((x) => String(x.id) === String(form.modelo_id));
    if (!selectedModel || !marcaId || !catTipoId) {
      setVarianteSugeridas([]);
      return () => {
        active = false;
      };
    }

    const needle = (selectedModel?.nombre || "").trim().toUpperCase();
    const cmatch = (catModelos || []).filter((cm) => {
      const a = (cm?.name || "").trim().toUpperCase();
      const alias = (cm?.alias || "").trim().toUpperCase();
      return (
        a === needle ||
        a.includes(needle) ||
        needle.includes(a) ||
        (alias && (alias === needle || needle.includes(alias) || alias.includes(needle)))
      );
    });
    if (cmatch.length !== 1) {
      setVarianteSugeridas([]);
      return () => {
        active = false;
      };
    }

    (async () => {
      try {
        const vars = await getCatalogVariantes(marcaId, catTipoId, cmatch[0].id);
        if (!active) return;
        const names = (Array.isArray(vars) ? vars : []).filter((v) => v?.name).map((v) => v.name);
        setVarianteSugeridas(names);
        if (!String(form.variante || "").trim() && names.length === 1) {
          setForm((prev) => ({ ...prev, variante: names[0] }));
        }
      } catch {
        if (!active) return;
        setVarianteSugeridas([]);
      }
    })();
    return () => {
      active = false;
    };
  }, [form.modelo_id, form.variante, modelos, marcaId, catTipoId, catModelos]);

  const onMarcaInput = (value) => {
    setMarcaTxt(value);
    const pool = tipoSel ? (marcasPorTipo.length ? marcasPorTipo : marcas) : marcas;
    const match = (pool || []).find(
      (m) => (m?.nombre || "").toLowerCase() === String(value || "").trim().toLowerCase()
    );
    setMarcaId(match ? Number(match.id) : null);
  };

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded shadow-lg w-full max-w-xl p-4">
        <div className="text-lg font-semibold mb-2">Agregar equipo al sistema</div>
        <div className="text-sm text-gray-600 mb-3">
          Esta alta registra el equipo en inventario sin generar ingreso ni remito.
        </div>
        {error && <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3">{error}</div>}
        {catalogErr && <div className="bg-amber-100 border border-amber-300 text-amber-900 rounded p-2 mb-3">{catalogErr}</div>}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Institución / Cliente *</div>
            <select
              className="border rounded p-2 w-full"
              value={form.customer_id}
              onChange={(e) => update("customer_id", e.target.value)}
              disabled={saving}
            >
              <option value="">Seleccione una institución</option>
              {customers.map((c) => (
                <option key={c.customer_id} value={c.customer_id}>
                  {c.razon_social} {c.cod_empresa ? `(${c.cod_empresa})` : ""}
                </option>
              ))}
            </select>
          </label>

          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Tipo de equipo</div>
            <select
              className="border rounded p-2 w-full"
              value={form.tipo_equipo}
              onChange={(e) => update("tipo_equipo", e.target.value || "")}
              disabled={saving}
            >
              <option value="">-- Seleccionar --</option>
              {(tiposEquipo || []).map((t, i) => (
                <option key={`${t}-${i}`} value={t}>{t}</option>
              ))}
            </select>
          </label>

          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Marca *</div>
            <input
              list="managed-device-marcas-list"
              className="border rounded p-2 w-full"
              value={marcaTxt}
              onChange={(e) => onMarcaInput(e.target.value)}
              placeholder="Marca"
              disabled={saving}
            />
            <datalist id="managed-device-marcas-list">
              {(tipoSel && marcasPorTipo.length ? marcasPorTipo : marcas).map((m) => (
                <option key={m.id} value={m.nombre} />
              ))}
            </datalist>
            {marcaTxt && !marcaId && (
              <div className="text-xs text-red-600 mt-1">Elija una marca de las sugeridas.</div>
            )}
          </label>

          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Modelo *</div>
            <select
              className="border rounded p-2 w-full"
              value={form.modelo_id}
              onChange={(e) => update("modelo_id", e.target.value)}
              disabled={!marcaId || !modelos.length || saving}
            >
              <option value="">{!marcaId ? "Seleccione marca primero" : "Seleccione modelo"}</option>
              {modelos.map((m) => (
                <option key={m.id} value={m.id}>{m.nombre}</option>
              ))}
            </select>
          </label>

          <label className="block md:col-span-2">
            <div className="text-sm text-gray-700 mb-1">Variante / detalle</div>
            <input
              list="managed-device-variantes-list"
              className="border rounded p-2 w-full"
              value={form.variante}
              onChange={(e) => update("variante", e.target.value)}
              disabled={saving}
            />
            <datalist id="managed-device-variantes-list">
              {(varianteSugeridas || []).map((v, i) => (
                <option key={`${v}-${i}`} value={v} />
              ))}
            </datalist>
          </label>

          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Número de serie</div>
            <input
              className="border rounded p-2 w-full"
              value={form.numero_serie}
              onChange={(e) => update("numero_serie", e.target.value)}
              disabled={saving}
            />
          </label>
          <label className="block">
            <div className="text-sm text-gray-700 mb-1">Número interno (MG)</div>
            <input
              className="border rounded p-2 w-full"
              value={form.numero_interno}
              onChange={(e) => update("numero_interno", e.target.value)}
              disabled={saving}
            />
          </label>

          <label className="block md:col-span-2">
            <span className="inline-flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={!!form.alquilado}
                onChange={(e) => update("alquilado", e.target.checked)}
                disabled={saving}
              />
              Equipo alquilado
            </span>
          </label>
          {form.alquilado && (
            <label className="block md:col-span-2">
              <div className="text-sm text-gray-700 mb-1">Alquilado a (cliente) *</div>
              <select
                className="border rounded p-2 w-full"
                value={form.alquiler_customer_id}
                onChange={(e) => update("alquiler_customer_id", e.target.value)}
                disabled={saving}
              >
                <option value="">Selecciona cliente</option>
                {customers.map((c) => (
                  <option key={c.customer_id} value={c.customer_id}>
                    {c.razon_social} {c.cod_empresa ? `(${c.cod_empresa})` : ""}
                  </option>
                ))}
              </select>
              {!customers.length && (
                <div className="text-xs text-amber-700 mt-1">
                  No hay clientes cargados para seleccionar.
                </div>
              )}
            </label>
          )}
        </div>

        <div className="flex justify-end gap-2 mt-4">
          <button className="px-3 py-1.5 rounded border" onClick={onClose} disabled={saving}>
            Cancelar
          </button>
          <button
            className="px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            onClick={() => onSubmit(form)}
            disabled={saving}
          >
            Guardar equipo
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Equipos() {
  const { user } = useAuth();
  const canManageDevices = can(user, PERMISSION_CODES.ACTION_DEVICES_PREVENTIVOS_MANAGE);
  const canEdit = canManageDevices;
  const canPlanEdit = canManageDevices;
  const canRevisionMutate = canManageDevices;
  const nav = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const tabParam = (searchParams.get("tab") || "equipos").toLowerCase();
  const initialTab = tabParam === "mantenimientos-preventivos" ? "preventivos" : tabParam;
  const [activeTab, setActiveTab] = useState(TAB_ITEMS.some((t) => t.value === initialTab) ? initialTab : "equipos");

  const updateSearchParam = (key, value) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value == null || value === "") next.delete(key);
      else next.set(key, String(value));
      return next;
    }, { replace: true });
  };

  useEffect(() => {
    updateSearchParam("tab", activeTab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const highlightId = searchParams.get("device_id");

  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [err, setErr] = useState("");
  const [page, setPage] = useState(1);
  const [hasNext, setHasNext] = useState(false);
  const [q, setQ] = useState(searchParams.get("q") || "");
  const [qDebounced, setQDebounced] = useState(searchParams.get("q") || "");
  const [editDeviceId, setEditDeviceId] = useState(null);
  const [mgModalRow, setMgModalRow] = useState(null);
  const [mgModalMode, setMgModalMode] = useState("venta");
  const [reloadDevicesKey, setReloadDevicesKey] = useState(0);
  const [sort, setSort] = useState("-id");

  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeEquipo1, setMergeEquipo1] = useState(null);
  const [mergeEquipo2, setMergeEquipo2] = useState(null);
  const [mergeStep, setMergeStep] = useState(1);
  const [mergeSearch, setMergeSearch] = useState("");
  const [mergeSearchResults, setMergeSearchResults] = useState([]);
  const [mergeSearching, setMergeSearching] = useState(false);
  const [mergeSearchErr, setMergeSearchErr] = useState("");
  const [mergeNsChoice, setMergeNsChoice] = useState("equipo1");
  const [mergeMgChoice, setMergeMgChoice] = useState("equipo1");
  const [mergeErr, setMergeErr] = useState("");
  const [mergeSaving, setMergeSaving] = useState(false);

  const [planModalCtx, setPlanModalCtx] = useState(null);
  const [planSaving, setPlanSaving] = useState(false);
  const [planErr, setPlanErr] = useState("");

  const [deviceRevisionCtx, setDeviceRevisionCtx] = useState(null);
  const [deviceRevisionSaving, setDeviceRevisionSaving] = useState(false);
  const [deviceRevisionErr, setDeviceRevisionErr] = useState("");

  const [agendaLoading, setAgendaLoading] = useState(false);
  const [agendaErr, setAgendaErr] = useState("");
  const [agendaItems, setAgendaItems] = useState([]);
  const [agendaExpanded, setAgendaExpanded] = useState({});
  const [agendaRepuestosByDevice, setAgendaRepuestosByDevice] = useState({});
  const [agendaCounts, setAgendaCounts] = useState({ total: 0, vencido: 0, proximo: 0, sin_plan: 0, al_dia: 0 });
  const [agendaEstado, setAgendaEstado] = useState("");
  const [agendaQ, setAgendaQ] = useState("");
  const [agendaCustomerId, setAgendaCustomerId] = useState("");
  const [repuestosCatalogOptions, setRepuestosCatalogOptions] = useState([]);
  const [repuestoModalCtx, setRepuestoModalCtx] = useState(null);
  const [repuestoModalSaving, setRepuestoModalSaving] = useState(false);
  const [repuestoModalErr, setRepuestoModalErr] = useState("");
  const [repuestoDeleteBusyId, setRepuestoDeleteBusyId] = useState(null);
  const [repuestoRevisionCtx, setRepuestoRevisionCtx] = useState(null);
  const [repuestoRevisionSaving, setRepuestoRevisionSaving] = useState(false);
  const [repuestoRevisionErr, setRepuestoRevisionErr] = useState("");

  const [institucionesLoading, setInstitucionesLoading] = useState(false);
  const [institucionesErr, setInstitucionesErr] = useState("");
  const [instituciones, setInstituciones] = useState([]);
  const [selectedInstitucionId, setSelectedInstitucionId] = useState(searchParams.get("institucion_id") || "");
  const [instRevisionesLoading, setInstRevisionesLoading] = useState(false);
  const [instRevisionesErr, setInstRevisionesErr] = useState("");
  const [instRevisiones, setInstRevisiones] = useState([]);
  const [instPlan, setInstPlan] = useState(null);

  const [addInstitutionOpen, setAddInstitutionOpen] = useState(false);
  const [addInstitutionSaving, setAddInstitutionSaving] = useState(false);
  const [addInstitutionErr, setAddInstitutionErr] = useState("");
  const [addPreventivoEquipoOpen, setAddPreventivoEquipoOpen] = useState(false);
  const [addManagedDeviceOpen, setAddManagedDeviceOpen] = useState(false);
  const [addManagedDeviceSaving, setAddManagedDeviceSaving] = useState(false);
  const [addManagedDeviceErr, setAddManagedDeviceErr] = useState("");
  const [addManagedDeviceContext, setAddManagedDeviceContext] = useState("general");

  const [revisionOpenId, setRevisionOpenId] = useState(null);
  const [revisionLoading, setRevisionLoading] = useState(false);
  const [revisionErr, setRevisionErr] = useState("");
  const [revisionData, setRevisionData] = useState(null);
  const [savingItemId, setSavingItemId] = useState(null);
  const [newItemName, setNewItemName] = useState("");
  const [closeRevisionForm, setCloseRevisionForm] = useState({ fecha_realizada: todayISO(), resumen: "" });
  const [closingRevision, setClosingRevision] = useState(false);

  const pageSize = 100;
  const autoEditDeviceIdRef = useRef("");

  useEffect(() => {
    const explicitEditId = searchParams.get("edit_device_id") || "";
    const rawEditId = explicitEditId || (searchParams.get("from") === "service" ? searchParams.get("device_id") : "");
    if (!rawEditId) return;
    const nextEditId = Number(rawEditId);
    if (!Number.isFinite(nextEditId) || nextEditId <= 0) return;
    const autoEditKey = `${explicitEditId ? "edit" : "service"}:${nextEditId}`;
    if (autoEditDeviceIdRef.current === autoEditKey) return;
    autoEditDeviceIdRef.current = autoEditKey;
    setActiveTab("equipos");
    if (canEdit) setEditDeviceId(nextEditId);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set("tab", "equipos");
      next.set("device_id", String(nextEditId));
      next.delete("edit_device_id");
      return next;
    }, { replace: true });
  }, [canEdit, searchParams, setSearchParams]);

  useEffect(() => {
    const timer = setTimeout(() => setQDebounced(q), 300);
    return () => clearTimeout(timer);
  }, [q]);

  const selectedInstitucion = useMemo(
    () => instituciones.find((it) => String(it.customer_id) === String(selectedInstitucionId)) || null,
    [instituciones, selectedInstitucionId]
  );

  const mergedInstPlan = selectedInstitucion?.plan || instPlan || null;

  const sortedInstituciones = useMemo(
    () => [...instituciones].sort((a, b) => String(a?.razon_social || "").localeCompare(String(b?.razon_social || ""))),
    [instituciones]
  );

  const institucionesConPreventivo = useMemo(
    () => sortedInstituciones.filter((it) => Boolean(it?.preventivo_plan_id || it?.plan?.id)),
    [sortedInstituciones]
  );

  const resetMergeState = () => {
    setMergeOpen(false);
    setMergeEquipo1(null);
    setMergeEquipo2(null);
    setMergeStep(1);
    setMergeSearch("");
    setMergeSearchResults([]);
    setMergeSearching(false);
    setMergeSearchErr("");
    setMergeNsChoice("equipo1");
    setMergeMgChoice("equipo1");
    setMergeErr("");
    setMergeSaving(false);
  };

  const openMergeFor = (row) => {
    setMergeEquipo1(row);
    setMergeEquipo2(null);
    setMergeStep(1);
    setMergeSearch("");
    setMergeSearchResults([]);
    setMergeSearching(false);
    setMergeSearchErr("");
    setMergeNsChoice("equipo1");
    setMergeMgChoice("equipo1");
    setMergeErr("");
    setMergeSaving(false);
    setMergeOpen(true);
  };

  const selectMergeEquipo2 = (row) => {
    setMergeEquipo2(row);
    const nsDefault = mergeEquipo1?.numero_serie ? "equipo1" : (row?.numero_serie ? "equipo2" : "equipo1");
    const mgDefault = mergeEquipo1?.numero_interno ? "equipo1" : (row?.numero_interno ? "equipo2" : "equipo1");
    setMergeNsChoice(nsDefault);
    setMergeMgChoice(mgDefault);
    setMergeStep(2);
    setMergeErr("");
  };

  const mergeNsFinal = mergeNsChoice === "equipo1" ? (mergeEquipo1?.numero_serie || "") : (mergeEquipo2?.numero_serie || "");
  const mergeMgFinal = mergeMgChoice === "equipo1" ? (mergeEquipo1?.numero_interno || "") : (mergeEquipo2?.numero_interno || "");
  const mergeNsFinalValue = (mergeNsFinal || "").trim();
  const mergeMgFinalValue = (mergeMgFinal || "").trim();
  const canSubmitMerge = !!mergeEquipo1 && !!mergeEquipo2 && !!(mergeNsFinalValue || mergeMgFinalValue) && !mergeSaving;

  async function loadDevices(p = 1, { reset = false } = {}) {
    try {
      if (reset) {
        setRows([]);
        setPage(1);
        setHasNext(false);
      }
      const isFirst = reset || p === 1;
      isFirst ? setLoading(true) : setLoadingMore(true);
      setErr("");
      const qEffective = (qDebounced || "").trim();
      const query = {
        page: p,
        page_size: pageSize,
        q: qEffective || undefined,
        propio: searchParams.get("propio") || undefined,
        alquilado: searchParams.get("alquilado") || undefined,
        sort: sort || undefined,
      };
      const res = await getDevices(query);
      const items = Array.isArray(res) ? res : (res.items || []);
      const next = Array.isArray(res) ? false : !!res.has_next;

      setRows((prev) => (isFirst ? items : [...prev, ...items]));
      setHasNext(next);
      setPage(p);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los equipos");
      if (reset) setRows([]);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }

  async function loadAgenda() {
    try {
      setAgendaLoading(true);
      setAgendaErr("");
      const params = {
        scope: "device",
        only_with_plan: 1,
        estado: agendaEstado || undefined,
        customer_id: agendaCustomerId || undefined,
        q: agendaQ || undefined,
      };
      const res = await getPreventivosAgenda(params);
      const items = (res?.items || []).filter((it) => (it?.scope_type || "device") === "device");
      const counts = { total: items.length, vencido: 0, proximo: 0, sin_plan: 0, al_dia: 0 };
      items.forEach((it) => {
        const estado = it?.preventivo_estado;
        if (estado === "vencido") counts.vencido += 1;
        else if (estado === "proximo") counts.proximo += 1;
        else if (estado === "sin_plan") counts.sin_plan += 1;
        else if (estado === "al_dia") counts.al_dia += 1;
      });
      setAgendaItems(items);
      setAgendaCounts(counts);
    } catch (e) {
      setAgendaErr(e?.message || "No se pudo cargar la agenda de preventivos.");
      setAgendaItems([]);
      setAgendaCounts({ total: 0, vencido: 0, proximo: 0, sin_plan: 0, al_dia: 0 });
    } finally {
      setAgendaLoading(false);
    }
  }

  async function loadRepuestosCatalogOptions(force = false) {
    if (!force && repuestosCatalogOptions.length > 0) return repuestosCatalogOptions;
    try {
      const res = await getRepuestosCatalogo({ limit: 500 });
      const rows = Array.isArray(res) ? res : [];
      setRepuestosCatalogOptions(rows);
      return rows;
    } catch (_) {
      return [];
    }
  }

  async function loadAgendaRepuestos(deviceId, { force = false } = {}) {
    if (!deviceId) return [];
    const key = String(deviceId);
    if (!force && Array.isArray(agendaRepuestosByDevice[key]?.items)) {
      return agendaRepuestosByDevice[key].items || [];
    }
    setAgendaRepuestosByDevice((prev) => ({
      ...prev,
      [key]: { loading: true, err: "", items: prev[key]?.items || [] },
    }));
    try {
      const res = await getDevicePreventivoRepuestos(deviceId);
      const items = Array.isArray(res?.items) ? res.items : [];
      setAgendaRepuestosByDevice((prev) => ({
        ...prev,
        [key]: { loading: false, err: "", items },
      }));
      return items;
    } catch (e) {
      setAgendaRepuestosByDevice((prev) => ({
        ...prev,
        [key]: { loading: false, err: e?.message || "No se pudieron cargar los repuestos.", items: [] },
      }));
      return [];
    }
  }

  async function toggleAgendaExpand(item) {
    const did = item?.device_id;
    if (!did) return;
    const key = String(did);
    const nextOpen = !agendaExpanded[key];
    setAgendaExpanded((prev) => ({ ...prev, [key]: nextOpen }));
    if (nextOpen) {
      await loadRepuestosCatalogOptions();
      await loadAgendaRepuestos(did);
    }
  }

  const openCreateRepuestoModal = async (item) => {
    if (!item?.device_id) return;
    setRepuestoModalErr("");
    await loadRepuestosCatalogOptions();
    setRepuestoModalCtx({ mode: "create", deviceId: item.device_id, item: null });
  };

  const openEditRepuestoModal = async (deviceId, repuesto) => {
    if (!deviceId || !repuesto?.id) return;
    setRepuestoModalErr("");
    await loadRepuestosCatalogOptions();
    setRepuestoModalCtx({ mode: "edit", deviceId, item: repuesto });
  };

  const saveRepuestoModal = async (payload) => {
    if (!repuestoModalCtx?.deviceId) return;
    try {
      setRepuestoModalSaving(true);
      setRepuestoModalErr("");
      if (repuestoModalCtx.mode === "edit" && repuestoModalCtx.item?.id) {
        await patchDevicePreventivoRepuesto(repuestoModalCtx.deviceId, repuestoModalCtx.item.id, payload);
      } else {
        await postDevicePreventivoRepuesto(repuestoModalCtx.deviceId, payload);
      }
      await loadAgendaRepuestos(repuestoModalCtx.deviceId, { force: true });
      await loadAgenda();
      setRepuestoModalCtx(null);
    } catch (e) {
      setRepuestoModalErr(e?.message || "No se pudo guardar el repuesto preventivo.");
    } finally {
      setRepuestoModalSaving(false);
    }
  };

  const deleteAgendaRepuesto = async (deviceId, repuesto) => {
    if (!deviceId || !repuesto?.id) return;
    if (!confirm(`Eliminar "${repuesto.nombre_repuesto || "repuesto"}" de este mantenimiento y equivalentes?`)) return;
    try {
      setRepuestoDeleteBusyId(repuesto.id);
      await deleteDevicePreventivoRepuesto(deviceId, repuesto.id);
      await loadAgendaRepuestos(deviceId, { force: true });
      await loadAgenda();
    } catch (e) {
      setAgendaErr(e?.message || "No se pudo eliminar el repuesto preventivo.");
    } finally {
      setRepuestoDeleteBusyId(null);
    }
  };

  const startAgendaDeviceRevision = async (item) => {
    const row =
      rows.find((r) => String(r.id) === String(item.device_id)) || {
        id: item.device_id,
        marca: item.marca,
        modelo: item.modelo,
        preventivo_plan_id: item.plan_id,
      };
    if (Number(item?.repuestos_total || 0) <= 0) {
      setDeviceRevisionCtx(row);
      return;
    }
    setRepuestoRevisionErr("");
    setRepuestoRevisionCtx({ row, repuestos: [], loading: true });
    const items = await loadAgendaRepuestos(item.device_id, { force: true });
    setRepuestoRevisionCtx({ row, repuestos: items, loading: false });
  };

  async function loadInstituciones() {
    try {
      setInstitucionesLoading(true);
      setInstitucionesErr("");
      const res = await getPreventivosClientes();
      setInstituciones(res?.items || []);
    } catch (e) {
      setInstitucionesErr(e?.message || "No se pudieron cargar las instituciones.");
      setInstituciones([]);
    } finally {
      setInstitucionesLoading(false);
    }
  }

  async function loadInstitucionRevisiones(customerId) {
    if (!customerId) {
      setInstPlan(null);
      setInstRevisiones([]);
      return;
    }
    try {
      setInstRevisionesLoading(true);
      setInstRevisionesErr("");
      const res = await getCustomerPreventivoRevisiones(customerId);
      setInstPlan(res?.plan || null);
      setInstRevisiones(res?.items || []);
    } catch (e) {
      setInstRevisionesErr(e?.message || "No se pudo cargar el historial de revisiones.");
      setInstPlan(null);
      setInstRevisiones([]);
    } finally {
      setInstRevisionesLoading(false);
    }
  }

  async function openRevision(revisionId) {
    if (!revisionId) return;
    try {
      setRevisionOpenId(revisionId);
      setRevisionLoading(true);
      setRevisionErr("");
      const res = await getPreventivoRevision(revisionId);
      setRevisionData({ revision: res?.revision || null, items: res?.items || [] });
      setCloseRevisionForm({
        fecha_realizada: res?.revision?.fecha_realizada || todayISO(),
        resumen: res?.revision?.resumen || "",
      });
    } catch (e) {
      setRevisionErr(e?.message || "No se pudo cargar la revisión.");
      setRevisionData(null);
    } finally {
      setRevisionLoading(false);
    }
  }

  async function startOrContinueInstitutionRevision(customerId) {
    if (!customerId) return;
    try {
      const current = instituciones.find((it) => String(it.customer_id) === String(customerId));
      const draftFromList = current?.borrador_revision_id;
      const draftFromHistory = (instRevisiones || []).find((it) => it.estado === "borrador")?.id;
      const draftId = draftFromList || draftFromHistory;
      if (draftId) {
        await openRevision(draftId);
        return;
      }
      const res = await postCustomerPreventivoRevision(customerId, {});
      const revId = res?.revision?.id;
      await loadInstituciones();
      await loadInstitucionRevisiones(customerId);
      if (revId) await openRevision(revId);
    } catch (e) {
      setInstRevisionesErr(e?.message || "No se pudo iniciar la revisión institucional.");
    }
  }

  const sentinelRef = useRef(null);
  useEffect(() => {
    if (!hasNext || activeTab !== "equipos") return;
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && !loadingMore) loadDevices(page + 1);
      }
    });
    io.observe(el);
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasNext, page, loadingMore, activeTab]);

  useEffect(() => {
    if (activeTab === "equipos") loadDevices(1, { reset: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, reloadDevicesKey, sort, qDebounced, searchParams.get("propio"), searchParams.get("alquilado")]);

  useEffect(() => {
    if (activeTab !== "preventivos") return;
    const timer = setTimeout(() => {
      loadAgenda();
    }, 250);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, agendaEstado, agendaCustomerId, agendaQ]);

  useEffect(() => {
    if (activeTab === "instituciones" || activeTab === "preventivos") loadInstituciones();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  useEffect(() => {
    if (!addManagedDeviceOpen) return;
    if (sortedInstituciones.length > 0 || institucionesLoading) return;
    loadInstituciones();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addManagedDeviceOpen, sortedInstituciones.length, institucionesLoading]);

  useEffect(() => {
    updateSearchParam("institucion_id", selectedInstitucionId || "");
    if (!selectedInstitucionId) {
      setInstPlan(null);
      setInstRevisiones([]);
      return;
    }
    if (activeTab === "instituciones") loadInstitucionRevisiones(selectedInstitucionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedInstitucionId, activeTab]);

  useEffect(() => {
    if (activeTab !== "instituciones" || !selectedInstitucionId) return;
    const exists = institucionesConPreventivo.some((it) => String(it.customer_id) === String(selectedInstitucionId));
    if (!exists) setSelectedInstitucionId("");
  }, [activeTab, institucionesConPreventivo, selectedInstitucionId]);

  const openDevicePlanModal = (row) => {
    setPlanErr("");
    setPlanModalCtx({
      scope: "device",
      id: row.id,
      title: `Configurar preventivo - Equipo #${row.id}`,
      isEdit: !!row.preventivo_plan_id,
      initialPlan: row.preventivo_plan_id
        ? {
            periodicidad_valor: row.preventivo_periodicidad_valor,
            periodicidad_unidad: row.preventivo_periodicidad_unidad,
            aviso_anticipacion_dias: row.preventivo_aviso_dias,
            ultima_revision_fecha: row.preventivo_ultima_revision,
            proxima_revision_fecha: row.preventivo_proxima_revision,
            activa: true,
            observaciones: "",
          }
        : null,
    });
  };

  const openCustomerPlanModal = (inst) => {
    setPlanErr("");
    const plan = inst?.plan || null;
    setPlanModalCtx({
      scope: "customer",
      id: inst?.customer_id,
      title: `Plan institucional - ${inst?.razon_social || "Institución"}`,
      isEdit: !!plan?.id,
      initialPlan: plan,
    });
  };

  const savePlan = async (payload) => {
    if (!planModalCtx) return;
    try {
      setPlanSaving(true);
      setPlanErr("");
      if (planModalCtx.scope === "device") {
        if (planModalCtx.isEdit) await patchDevicePreventivoPlan(planModalCtx.id, payload);
        else await postDevicePreventivoPlan(planModalCtx.id, payload);
        setReloadDevicesKey(Date.now());
      } else {
        if (planModalCtx.isEdit) await patchCustomerPreventivoPlan(planModalCtx.id, payload);
        else await postCustomerPreventivoPlan(planModalCtx.id, payload);
        await loadInstituciones();
        if (selectedInstitucionId) await loadInstitucionRevisiones(selectedInstitucionId);
      }
      setPlanModalCtx(null);
      if (activeTab === "preventivos") loadAgenda();
    } catch (e) {
      setPlanErr(e?.message || "No se pudo guardar el plan preventivo.");
    } finally {
      setPlanSaving(false);
    }
  };

  const saveDeviceRevision = async (form) => {
    if (!deviceRevisionCtx) return;
    try {
      setDeviceRevisionSaving(true);
      setDeviceRevisionErr("");
      await postDevicePreventivoRevision(deviceRevisionCtx.id, form);
      setDeviceRevisionCtx(null);
      setReloadDevicesKey(Date.now());
      if (activeTab === "preventivos") loadAgenda();
    } catch (e) {
      setDeviceRevisionErr(e?.message || "No se pudo registrar la revisión.");
    } finally {
      setDeviceRevisionSaving(false);
    }
  };

  const saveDeviceRepuestoRevision = async (form) => {
    if (!repuestoRevisionCtx?.row?.id) return;
    try {
      setRepuestoRevisionSaving(true);
      setRepuestoRevisionErr("");
      await postDevicePreventivoRevision(repuestoRevisionCtx.row.id, form);
      setRepuestoRevisionCtx(null);
      setReloadDevicesKey(Date.now());
      if (activeTab === "preventivos") {
        await loadAgendaRepuestos(repuestoRevisionCtx.row.id, { force: true });
        await loadAgenda();
      }
    } catch (e) {
      setRepuestoRevisionErr(e?.message || "No se pudo registrar la revisión por repuestos.");
    } finally {
      setRepuestoRevisionSaving(false);
    }
  };

  const onSelectExistingPreventivoDevice = (row) => {
    setAddPreventivoEquipoOpen(false);
    openDevicePlanModal(row);
  };

  const onCreateManagedDevice = async (form) => {
    if (!form?.customer_id) {
      setAddManagedDeviceErr("Debe seleccionar una institución.");
      return;
    }
    if (!form?.marca_id) {
      setAddManagedDeviceErr("Debes seleccionar una marca valida.");
      return;
    }
    if (!form?.modelo_id) {
      setAddManagedDeviceErr("Debes seleccionar un modelo.");
      return;
    }
    if (!(form?.numero_serie || "").trim() && !(form?.numero_interno || "").trim()) {
      setAddManagedDeviceErr("Debe cargar número de serie o número interno.");
      return;
    }
    let alquilerA = "";
    if (form?.alquilado) {
      if (!form?.alquiler_customer_id) {
        setAddManagedDeviceErr("Debes seleccionar a que cliente esta alquilado el equipo.");
        return;
      }
      const alquilerCustomer = sortedInstituciones.find(
        (c) => String(c.customer_id) === String(form.alquiler_customer_id)
      );
      if (!alquilerCustomer?.razon_social) {
        setAddManagedDeviceErr("El cliente seleccionado para alquiler no es válido.");
        return;
      }
      alquilerA = String(alquilerCustomer.razon_social || "").trim();
    }
    try {
      setAddManagedDeviceSaving(true);
      setAddManagedDeviceErr("");
      const res = await postDeviceDirectCreate({
        customer_id: Number(form.customer_id),
        tipo_equipo: (form.tipo_equipo || "").trim(),
        marca_id: Number(form.marca_id),
        model_id: Number(form.modelo_id),
        variante: (form.variante || "").trim(),
        numero_serie: (form.numero_serie || "").trim(),
        numero_interno: (form.numero_interno || "").trim(),
        alquilado: !!form.alquilado,
        alquiler_a: alquilerA,
      });
      const created = res?.device || null;
      setAddManagedDeviceOpen(false);
      setAddPreventivoEquipoOpen(false);
      await loadDevices(1, { reset: true });
      await loadAgenda();
      if (created?.id && addManagedDeviceContext === "preventivos") {
        openDevicePlanModal(created);
      } else if (created?.id) {
        updateSearchParam("device_id", created.id);
        setActiveTab("equipos");
      }
      setAddManagedDeviceContext("general");
    } catch (e) {
      setAddManagedDeviceErr(e?.message || "No se pudo crear el equipo sin ingreso.");
    } finally {
      setAddManagedDeviceSaving(false);
    }
  };

  const runMergeSearch = async () => {
    const term = (mergeSearch || "").trim();
    if (!term) {
      setMergeSearchResults([]);
      setMergeSearchErr("Ingrese un N/S o número interno para buscar.");
      return;
    }
    try {
      setMergeSearching(true);
      setMergeSearchErr("");
      const res = await getDevices({ q: term, page: 1, page_size: 20, sort: "id" });
      const items = Array.isArray(res) ? res : (res.items || []);
      const filtered = items.filter((item) => item.id !== mergeEquipo1?.id);
      setMergeSearchResults(filtered);
      if (!filtered.length) setMergeSearchErr("No hay resultados para esa búsqueda.");
    } catch (e) {
      setMergeSearchErr(e?.message || "No se pudo buscar el equipo.");
      setMergeSearchResults([]);
    } finally {
      setMergeSearching(false);
    }
  };

  const selectedInstState = selectedInstitucion?.preventivo_estado || mergedInstPlan?.preventivo_estado || "sin_plan";
  const selectedInstDraftId = selectedInstitucion?.borrador_revision_id || (instRevisiones || []).find((r) => r.estado === "borrador")?.id;

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="h1">Equipos</div>
          <p className="text-sm text-gray-600">
            Gestión de equipos, mantenimientos preventivos e instituciones.
          </p>
        </div>
        {canEdit && (
          <button
            className="btn"
            onClick={() => {
              setAddManagedDeviceErr("");
              setAddManagedDeviceContext("general");
              setAddManagedDeviceOpen(true);
            }}
          >
            Agregar equipo
          </button>
        )}
      </div>

      <Tabs value={activeTab} onChange={setActiveTab} items={TAB_ITEMS} />

      {activeTab === "equipos" && (
        <>
          {err && <div className="bg-red-100 border border-red-300 text-red-800 p-2 rounded mb-3">{err}</div>}

          <div className="flex flex-wrap items-center gap-2 mb-3">
            <input
              type="text"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Buscar por N/S, MG, cliente, marca, modelo..."
              className="border rounded p-2 w-full max-w-md"
            />
          </div>

          {loading ? (
            "Cargando..."
          ) : rows.length === 0 ? (
            <div className="text-sm text-gray-500">No hay resultados.</div>
          ) : (
            <div className="overflow-x-auto overflow-y-visible">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="text-left">
                    <SortableTh label="ID" field="id" sort={sort} setSort={setSort} />
                    <th className="p-2">Propiedad</th>
                    <SortableTh label="Último cliente/Dueño" field="cliente" sort={sort} setSort={setSort} />
                    <SortableTh label="Identificación" field="ns" sort={sort} setSort={setSort} />
                    <SortableTh label="Marca" field="marca" sort={sort} setSort={setSort} />
                    <SortableTh label="Modelo" field="modelo" sort={sort} setSort={setSort} />
                    <SortableTh label="Ubicación" field="ubicacion" sort={sort} setSort={setSort} />
                    <th className="p-2">Alquiler</th>
                    <th className="p-2">Acciones</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const isHighlight = highlightId && String(highlightId) === String(row.id);
                    return (
                      <tr key={row.id} className={`hover:bg-gray-50 ${isHighlight ? "bg-amber-50" : ""}`}>
                        <td className="p-2 font-mono text-xs">{row.id}</td>
                        <td className="p-2"><PropiedadBadge row={row} /></td>
                        <td className="p-2">
                          <div className="font-medium">{deviceCustomerTitle(row)}</div>
                          {deviceCustomerSubtitle(row) ? (
                            <div className="text-xs text-gray-500">{deviceCustomerSubtitle(row)}</div>
                          ) : null}
                          {deviceOwnerMeta(row) ? <div className="text-xs text-gray-500">{deviceOwnerMeta(row)}</div> : null}
                          {row.last_ingreso_id ? <div className="text-xs text-gray-500">Último ingreso #{row.last_ingreso_id}</div> : null}
                          {row.es_propietario_mg && !row.mg_inactivo_venta && <div className="text-xs text-gray-500">Dueño base (propio MG/BIO)</div>}
                        </td>
                        <td className="p-2"><DeviceIdentifier row={row} /></td>
                        <td className="p-2">{row.marca || "-"}</td>
                        <td className="p-2">
                          <div className="font-medium">{deviceModelTitle(row)}</div>
                          {tipoEquipoOf(row, "") ? (
                            <div className="text-xs text-gray-500">{tipoEquipoOf(row, "")}</div>
                          ) : null}
                        </td>
                        <td className="p-2">{row.ubicacion_nombre || "-"}</td>
                        <td className="p-2">
                          {row.alquilado ? (
                            <div>
                              <div className="text-xs text-gray-700">Alquilado</div>
                              <div className="text-xs text-gray-500">{row.alquiler_a || ""}</div>
                            </div>
                          ) : (
                            <span className="text-xs text-gray-500">No</span>
                          )}
                        </td>
                        <td className="p-2">
                          <div className="inline-block text-left">
                            <Menu
                              button={({ toggle, buttonRef }) => (
                                <button ref={buttonRef} onClick={toggle} className="px-2 py-1 rounded hover:bg-gray-100" aria-label="Acciones">
                                  &#8942;
                                </button>
                              )}
                            >
                              {({ close }) => (
                                <div className="w-40 bg-white border border-gray-200 rounded shadow z-10">
                                  <button
                                    className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                                    onClick={() => {
                                      close();
                                      if (row.last_ingreso_id) nav(`/ingresos/${row.last_ingreso_id}`);
                                    }}
                                  >
                                    Ver ingreso
                                  </button>
                                  {canEdit && (
                                    <button
                                      className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                                      onClick={() => {
                                        close();
                                        setEditDeviceId(Number(row?.id || 0) || null);
                                      }}
                                    >
                                      Editar datos
                                    </button>
                                  )}
                                  {canEdit && row?.es_propietario_mg && row?.numero_interno && (
                                    <button
                                      className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                                      onClick={() => {
                                        close();
                                        setMgModalMode(row?.mg_inactivo_venta ? "reactivar" : "venta");
                                        setMgModalRow(row);
                                      }}
                                    >
                                      {row?.mg_inactivo_venta ? "Reactivar MG" : "Marcar vendido"}
                                    </button>
                                  )}
                                  {canEdit && (
                                    <button
                                      className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                                      onClick={() => {
                                        close();
                                        openMergeFor(row);
                                      }}
                                    >
                                      Unificar
                                    </button>
                                  )}
                                </div>
                              )}
                            </Menu>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div className="text-xs text-gray-500 mt-2">
                Mostrando {rows.length} {hasNext ? "(hay más, desplaza para cargar...)" : ""}
              </div>
              <div ref={sentinelRef} style={{ height: 1 }} />
              {loadingMore && <div className="text-xs text-gray-500 mt-2">Cargando más...</div>}
            </div>
          )}
        </>
      )}

      {activeTab === "preventivos" && (
        <>
          {agendaErr && <div className="bg-red-100 border border-red-300 text-red-800 p-2 rounded mb-3">{agendaErr}</div>}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
            <div className="border rounded p-3 bg-red-50"><div className="text-xs text-gray-600">Vencidos</div><div className="text-xl font-semibold text-red-700">{agendaCounts.vencido || 0}</div></div>
            <div className="border rounded p-3 bg-amber-50"><div className="text-xs text-gray-600">Próximos</div><div className="text-xl font-semibold text-amber-700">{agendaCounts.proximo || 0}</div></div>
            <div className="border rounded p-3 bg-slate-50"><div className="text-xs text-gray-600">Sin plan</div><div className="text-xl font-semibold text-slate-700">{agendaCounts.sin_plan || 0}</div></div>
            <div className="border rounded p-3 bg-emerald-50"><div className="text-xs text-gray-600">Total</div><div className="text-xl font-semibold text-emerald-700">{agendaCounts.total || 0}</div></div>
          </div>

          <div className="flex flex-wrap items-center gap-2 mb-3">
            <select className="border rounded p-2" value={agendaEstado} onChange={(e) => setAgendaEstado(e.target.value)}>
              <option value="">Estado: todos</option>
              <option value="vencido">Vencido</option>
              <option value="proximo">Próximo</option>
              <option value="sin_plan">Sin plan</option>
              <option value="al_dia">Al día</option>
            </select>
            <select className="border rounded p-2" value={agendaCustomerId} onChange={(e) => setAgendaCustomerId(e.target.value)}>
              <option value="">Institución: todas</option>
              {sortedInstituciones.map((inst) => (
                <option key={inst.customer_id} value={inst.customer_id}>{inst.razon_social}</option>
              ))}
            </select>
            <input
              type="text"
              className="border rounded p-2 w-full max-w-md"
              value={agendaQ}
              onChange={(e) => setAgendaQ(e.target.value)}
              placeholder="Buscar por cliente, código, marca, modelo, N/S"
            />
            {canPlanEdit && (
              <button
                className="btn"
                onClick={() => {
                  setAddManagedDeviceErr("");
                  setAddPreventivoEquipoOpen(true);
                }}
              >
                Sumar preventivo
              </button>
            )}
          </div>

          {agendaLoading ? (
            "Cargando agenda..."
          ) : agendaItems.length === 0 ? (
            <div className="text-sm text-gray-500">Sin resultados.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="text-left">
                    <th className="p-2">Cliente</th>
                    <th className="p-2">Equipo</th>
                    <th className="p-2">Identificación</th>
                    <th className="p-2">Ultima revision</th>
                    <th className="p-2">Próxima</th>
                    <th className="p-2">Estado</th>
                    <th className="p-2">Acción</th>
                  </tr>
                </thead>
                <tbody>
                  {agendaItems.map((item, idx) => {
                    const rowKey = `${item.plan_id || "sp"}-${item.device_id || idx}-${idx}`;
                    const did = String(item.device_id || "");
                    const expanded = !!agendaExpanded[did];
                    const repState = agendaRepuestosByDevice[did] || { loading: false, err: "", items: [] };
                    return (
                      <Fragment key={rowKey}>
                        <tr
                          className={`border-t ${item.plan_id ? "cursor-pointer hover:bg-gray-50" : ""}`}
                          onClick={() => {
                            if (!item.plan_id) return;
                            toggleAgendaExpand(item);
                          }}
                        >
                          <td className="p-2">{item.customer_nombre || "-"}</td>
                          <td className="p-2">
                            <div>{item.equipo_label || `${item.marca || ""} ${item.modelo || ""}`.trim() || "-"}</div>
                            {item.plan_id ? (
                              <div className="text-xs text-gray-500">
                                {expanded ? "v" : ">"} Repuestos: {Number(item.repuestos_total || 0)} {item.repuesto_proximo_nombre ? `| Próximo: ${item.repuesto_proximo_nombre}` : ""}
                              </div>
                            ) : null}
                          </td>
                          <td className="p-2"><DeviceIdentifier row={item} /></td>
                          <td className="p-2">{fmtDate(item.ultima_revision_fecha)}</td>
                          <td className="p-2">{fmtDate(item.proxima_revision_fecha)}</td>
                          <td className="p-2"><PreventivoBadge estado={item.preventivo_estado} dias={item.preventivo_dias_restantes} /></td>
                          <td className="p-2">
                            {item.plan_id ? (
                              <div className="flex items-center gap-1">
                                <button
                                  className="px-2 py-1 rounded border text-xs hover:bg-gray-50"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    startAgendaDeviceRevision(item);
                                  }}
                                >
                                  Registrar revisión
                                </button>
                                {canEdit && !!item.device_id && (
                                  <button
                                    className="px-2 py-1 rounded border text-xs hover:bg-gray-50"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setEditDeviceId(Number(item.device_id));
                                    }}
                                  >
                                    Editar equipo
                                  </button>
                                )}
                              </div>
                            ) : (
                              <div className="flex items-center gap-1">
                                {canPlanEdit && (
                                  <button
                                    className="px-2 py-1 rounded border text-xs hover:bg-gray-50"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      openDevicePlanModal({
                                        id: item.device_id,
                                        marca: item.marca,
                                        modelo: item.modelo,
                                        preventivo_plan_id: null,
                                      });
                                    }}
                                  >
                                    Configurar plan
                                  </button>
                                )}
                                <button
                                  className="px-2 py-1 rounded border text-xs hover:bg-gray-50"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setActiveTab("equipos");
                                  }}
                                >
                                  Ir a equipo
                                </button>
                                {canEdit && !!item.device_id && (
                                  <button
                                    className="px-2 py-1 rounded border text-xs hover:bg-gray-50"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setEditDeviceId(Number(item.device_id));
                                    }}
                                  >
                                    Editar equipo
                                  </button>
                                )}
                              </div>
                            )}
                          </td>
                        </tr>
                        {item.plan_id && expanded && (
                          <tr className="border-t bg-gray-50/60">
                            <td className="p-3" colSpan={8}>
                              {repState.loading ? (
                                <div className="text-sm text-gray-500">Cargando repuestos...</div>
                              ) : repState.err ? (
                                <div className="text-sm text-red-700">{repState.err}</div>
                              ) : (
                                <div className="space-y-2">
                                  <div className="text-sm font-medium">Detalle de repuestos preventivos</div>
                                  {!repState.items?.length ? (
                                    <div className="text-sm text-gray-500">Sin repuestos configurados.</div>
                                  ) : (
                                    <div className="overflow-x-auto">
                                      <table className="min-w-full text-xs">
                                        <thead>
                                          <tr className="text-left">
                                            <th className="p-2">Repuesto</th>
                                            <th className="p-2">Periodicidad</th>
                                            <th className="p-2">Última</th>
                                            <th className="p-2">Próxima</th>
                                            <th className="p-2">Estado</th>
                                            <th className="p-2">Acciones</th>
                                          </tr>
                                        </thead>
                                        <tbody>
                                          {repState.items.map((rep) => (
                                            <tr key={rep.id} className="border-t">
                                              <td className="p-2">{rep.nombre_repuesto || "-"}</td>
                                              <td className="p-2">{rep.periodicidad_valor} {rep.periodicidad_unidad}</td>
                                              <td className="p-2">{fmtDate(rep.ultima_revision_fecha)}</td>
                                              <td className="p-2">{fmtDate(rep.proxima_revision_fecha)}</td>
                                              <td className="p-2"><PreventivoBadge estado={rep.preventivo_estado} dias={rep.preventivo_dias_restantes} /></td>
                                              <td className="p-2">
                                                {canPlanEdit ? (
                                                  <div className="flex items-center gap-1">
                                                    <button
                                                      className="px-2 py-1 rounded border hover:bg-gray-100"
                                                      onClick={() => openEditRepuestoModal(item.device_id, rep)}
                                                    >
                                                      Editar
                                                    </button>
                                                    <button
                                                      className="px-2 py-1 rounded border border-red-300 text-red-700 hover:bg-red-50 disabled:opacity-50"
                                                      disabled={repuestoDeleteBusyId === rep.id}
                                                      onClick={() => deleteAgendaRepuesto(item.device_id, rep)}
                                                    >
                                                      Eliminar
                                                    </button>
                                                  </div>
                                                ) : (
                                                  <span className="text-gray-400">-</span>
                                                )}
                                              </td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  )}
                                  {canPlanEdit && (
                                    <div className="pt-1">
                                      <button className="px-2 py-1 rounded border text-xs hover:bg-white" onClick={() => openCreateRepuestoModal(item)}>
                                        + Agregar repuesto
                                      </button>
                                    </div>
                                  )}
                                </div>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {activeTab === "instituciones" && (
        <>
          {institucionesErr && <div className="bg-red-100 border border-red-300 text-red-800 p-2 rounded mb-3">{institucionesErr}</div>}
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <span className="text-sm">Institución:</span>
            <select className="border rounded p-2 min-w-72" value={selectedInstitucionId} onChange={(e) => setSelectedInstitucionId(e.target.value)}>
              <option value="">Seleccione una institución</option>
              {institucionesConPreventivo.map((inst) => (
                <option key={inst.customer_id} value={inst.customer_id}>
                  {inst.razon_social} {inst.cod_empresa ? `(${inst.cod_empresa})` : ""}
                </option>
              ))}
            </select>
            {canPlanEdit && (
              <button className="btn" onClick={() => { setAddInstitutionErr(""); setAddInstitutionOpen(true); }}>
                Agregar institución
              </button>
            )}
          </div>

          {!selectedInstitucionId ? (
            <div className="text-sm text-gray-500">Seleccione una institución para ver su plan y revisiones.</div>
          ) : (
            <>
              {selectedInstState === "vencido" || selectedInstState === "proximo" || selectedInstDraftId ? (
                <div className="border rounded p-3 mb-3 bg-amber-50 border-amber-200 flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm text-amber-900">
                    {selectedInstDraftId
                      ? "Hay una revisión institucional en borrador pendiente de cierre."
                      : "Esta institución requiere actualización de revisión preventiva."}
                  </div>
                  {canRevisionMutate && (
                    <button
                      className="px-3 py-1.5 rounded bg-amber-600 text-white hover:bg-amber-700"
                      onClick={() => {
                        if (selectedInstDraftId) openRevision(selectedInstDraftId);
                        else startOrContinueInstitutionRevision(selectedInstitucionId);
                      }}
                    >
                      {selectedInstDraftId ? "Continuar revisión pendiente" : "Actualizar revisión ahora"}
                    </button>
                  )}
                </div>
              ) : null}

              <div className="border rounded p-3 mb-3">
                <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                  <div className="font-medium">Plan preventivo institucional</div>
                  <div className="flex items-center gap-2">
                    {canPlanEdit && (
                      <button className="px-2 py-1 rounded border text-xs hover:bg-gray-50" onClick={() => openCustomerPlanModal(selectedInstitucion)}>
                        {mergedInstPlan ? "Editar plan" : "Crear plan"}
                      </button>
                    )}
                    {canRevisionMutate && mergedInstPlan && (
                      <>
                        <button className="px-2 py-1 rounded border text-xs hover:bg-gray-50" onClick={() => startOrContinueInstitutionRevision(selectedInstitucionId)}>
                          Nueva revisión
                        </button>
                        <button className="px-2 py-1 rounded border text-xs hover:bg-gray-50" onClick={() => startOrContinueInstitutionRevision(selectedInstitucionId)}>
                          Actualizar revisión ahora
                        </button>
                      </>
                    )}
                  </div>
                </div>
                {!mergedInstPlan ? (
                  <div className="text-sm text-gray-500">Sin plan activo.</div>
                ) : (
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
                    <div><div className="text-xs text-gray-500">Periodicidad</div><div>{mergedInstPlan.periodicidad_valor} {mergedInstPlan.periodicidad_unidad}</div></div>
                    <div><div className="text-xs text-gray-500">Última</div><div>{fmtDate(mergedInstPlan.ultima_revision_fecha)}</div></div>
                    <div><div className="text-xs text-gray-500">Próxima</div><div>{fmtDate(mergedInstPlan.proxima_revision_fecha)}</div></div>
                    <div><div className="text-xs text-gray-500">Aviso</div><div>{mergedInstPlan.aviso_anticipacion_dias} días</div></div>
                    <div><div className="text-xs text-gray-500">Estado</div><div><PreventivoBadge estado={selectedInstState} dias={selectedInstitucion?.preventivo_dias_restantes || mergedInstPlan?.preventivo_dias_restantes} /></div></div>
                  </div>
                )}
              </div>

              {instRevisionesErr && <div className="bg-red-100 border border-red-300 text-red-800 p-2 rounded mb-3">{instRevisionesErr}</div>}

              <div className="border rounded p-3 mb-3">
                <div className="font-medium mb-2">Historial de revisiones</div>
                {instRevisionesLoading ? (
                  "Cargando revisiones..."
                ) : instRevisiones.length === 0 ? (
                  <div className="text-sm text-gray-500">Sin revisiones.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="min-w-full text-sm">
                      <thead>
                        <tr className="text-left">
                          <th className="p-2">ID</th>
                          <th className="p-2">Estado</th>
                          <th className="p-2">Programada</th>
                          <th className="p-2">Realizada</th>
                          <th className="p-2">Items</th>
                          <th className="p-2">Acción</th>
                        </tr>
                      </thead>
                      <tbody>
                        {instRevisiones.map((rev) => (
                          <tr key={rev.id} className="border-t">
                            <td className="p-2">#{rev.id}</td>
                            <td className="p-2">{rev.estado}</td>
                            <td className="p-2">{fmtDate(rev.fecha_programada)}</td>
                            <td className="p-2">{fmtDate(rev.fecha_realizada)}</td>
                            <td className="p-2">{rev.total_items || 0}</td>
                            <td className="p-2">
                              <button className="px-2 py-1 rounded border text-xs hover:bg-gray-50" onClick={() => openRevision(rev.id)}>
                                {rev.estado === "borrador" ? "Continuar" : "Ver"}
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {revisionOpenId && (
                <div className="border rounded p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                    <div className="font-medium">Editor de revisión #{revisionOpenId}</div>
                    <button className="px-2 py-1 rounded border text-xs hover:bg-gray-50" onClick={() => { setRevisionOpenId(null); setRevisionData(null); }}>
                      Cerrar editor
                    </button>
                  </div>
                  {revisionErr && <div className="bg-red-100 border border-red-300 text-red-800 p-2 rounded mb-2">{revisionErr}</div>}
                  {revisionLoading || !revisionData ? (
                    "Cargando revisión..."
                  ) : (
                    <>
                      <div className="text-sm text-gray-600 mb-3">
                        Estado: <b>{revisionData.revision?.estado}</b> | Programada: {fmtDate(revisionData.revision?.fecha_programada)} | Realizada: {fmtDate(revisionData.revision?.fecha_realizada)}
                      </div>

                      <div className="overflow-x-auto">
                        <table className="min-w-full text-xs">
                          <thead>
                            <tr className="text-left">
                              <th className="p-2">Item</th>
                              <th className="p-2">Estado</th>
                              <th className="p-2">Motivo no control</th>
                              <th className="p-2">Ubicación</th>
                              <th className="p-2">Acc. cambiados</th>
                              <th className="p-2">Detalle accesorios</th>
                              <th className="p-2">Observaciones</th>
                              <th className="p-2">Arrastrar próxima</th>
                              <th className="p-2">Guardar</th>
                            </tr>
                          </thead>
                          <tbody>
                            {(revisionData.items || []).map((item) => (
                              <tr key={item.id} className="border-t">
                                <td className="p-2 min-w-56">{item.equipo_snapshot || "-"}</td>
                                <td className="p-2">
                                  <select
                                    className="border rounded p-1"
                                    value={item.estado_item || "pendiente"}
                                    disabled={revisionData.revision?.estado !== "borrador"}
                                    onChange={(e) => {
                                      const val = e.target.value;
                                      setRevisionData((prev) => ({
                                        ...prev,
                                        items: prev.items.map((it) =>
                                          it.id === item.id ? { ...it, estado_item: val, arrastrar_proxima: val === "retirado" ? false : it.arrastrar_proxima } : it
                                        ),
                                      }));
                                    }}
                                  >
                                    {ITEM_STATES.map((st) => <option key={st.value} value={st.value}>{st.label}</option>)}
                                  </select>
                                </td>
                                <td className="p-2">
                                  <input
                                    className="border rounded p-1 w-44"
                                    value={item.motivo_no_control || ""}
                                    disabled={revisionData.revision?.estado !== "borrador"}
                                    onChange={(e) =>
                                      setRevisionData((prev) => ({
                                        ...prev,
                                        items: prev.items.map((it) => (it.id === item.id ? { ...it, motivo_no_control: e.target.value } : it)),
                                      }))
                                    }
                                  />
                                </td>
                                <td className="p-2"><input className="border rounded p-1 w-36" value={item.ubicacion_detalle || ""} disabled={revisionData.revision?.estado !== "borrador"} onChange={(e) => setRevisionData((prev) => ({ ...prev, items: prev.items.map((it) => it.id === item.id ? { ...it, ubicacion_detalle: e.target.value } : it) }))} /></td>
                                <td className="p-2 text-center"><input type="checkbox" checked={!!item.accesorios_cambiados} disabled={revisionData.revision?.estado !== "borrador"} onChange={(e) => setRevisionData((prev) => ({ ...prev, items: prev.items.map((it) => it.id === item.id ? { ...it, accesorios_cambiados: e.target.checked } : it) }))} /></td>
                                <td className="p-2"><input className="border rounded p-1 w-44" value={item.accesorios_detalle || ""} disabled={revisionData.revision?.estado !== "borrador"} onChange={(e) => setRevisionData((prev) => ({ ...prev, items: prev.items.map((it) => it.id === item.id ? { ...it, accesorios_detalle: e.target.value } : it) }))} /></td>
                                <td className="p-2"><input className="border rounded p-1 w-44" value={item.notas || ""} disabled={revisionData.revision?.estado !== "borrador"} onChange={(e) => setRevisionData((prev) => ({ ...prev, items: prev.items.map((it) => it.id === item.id ? { ...it, notas: e.target.value } : it) }))} /></td>
                                <td className="p-2 text-center"><input type="checkbox" checked={!!item.arrastrar_proxima} disabled={revisionData.revision?.estado !== "borrador"} onChange={(e) => setRevisionData((prev) => ({ ...prev, items: prev.items.map((it) => it.id === item.id ? { ...it, arrastrar_proxima: e.target.checked } : it) }))} /></td>
                                <td className="p-2">
                                  <button
                                    className="px-2 py-1 rounded border text-xs hover:bg-gray-50 disabled:opacity-50"
                                    disabled={revisionData.revision?.estado !== "borrador" || savingItemId === item.id}
                                    onClick={async () => {
                                      try {
                                        setSavingItemId(item.id);
                                        const payload = {
                                          estado_item: item.estado_item,
                                          motivo_no_control: item.motivo_no_control || "",
                                          ubicacion_detalle: item.ubicacion_detalle || "",
                                          accesorios_cambiados: !!item.accesorios_cambiados,
                                          accesorios_detalle: item.accesorios_detalle || "",
                                          notas: item.notas || "",
                                          arrastrar_proxima: !!item.arrastrar_proxima,
                                          equipo_snapshot: item.equipo_snapshot || "",
                                          serie_snapshot: item.serie_snapshot || "",
                                          interno_snapshot: item.interno_snapshot || "",
                                          orden: item.orden || 1,
                                        };
                                        const res = await patchPreventivoRevisionItem(revisionOpenId, item.id, payload);
                                        setRevisionData((prev) => ({
                                          ...prev,
                                          items: prev.items.map((it) => (it.id === item.id ? { ...it, ...(res?.item || {}) } : it)),
                                        }));
                                      } catch (e) {
                                        setRevisionErr(e?.message || "No se pudo guardar el elemento.");
                                      } finally {
                                        setSavingItemId(null);
                                      }
                                    }}
                                  >
                                    Guardar
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>

                      {revisionData.revision?.estado === "borrador" && (
                        <>
                          <div className="flex flex-wrap items-center gap-2 mt-3">
                            <input
                              type="text"
                              className="border rounded p-2 w-full max-w-md"
                              placeholder="Nuevo elemento libre (equipo/elemento)"
                              value={newItemName}
                              onChange={(e) => setNewItemName(e.target.value)}
                            />
                            <button
                              className="px-3 py-1.5 rounded border hover:bg-gray-50"
                              onClick={async () => {
                                const txt = (newItemName || "").trim();
                                if (!txt) return;
                                try {
                                  await postPreventivoRevisionItem(revisionOpenId, { equipo_snapshot: txt, estado_item: "pendiente", arrastrar_proxima: true });
                                  setNewItemName("");
                                  await openRevision(revisionOpenId);
                                } catch (e) {
                                  setRevisionErr(e?.message || "No se pudo agregar el elemento.");
                                }
                              }}
                            >
                              Agregar elemento
                            </button>
                          </div>

                          <div className="border rounded p-3 mt-3 bg-gray-50">
                            <div className="font-medium mb-2">Cerrar revisión</div>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                              <label className="block">
                                <div className="text-xs text-gray-600 mb-1">Fecha realizada</div>
                                <input type="date" className="border rounded p-2 w-full" value={closeRevisionForm.fecha_realizada} onChange={(e) => setCloseRevisionForm((prev) => ({ ...prev, fecha_realizada: e.target.value }))} />
                              </label>
                              <label className="block">
                                <div className="text-xs text-gray-600 mb-1">Resumen</div>
                                <input type="text" className="border rounded p-2 w-full" value={closeRevisionForm.resumen} onChange={(e) => setCloseRevisionForm((prev) => ({ ...prev, resumen: e.target.value }))} />
                              </label>
                            </div>
                            <div className="flex justify-end mt-3">
                              <button
                                className="px-3 py-1.5 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
                                disabled={closingRevision}
                                onClick={async () => {
                                  try {
                                    setClosingRevision(true);
                                    await postPreventivoRevisionCerrar(revisionOpenId, {
                                      fecha_realizada: closeRevisionForm.fecha_realizada || todayISO(),
                                      resumen: closeRevisionForm.resumen || "",
                                    });
                                    await openRevision(revisionOpenId);
                                    await loadInstituciones();
                                    if (selectedInstitucionId) await loadInstitucionRevisiones(selectedInstitucionId);
                                    if (activeTab === "preventivos") await loadAgenda();
                                  } catch (e) {
                                    setRevisionErr(e?.message || "No se pudo cerrar la revisión.");
                                  } finally {
                                    setClosingRevision(false);
                                  }
                                }}
                              >
                                Cerrar revisión
                              </button>
                            </div>
                          </div>
                        </>
                      )}
                    </>
                  )}
                </div>
              )}
            </>
          )}
        </>
      )}

      {editDeviceId && (
        <EditModal
          deviceId={editDeviceId}
          canEdit={canEdit}
          customers={sortedInstituciones}
          onClose={() => setEditDeviceId(null)}
          onSaved={async () => {
            setReloadDevicesKey(Date.now());
            if (activeTab === "preventivos") await loadAgenda();
          }}
        />
      )}
      {mgModalRow && (
        <MgVentaModal
          row={mgModalRow}
          mode={mgModalMode}
          onClose={() => setMgModalRow(null)}
          onSaved={() => setReloadDevicesKey(Date.now())}
        />
      )}

      {planModalCtx && (
        <PreventivoPlanModal
          title={planModalCtx.title}
          initialPlan={planModalCtx.initialPlan}
          onClose={() => setPlanModalCtx(null)}
          onSubmit={savePlan}
          saving={planSaving}
          error={planErr}
        />
      )}

      {deviceRevisionCtx && (
        <DeviceRevisionModal
          row={deviceRevisionCtx}
          onClose={() => setDeviceRevisionCtx(null)}
          onSubmit={saveDeviceRevision}
          saving={deviceRevisionSaving}
          error={deviceRevisionErr}
        />
      )}

      {repuestoRevisionCtx && (
        <DeviceRepuestosRevisionModal
          row={repuestoRevisionCtx.row}
          repuestos={repuestoRevisionCtx.repuestos || []}
          onClose={() => setRepuestoRevisionCtx(null)}
          onSubmit={saveDeviceRepuestoRevision}
          saving={repuestoRevisionSaving}
          loading={!!repuestoRevisionCtx.loading}
          error={repuestoRevisionErr}
        />
      )}

      {repuestoModalCtx && (
        <PreventivoRepuestoModal
          title={repuestoModalCtx.mode === "edit" ? "Editar repuesto preventivo" : "Agregar repuesto preventivo"}
          initialItem={repuestoModalCtx.item || null}
          catalogOptions={repuestosCatalogOptions}
          onClose={() => setRepuestoModalCtx(null)}
          onSubmit={saveRepuestoModal}
          saving={repuestoModalSaving}
          error={repuestoModalErr}
        />
      )}

      {addInstitutionOpen && (
        <AddInstitutionModal
          onClose={() => setAddInstitutionOpen(false)}
          saving={addInstitutionSaving}
          error={addInstitutionErr}
          onSubmit={async (form) => {
            if (!form.razon_social.trim() || !form.cod_empresa.trim()) {
              setAddInstitutionErr("Razón social y código de empresa son obligatorios.");
              return;
            }
            try {
              setAddInstitutionSaving(true);
              setAddInstitutionErr("");
              await postCliente({
                razon_social: form.razon_social.trim(),
                cod_empresa: form.cod_empresa.trim(),
                telefono: form.telefono.trim() || null,
                telefono_2: form.telefono_2.trim() || null,
                email: form.email.trim() || null,
              });
              setAddInstitutionOpen(false);
              await loadInstituciones();
            } catch (e) {
              setAddInstitutionErr(e?.message || "No se pudo crear la institución.");
            } finally {
              setAddInstitutionSaving(false);
            }
          }}
        />
      )}

      {addPreventivoEquipoOpen && (
        <AddPreventivoEquipoModal
          onClose={() => setAddPreventivoEquipoOpen(false)}
          onSelect={onSelectExistingPreventivoDevice}
          onCreateManaged={() => {
            setAddManagedDeviceErr("");
            setAddManagedDeviceContext("preventivos");
            setAddPreventivoEquipoOpen(false);
            setAddManagedDeviceOpen(true);
          }}
        />
      )}

      {addManagedDeviceOpen && (
        <AddManagedDeviceModal
          customers={sortedInstituciones}
          onClose={() => setAddManagedDeviceOpen(false)}
          onSubmit={onCreateManagedDevice}
          saving={addManagedDeviceSaving}
          error={addManagedDeviceErr}
        />
      )}

      {mergeOpen && mergeEquipo1 && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <div className="bg-white rounded shadow-lg w-full max-w-4xl p-4">
            <div className="flex items-start justify-between mb-3 gap-3">
              <div>
                <div className="text-lg font-semibold">Unificar equipos</div>
                <div className="text-sm text-gray-600">Paso {mergeStep} de 2</div>
              </div>
              {mergeStep === 2 && (
                <button className="px-3 py-1.5 rounded border text-sm hover:bg-gray-50" onClick={() => setMergeStep(1)} disabled={mergeSaving}>
                  Cambiar equipo 2
                </button>
              )}
            </div>

            {mergeErr && <div className="bg-red-100 text-red-800 border border-red-300 rounded p-2 mb-3">{mergeErr}</div>}

            {mergeStep === 1 ? (
              <div className="space-y-3">
                <div className="text-sm">
                  <div className="text-gray-700">Equipo 1</div>
                  <div className="text-gray-900 font-medium">
                    #{mergeEquipo1.id} - <DeviceIdentifier row={mergeEquipo1} />
                  </div>
                </div>
                <label className="block">
                  <div className="text-sm text-gray-700 mb-1">Buscar equipo 2 (N/S o MG)</div>
                  <div className="flex items-center gap-2">
                    <input type="text" value={mergeSearch} onChange={(e) => setMergeSearch(e.target.value)} className="border rounded p-2 w-full" placeholder="Ej: MG 1234 o NS 00123" disabled={mergeSearching} onKeyDown={(e) => { if (e.key === "Enter") runMergeSearch(); }} />
                    <button className="btn" onClick={runMergeSearch} disabled={mergeSearching}>Buscar</button>
                  </div>
                </label>
                {mergeSearchErr && <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2">{mergeSearchErr}</div>}
                <div className="border rounded overflow-auto max-h-72">
                  {mergeSearching ? "Buscando..." : mergeSearchResults.map((row) => (
                    <div key={row.id} className="p-2 border-t flex items-center justify-between">
                      <div className="text-xs">#{row.id} - <DeviceIdentifier row={row} /></div>
                      <button className="px-2 py-1 rounded border text-xs" onClick={() => selectMergeEquipo2(row)}>Seleccionar</button>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="space-y-4 text-sm">
                {mergeEquipo2 ? (
                  <>
                    <div className="border rounded p-3">
                      <div className="text-sm font-medium mb-2">N/S final</div>
                      <label className="flex items-center gap-2">
                        <input
                          type="radio"
                          name="merge-ns"
                          value="equipo1"
                          checked={mergeNsChoice === "equipo1"}
                          onChange={() => setMergeNsChoice("equipo1")}
                        />
                        Equipo 1: {mergeEquipo1?.numero_serie || "(vacío)"}
                      </label>
                      <label className="flex items-center gap-2 mt-2">
                        <input
                          type="radio"
                          name="merge-ns"
                          value="equipo2"
                          checked={mergeNsChoice === "equipo2"}
                          onChange={() => setMergeNsChoice("equipo2")}
                        />
                        Equipo 2: {mergeEquipo2?.numero_serie || "(vacío)"}
                      </label>
                    </div>
                    <div className="border rounded p-3">
                      <div className="text-sm font-medium mb-2">MG final</div>
                      <label className="flex items-center gap-2">
                        <input
                          type="radio"
                          name="merge-mg"
                          value="equipo1"
                          checked={mergeMgChoice === "equipo1"}
                          onChange={() => setMergeMgChoice("equipo1")}
                        />
                        Equipo 1: {mergeEquipo1?.numero_interno || "(vacío)"}
                      </label>
                      <label className="flex items-center gap-2 mt-2">
                        <input
                          type="radio"
                          name="merge-mg"
                          value="equipo2"
                          checked={mergeMgChoice === "equipo2"}
                          onChange={() => setMergeMgChoice("equipo2")}
                        />
                        Equipo 2: {mergeEquipo2?.numero_interno || "(vacío)"}
                      </label>
                    </div>
                  </>
                ) : (
                  <div>Selecciona equipo 2.</div>
                )}
              </div>
            )}

            <div className="flex justify-between items-center mt-4">
              <button className="px-3 py-1.5 rounded border" onClick={resetMergeState} disabled={mergeSaving}>Cancelar</button>
              {mergeStep === 2 && (
                <button
                  className="px-3 py-1.5 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
                  disabled={!canSubmitMerge}
                  onClick={async () => {
                    try {
                      setMergeSaving(true);
                      setMergeErr("");
                      await postDevicesMerge({
                        target_id: mergeEquipo1.id,
                        source_id: mergeEquipo2.id,
                        numero_serie: mergeNsFinalValue,
                        numero_interno: mergeMgFinalValue,
                      });
                      resetMergeState();
                      setReloadDevicesKey(Date.now());
                    } catch (e) {
                      setMergeErr(e?.message || "No se pudo unificar.");
                    } finally {
                      setMergeSaving(false);
                    }
                  }}
                >
                  Unificar
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Menu({ button, children }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const buttonRef = useRef(null);
  const panelRef = useRef(null);
  const [coords, setCoords] = useState({ top: 0, left: 0 });

  const recalcPosition = () => {
    const btn = buttonRef.current;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const panelWidth = panelRef.current?.offsetWidth || 160;
    const margin = 8;
    const viewportWidth = window.innerWidth || 0;
    let left = rect.right - panelWidth;
    if (left < margin) left = margin;
    if (left + panelWidth > viewportWidth - margin) {
      left = Math.max(margin, viewportWidth - panelWidth - margin);
    }
    const top = rect.bottom + 6;
    setCoords({ top, left });
  };

  const toggle = () => setOpen((v) => !v);
  const close = () => setOpen(false);

  useEffect(() => {
    if (!open) return;
    const raf = requestAnimationFrame(recalcPosition);
    return () => cancelAnimationFrame(raf);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e) => {
      const insideTrigger = ref.current && ref.current.contains(e.target);
      const insidePanel = panelRef.current && panelRef.current.contains(e.target);
      if (!insideTrigger && !insidePanel) setOpen(false);
    };
    const onKeyDown = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onResize = () => recalcPosition();
    const onScroll = () => recalcPosition();
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  return (
    <div ref={ref}>
      {button({ open, toggle, buttonRef })}
      {open
        ? createPortal(
          <div
            ref={panelRef}
            style={{ position: "fixed", top: coords.top, left: coords.left, zIndex: 80 }}
            onClick={(e) => e.stopPropagation()}
          >
            {children({ close })}
          </div>,
          document.body
        )
        : null}
    </div>
  );
}

function SortableTh({ label, field, sort, setSort }) {
  const isAsc = sort === field;
  const isDesc = sort === `-${field}`;
  const next = () => {
    if (isAsc) setSort(`-${field}`);
    else if (isDesc) setSort("id");
    else setSort(field);
  };
  return (
    <th className="p-2 cursor-pointer select-none" onClick={next}>
      <span className="inline-flex items-center gap-1">
        {label}
        {isAsc && <span aria-label="asc">^</span>}
        {isDesc && <span aria-label="desc">v</span>}
      </span>
    </th>
  );
}




