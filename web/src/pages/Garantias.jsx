import { useEffect, useMemo, useState } from "react";
import {
  checkGarantiaFabrica,
  listWarrantyRules,
  createWarrantyRule,
  deleteWarrantyRule,
  patchWarrantyRule,
  getMarcas,
  getModelosByBrand,
  getBejermanIngressCompanies,
} from "@/lib/api";

const DEFAULT_BEJERMAN_COMPANIES = [
  { key: "SEPID", label: "SEPID SA" },
  { key: "MGBIO", label: "MG BIO" },
  { key: "TEST", label: "Empresa de prueba" },
];

const fieldClass = "border border-gray-300 bg-white rounded p-2 text-[15px] font-semibold text-gray-900";
const labelClass = "block text-sm font-medium mb-1";

const yesNoUnknown = (value) => {
  if (value === true) return "Sí";
  if (value === false) return "No";
  return "No determinada";
};

export default function Garantias() {
  const [ns, setNs] = useState("");
  const [empresaBejerman, setEmpresaBejerman] = useState("SEPID");
  const [bejermanCompanies, setBejermanCompanies] = useState(DEFAULT_BEJERMAN_COMPANIES);
  const [searchMarcaId, setSearchMarcaId] = useState("");
  const [searchModeloId, setSearchModeloId] = useState("");
  const [searchModelos, setSearchModelos] = useState([]);
  const [out, setOut] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const [marcas, setMarcas] = useState([]);
  const [marcaId, setMarcaId] = useState("");
  const [modelos, setModelos] = useState([]);
  const [modeloId, setModeloId] = useState("");
  const [rules, setRules] = useState([]);
  const [ruleDays, setRuleDays] = useState(365);
  const [ruleNotas, setRuleNotas] = useState("");
  const [busyRule, setBusyRule] = useState(false);
  const [modelNames, setModelNames] = useState({});
  const [loadedBrands, setLoadedBrands] = useState(new Set());

  const selectedSearchBrand = useMemo(
    () => (marcas || []).find((m) => String(m.id) === String(searchMarcaId)) || null,
    [marcas, searchMarcaId]
  );

  useEffect(() => {
    (async () => {
      try {
        const ms = await getMarcas();
        setMarcas(ms || []);
      } catch {}
      try {
        const rs = await listWarrantyRules({ activo: 1 });
        setRules(Array.isArray(rs) ? rs : []);
      } catch {}
      try {
        const companies = await getBejermanIngressCompanies();
        const items = Array.isArray(companies?.items) && companies.items.length ? companies.items : DEFAULT_BEJERMAN_COMPANIES;
        setBejermanCompanies(items);
        if (!items.some((item) => item.key === empresaBejerman)) {
          setEmpresaBejerman(items[0]?.key || "SEPID");
        }
      } catch {
        setBejermanCompanies(DEFAULT_BEJERMAN_COMPANIES);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    (async () => {
      if (!searchMarcaId) {
        setSearchModelos([]);
        setSearchModeloId("");
        return;
      }
      try {
        const mods = await getModelosByBrand(searchMarcaId);
        setSearchModelos(mods || []);
      } catch {
        setSearchModelos([]);
      }
    })();
  }, [searchMarcaId]);

  useEffect(() => {
    (async () => {
      if (!marcaId) {
        setModelos([]);
        return;
      }
      try {
        const mods = await getModelosByBrand(marcaId);
        setModelos(mods || []);
      } catch {}
    })();
  }, [marcaId]);

  useEffect(() => {
    (async () => {
      if (!Array.isArray(rules) || rules.length === 0) return;
      const distinctBrands = Array.from(new Set(rules.map((r) => r.brand_id).filter(Boolean)));
      const toLoad = distinctBrands.filter((bid) => !loadedBrands.has(bid));
      if (toLoad.length === 0) return;
      const newModelNames = { ...modelNames };
      const newLoaded = new Set(loadedBrands);
      for (const bid of toLoad) {
        try {
          const mods = await getModelosByBrand(bid);
          (mods || []).forEach((m) => {
            if (m?.id) newModelNames[m.id] = m.nombre || String(m.id);
          });
          newLoaded.add(bid);
        } catch {}
      }
      setModelNames(newModelNames);
      setLoadedBrands(newLoaded);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(rules)]);

  async function probe(e) {
    e?.preventDefault?.();
    setLoading(true);
    setErr("");
    setOut(null);
    try {
      const r = await checkGarantiaFabrica(ns.trim(), selectedSearchBrand?.nombre || "", {
        brand_id: searchMarcaId || null,
        model_id: searchModeloId || null,
        company_key: empresaBejerman || "SEPID",
      });
      setOut(r || null);
    } catch (e2) {
      setErr(e2?.message || "Error consultando garantía");
    } finally {
      setLoading(false);
    }
  }

  const meta = out?.meta || {};
  const warrantyDays = out?.warranty_days || out?.days || meta.days || null;
  const hasSale = out?.found === true;
  const statusClass =
    out?.within_365_days === true
      ? "border-emerald-200 bg-emerald-50 text-emerald-950"
      : out?.within_365_days === false
        ? "border-amber-200 bg-amber-50 text-amber-950"
        : "border-gray-200 bg-gray-50 text-gray-800";

  return (
    <div className="p-4">
      <h1 className="text-xl font-semibold mb-3">Garantías</h1>
      <p className="text-sm text-gray-600 mb-4">
        La fecha de venta se toma de Bejerman. La duración base es de 365 días y las excepciones se aplican por marca/modelo.
      </p>

      <form className="mb-4 rounded border border-gray-200 bg-white p-3" onSubmit={probe}>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-12 md:items-end">
          <div className="md:col-span-3">
            <label className={labelClass}>Empresa Bejerman</label>
            <select
              className={`${fieldClass} w-full`}
              value={empresaBejerman}
              onChange={(e) => setEmpresaBejerman((e.target.value || "SEPID").toUpperCase())}
            >
              {bejermanCompanies.map((company) => (
                <option key={company.key} value={company.key}>
                  {company.label}
                </option>
              ))}
            </select>
          </div>
          <div className="md:col-span-3">
            <label className={labelClass}>Número de serie</label>
            <input
              className={`${fieldClass} w-full`}
              value={ns}
              onChange={(e) => setNs(e.target.value)}
              placeholder="E21BE704193"
              required
            />
          </div>
          <div className="md:col-span-3">
            <label className={labelClass}>Marca para regla (opcional)</label>
            <select
              className={`${fieldClass} w-full`}
              value={searchMarcaId}
              onChange={(e) => {
                setSearchMarcaId(e.target.value);
                setSearchModeloId("");
              }}
            >
              <option value="">Usar marca del equipo si existe</option>
              {(marcas || []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.nombre}
                </option>
              ))}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className={labelClass}>Modelo (opcional)</label>
            <select
              className={`${fieldClass} w-full`}
              value={searchModeloId}
              onChange={(e) => setSearchModeloId(e.target.value)}
              disabled={!searchMarcaId}
            >
              <option value="">Sin modelo</option>
              {(searchModelos || []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.nombre}
                </option>
              ))}
            </select>
          </div>
          <div className="md:col-span-1">
            <button
              type="submit"
              disabled={loading || !ns.trim()}
              className="w-full rounded bg-blue-600 px-3 py-2 font-semibold text-white disabled:opacity-50"
            >
              {loading ? "..." : "Verificar"}
            </button>
          </div>
        </div>
      </form>

      {err && <div className="text-red-600 text-sm mb-2">{err}</div>}
      {out && (
        <div className={`rounded border p-3 text-sm ${statusClass}`}>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <div>
              <div className="text-xs font-semibold uppercase opacity-80">Garantía de fábrica</div>
              <div className="text-lg font-semibold">{yesNoUnknown(out.within_365_days)}</div>
            </div>
            <div>
              <div className="text-xs font-semibold uppercase opacity-80">Fecha de venta</div>
              <div className="font-semibold">{out.fecha_venta || "-"}</div>
            </div>
            <div>
              <div className="text-xs font-semibold uppercase opacity-80">Vence</div>
              <div className="font-semibold">{out.garantia_vence || "-"}</div>
            </div>
            <div>
              <div className="text-xs font-semibold uppercase opacity-80">Duración aplicada</div>
              <div className="font-semibold">{warrantyDays ? `${warrantyDays} días` : "-"}</div>
            </div>
          </div>

          {!hasSale && (
            <div className="mt-3 rounded border border-gray-300 bg-white/70 p-2 text-gray-700">
              No se encontró una venta Bejerman para esta serie. La garantía queda no determinada.
            </div>
          )}

          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
            <div>
              <div className="text-xs font-semibold uppercase opacity-80">Fuente</div>
              <div>{out.source === "bejerman_sale" ? "Venta Bejerman" : out.source || "-"}</div>
              <div className="text-xs opacity-80">{meta.companyKey ? `Empresa: ${meta.companyKey}` : ""}</div>
            </div>
            <div>
              <div className="text-xs font-semibold uppercase opacity-80">Comprobante</div>
              <div>{meta.documentLabel || "-"}</div>
              <div className="text-xs opacity-80">{meta.cacheSyncedAt ? `Sincronizado: ${meta.cacheSyncedAt}` : ""}</div>
            </div>
            <div>
              <div className="text-xs font-semibold uppercase opacity-80">Cliente</div>
              <div>{meta.customerName || "-"}</div>
              <div className="text-xs opacity-80">{[meta.customerCode, meta.customerCuit].filter(Boolean).join(" · ")}</div>
            </div>
            <div className="md:col-span-3">
              <div className="text-xs font-semibold uppercase opacity-80">Artículo</div>
              <div>{[meta.articleCode, meta.articleDescription].filter(Boolean).join(" · ") || "-"}</div>
            </div>
          </div>
        </div>
      )}

      <div className="mt-8">
        <h2 className="font-semibold mb-2">Excepciones de garantía</h2>
        <div className="text-xs text-gray-600 mb-3">Las nuevas reglas requieren rol de administrador.</div>
        <div className="flex flex-wrap items-end gap-3 mb-4">
          <div>
            <label className={labelClass}>Marca</label>
            <select className={`${fieldClass} w-64`} value={marcaId} onChange={(e) => { setMarcaId(e.target.value); setModeloId(""); }}>
              <option value="">-- Seleccione --</option>
              {(marcas || []).map((m) => <option key={m.id} value={m.id}>{m.nombre}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Modelo</label>
            <select className={`${fieldClass} w-64`} value={modeloId} onChange={(e) => setModeloId(e.target.value)} disabled={!marcaId}>
              <option value="">-- (opcional) --</option>
              {(modelos || []).map((m) => <option key={m.id} value={m.id}>{m.nombre}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Duración (días)</label>
            <input className={`${fieldClass} w-40`} type="number" min={1} value={ruleDays} onChange={(e) => setRuleDays(Number(e.target.value || 0))} />
          </div>
          <div>
            <label className={labelClass}>Notas</label>
            <input className={`${fieldClass} w-64`} value={ruleNotas} onChange={(e) => setRuleNotas(e.target.value)} />
          </div>
          <div>
            <button disabled={busyRule || !ruleDays || (!marcaId && !modeloId)} onClick={async () => {
              try {
                setBusyRule(true);
                const payload = { days: ruleDays };
                if (marcaId) payload.brand_id = Number(marcaId);
                if (modeloId) payload.model_id = Number(modeloId);
                if (ruleNotas) payload.notas = ruleNotas;
                await createWarrantyRule(payload);
                const rs = await listWarrantyRules({ activo: 1 });
                setRules(Array.isArray(rs) ? rs : []);
                setRuleNotas("");
              } catch (e) {
                alert(e?.message || "Error creando regla");
              } finally {
                setBusyRule(false);
              }
            }} className="bg-green-600 text-white px-3 py-2 rounded disabled:opacity-50">Agregar</button>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="p-2">ID</th>
                <th className="p-2">Marca</th>
                <th className="p-2">Modelo</th>
                <th className="p-2">Días</th>
                <th className="p-2">Notas</th>
                <th className="p-2">Activo</th>
                <th className="p-2">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {(rules || []).map((r) => (
                <tr key={r.id} className="border-t">
                  <td className="p-2">{r.id}</td>
                  <td className="p-2">{(marcas.find((m) => m.id === r.brand_id)?.nombre) || (r.brand_id || "-")}</td>
                  <td className="p-2">{(r.model_id ? (modelNames[r.model_id] || r.model_id) : "-")}</td>
                  <td className="p-2">
                    <input
                      type="number"
                      min={1}
                      className="border rounded p-1 w-24"
                      value={r.days}
                      onChange={(e) => setRules((prev) => prev.map((rr) => rr.id === r.id ? { ...rr, days: Number(e.target.value || 0) } : rr))}
                    />
                  </td>
                  <td className="p-2">
                    <input
                      className="border rounded p-1 w-56"
                      value={r.notas || ""}
                      onChange={(e) => setRules((prev) => prev.map((rr) => rr.id === r.id ? { ...rr, notas: e.target.value } : rr))}
                    />
                  </td>
                  <td className="p-2">
                    <input
                      type="checkbox"
                      checked={!!r.activo}
                      onChange={async (e) => {
                        const checked = !!e.target.checked;
                        setRules((prev) => prev.map((rr) => rr.id === r.id ? { ...rr, activo: checked } : rr));
                        try {
                          await patchWarrantyRule(r.id, { activo: checked });
                          const rs = await listWarrantyRules({ activo: 1 });
                          setRules(Array.isArray(rs) ? rs : []);
                        } catch (err2) {
                          alert(err2?.message || "Error guardando activo");
                        }
                      }}
                    />
                  </td>
                  <td className="p-2">
                    <button
                      className="text-blue-600 underline mr-3"
                      onClick={async () => {
                        try {
                          await patchWarrantyRule(r.id, { days: Number(r.days || 0), notas: r.notas || "", activo: !!r.activo });
                          const rs = await listWarrantyRules({ activo: 1 });
                          setRules(Array.isArray(rs) ? rs : []);
                        } catch (e) {
                          alert(e?.message || "Error guardando regla");
                        }
                      }}
                    >Guardar</button>
                    <button className="text-red-600 underline" onClick={async () => {
                      if (!confirm("¿Eliminar (desactivar) regla?")) return;
                      try {
                        await deleteWarrantyRule(r.id);
                        const rs = await listWarrantyRules({ activo: 1 });
                        setRules(Array.isArray(rs) ? rs : []);
                      } catch (e) {
                        alert(e?.message || "Error eliminando regla");
                      }
                    }}>Eliminar</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
