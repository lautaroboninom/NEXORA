import { useEffect, useMemo, useRef, useState } from "react";
import { getIngresoTest, getIngresoTestPdfBlob, getTestProtocols, patchIngresoTest } from "../../../lib/api";

function refLabel(ref) {
  const rid = (ref?.ref_id || "").trim();
  const tipo = (ref?.tipo || "").trim();
  const titulo = (ref?.titulo || "").trim();
  const edition = (ref?.edicion || ref?.anio || "").toString().trim();
  const org = (ref?.organismo_o_fabricante || "").trim();
  const parts = [];
  if (rid) parts.push(rid);
  if (tipo) parts.push(`[${tipo}]`);
  if (titulo) parts.push(titulo);
  if (edition) parts.push(`(${edition})`);
  if (org) parts.push(`- ${org}`);
  return parts.join(" ");
}

export default function TestTab({ id, setErr }) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [printing, setPrinting] = useState(false);
  const [error, setError] = useState("");

  const [templateInfo, setTemplateInfo] = useState({ key: "", version: "", tipo: "" });
  const [selectedTemplateKey, setSelectedTemplateKey] = useState("");
  const [protocolCatalog, setProtocolCatalog] = useState([]);
  const [references, setReferences] = useState([]);
  const [sections, setSections] = useState([]);
  const [resultOptions, setResultOptions] = useState([]);
  const [globalResultOptions, setGlobalResultOptions] = useState([]);

  const [values, setValues] = useState({});
  const [resultadoGlobal, setResultadoGlobal] = useState("pendiente");
  const [conclusion, setConclusion] = useState("");
  const [instrumentos, setInstrumentos] = useState("");

  const lastSavedPayloadRef = useRef("");
  const loadedRef = useRef(false);

  const hasReferences = (references || []).length > 0;
  const canMarkApto = hasReferences;
  const templateOptions = useMemo(() => {
    const rows = (protocolCatalog || [])
      .filter((row) => row && row.active !== false)
      .map((row) => {
        const key = String(row?.template_key || "").trim();
        if (!key) return null;
        const labelBase = String(row?.display_name || row?.type_key || key).trim();
        return {
          value: key,
          label: `${labelBase} | ${key}`,
          typeKey: String(row?.type_key || "").trim(),
        };
      })
      .filter(Boolean);
    if (!rows.some((row) => row.value === "aspirador_electrico_v1")) {
      rows.unshift({
        value: "aspirador_electrico_v1",
        label: "Aspirador eléctrico | aspirador_electrico_v1",
        typeKey: "aspirador",
      });
    }
    rows.sort((a, b) => a.label.localeCompare(b.label, "es"));
    return rows;
  }, [protocolCatalog]);

  const referencesById = useMemo(() => {
    const map = {};
    (references || []).forEach((r) => {
      if (r?.ref_id) map[r.ref_id] = r;
    });
    return map;
  }, [references]);

  const draftPayload = useMemo(
    () => ({
      values,
      resultado_global: resultadoGlobal,
      conclusion,
      instrumentos,
    }),
    [values, resultadoGlobal, conclusion, instrumentos]
  );
  const draftPayloadKey = useMemo(() => JSON.stringify(draftPayload), [draftPayload]);
  const hasUnsavedChanges = loadedRef.current && draftPayloadKey !== lastSavedPayloadRef.current;

  function hydrate(data) {
    const loadedValues = data?.values && typeof data.values === "object" ? data.values : {};
    const loadedResultadoGlobal = (data?.resultado_global || "pendiente").toString();
    const loadedConclusion = (data?.conclusion || "").toString();
    const loadedInstrumentos = (data?.instrumentos || "").toString();

    setTemplateInfo({
      key: data?.template_key || "",
      version: data?.template_version || "",
      tipo: data?.tipo_equipo_resuelto || "",
    });
    setSelectedTemplateKey(data?.template_key || "");
    setReferences(Array.isArray(data?.schema?.references) ? data.schema.references : []);
    setSections(Array.isArray(data?.schema?.sections) ? data.schema.sections : []);
    setResultOptions(Array.isArray(data?.schema?.result_options) ? data.schema.result_options : []);
    setGlobalResultOptions(Array.isArray(data?.schema?.global_result_options) ? data.schema.global_result_options : []);
    setValues(loadedValues);
    setResultadoGlobal(loadedResultadoGlobal);
    setConclusion(loadedConclusion);
    setInstrumentos(loadedInstrumentos);
    lastSavedPayloadRef.current = JSON.stringify({
      values: loadedValues,
      resultado_global: loadedResultadoGlobal,
      conclusion: loadedConclusion,
      instrumentos: loadedInstrumentos,
    });
    loadedRef.current = true;
  }

  async function reload() {
    const data = await getIngresoTest(id);
    hydrate(data);
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        setLoading(true);
        setError("");
        loadedRef.current = false;
        const [data, protocols] = await Promise.all([
          getIngresoTest(id),
          getTestProtocols().catch(() => []),
        ]);
        if (cancelled) return;
        setProtocolCatalog(Array.isArray(protocols) ? protocols : []);
        hydrate(data);
      } catch (e) {
        if (cancelled) return;
        const detail = e?.message || "No se pudo cargar el test";
        setError(detail);
        if (typeof setErr === "function") setErr(detail);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [id, setErr]);

  const updateValue = (key, field, val) => {
    setValues((prev) => {
      const cur = prev?.[key] && typeof prev[key] === "object" ? prev[key] : {};
      return { ...prev, [key]: { ...cur, [field]: val } };
    });
  };

  async function save(payload = draftPayload, payloadKey = draftPayloadKey) {
    try {
      setSaving(true);
      setError("");
      if ((payload?.resultado_global || "") === "apto" && !canMarkApto) {
        const detail = "No se puede emitir 'Apto' sin referencias técnicas cargadas.";
        setError(detail);
        return false;
      }
      await patchIngresoTest(id, payload);
      lastSavedPayloadRef.current = payloadKey;
      return true;
    } catch (e) {
      const detail = e?.message || "No se pudo guardar el test";
      setError(detail);
      if (typeof setErr === "function") setErr(detail);
      return false;
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    if (loading || saving || !loadedRef.current) return;
    if (!hasUnsavedChanges) return;
    const timer = setTimeout(() => {
      void save(draftPayload, draftPayloadKey);
    }, 800);
    return () => clearTimeout(timer);
  }, [loading, saving, hasUnsavedChanges, draftPayload, draftPayloadKey]);

  async function printPdf() {
    try {
      setPrinting(true);
      setError("");
      if (hasUnsavedChanges) {
        const saved = await save(draftPayload, draftPayloadKey);
        if (!saved) return;
      }
      const blob = await getIngresoTestPdfBlob(id);
      if (!(blob instanceof Blob)) throw new Error("La respuesta no fue un PDF");
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      const detail = e?.message || "No se pudo imprimir el informe de test";
      setError(detail);
      if (typeof setErr === "function") setErr(detail);
    } finally {
      setPrinting(false);
    }
  }

  async function applySelectedTemplate() {
    const nextTemplateKey = String(selectedTemplateKey || "").trim().toLowerCase();
    if (!nextTemplateKey || nextTemplateKey === String(templateInfo.key || "").trim().toLowerCase()) return;
    if (!window.confirm("Cambiar el protocolo va a recargar el formulario con la nueva plantilla. ¿Querés continuar?")) {
      setSelectedTemplateKey(templateInfo.key || "");
      return;
    }
    try {
      setSaving(true);
      setError("");
      await patchIngresoTest(id, {
        template_key: nextTemplateKey,
        values: {},
        resultado_global: "pendiente",
        conclusion: "",
        instrumentos: "",
      });
      await reload();
    } catch (e) {
      const detail = e?.message || "No se pudo cambiar el test";
      setError(detail);
      if (typeof setErr === "function") setErr(detail);
      setSelectedTemplateKey(templateInfo.key || "");
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="border rounded p-4">Cargando test...</div>;

  return (
    <div className="border rounded p-4">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <h2 className="text-lg font-semibold">Test técnico</h2>
          <div className="text-sm text-gray-600">
            {templateInfo.tipo || "-"} | {templateInfo.key || "-"} v{templateInfo.version || "-"}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="text-sm text-gray-600">
            {saving ? "Guardando..." : hasUnsavedChanges ? "Cambios pendientes..." : "Guardado automático"}
          </div>
          <button
            type="button"
            className="px-3 py-2 rounded bg-neutral-800 text-white disabled:opacity-60"
            onClick={printPdf}
            disabled={printing || saving}
          >
            {printing ? "Generando PDF..." : "Imprimir informe"}
          </button>
        </div>
      </div>

      <div className="mb-4 rounded border bg-blue-50 p-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="block min-w-[280px] flex-1">
            <span className="block text-sm font-medium text-gray-700 mb-1">Elegir protocolo manualmente</span>
            <select
              className="border rounded p-2 w-full bg-white"
              value={selectedTemplateKey}
              onChange={(e) => setSelectedTemplateKey(e.target.value)}
            >
              <option value="">-- seleccionar --</option>
              {templateOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="px-3 py-2 rounded bg-blue-700 text-white disabled:opacity-60"
            onClick={applySelectedTemplate}
            disabled={saving || printing || !selectedTemplateKey || selectedTemplateKey === templateInfo.key}
          >
            Aplicar test
          </button>
        </div>
        <div className="mt-2 text-xs text-gray-600">
          Si cambiás el protocolo, se recarga el formulario con la nueva plantilla y se guardan sus referencias.
        </div>
      </div>

      {error && <div className="mb-3 bg-red-100 border border-red-300 text-red-700 p-2 rounded">{error}</div>}

      <div className="mb-5 rounded border bg-gray-50 p-3">
        <div className="text-sm font-semibold mb-2">Norma técnica aplicada</div>
        {!references.length ? (
          <div className="text-sm text-amber-700">
            No hay referencias técnicas declaradas. El estado global no podrá marcarse como "Apto".
          </div>
        ) : (
          <div className="space-y-2">
            {references.map((ref) => (
              <div key={ref?.ref_id || Math.random()} className="text-sm">
                <div className="font-medium">{refLabel(ref)}</div>
                <div className="text-gray-600">{(ref?.aplica_a || "").toString()}</div>
                {ref?.url ? (
                  <a
                    className="text-blue-700 underline"
                    href={String(ref.url)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Ver fuente
                  </a>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </div>

      {sections.map((section) => (
        <div key={section?.id || section?.title} className="mb-6">
          <div className="font-semibold mb-2">{section?.title || "Sección"}</div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm border">
              <thead className="bg-gray-100">
                <tr className="text-left">
                  <th className="p-2 border">Parámetro</th>
                  <th className="p-2 border">Objetivo / Tolerancia</th>
                  {!['result_only', 'measured_only'].includes((section?.entry_mode || "").toString().trim().toLowerCase()) ? <th className="p-2 border">Valor a medir</th> : null}
                  {(section?.entry_mode || "").toString().trim().toLowerCase() !== "result_only" ? <th className="p-2 border">Medido</th> : null}
                  <th className="p-2 border">Resultado</th>
                  <th className="p-2 border">Ref.</th>
                </tr>
              </thead>
              <tbody>
                {(section?.items || []).map((item) => {
                  const key = (item?.key || "").toString();
                  const val = values?.[key] && typeof values[key] === "object" ? values[key] : {};
                  const refIds = Array.isArray(item?.ref_ids) ? item.ref_ids : [];
                  return (
                    <tr key={key || Math.random()} className="border-t align-top">
                      <td className="p-2 border">
                        <div className="font-medium">{item?.label || "-"}</div>
                        {(section?.entry_mode || "").toString().trim().toLowerCase() !== "result_only" && item?.unit ? <div className="text-xs text-gray-500">Unidad: {item.unit}</div> : null}
                      </td>
                      <td className="p-2 border">{item?.target || "-"}</td>
                      {!['result_only', 'measured_only'].includes((section?.entry_mode || "").toString().trim().toLowerCase()) ? (
                        <td className="p-2 border">
                          <input
                            className="border rounded p-1 w-full"
                            value={(val?.valor_a_medir || "").toString()}
                            onChange={(e) => updateValue(key, "valor_a_medir", e.target.value)}
                          />
                        </td>
                      ) : null}
                      {(section?.entry_mode || "").toString().trim().toLowerCase() !== "result_only" ? (
                        <td className="p-2 border">
                          <input
                            className="border rounded p-1 w-full"
                            value={(val?.measured || "").toString()}
                            onChange={(e) => updateValue(key, "measured", e.target.value)}
                          />
                        </td>
                      ) : null}
                      <td className="p-2 border">
                        <select
                          className="border rounded p-1 w-full"
                          value={(val?.result || "").toString()}
                          onChange={(e) => updateValue(key, "result", e.target.value)}
                        >
                          <option value="">--</option>
                          {(resultOptions || []).map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="p-2 border">
                        <div className="flex flex-wrap gap-1">
                          {refIds.map((rid) => {
                            const hit = referencesById[rid];
                            const title = hit ? refLabel(hit) : rid;
                            return (
                              <span key={`${key}-${rid}`} className="text-xs px-2 py-1 rounded bg-gray-100 border" title={title}>
                                {rid}
                              </span>
                            );
                          })}
                          {refIds.length === 0 ? <span className="text-xs text-gray-500">-</span> : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      <div className="rounded border p-3">
        <div>
          <label className="block text-sm text-gray-600 mb-1">Resultado global</label>
          <select
            className="border rounded p-2 w-full"
            value={resultadoGlobal}
            onChange={(e) => setResultadoGlobal(e.target.value)}
          >
            {(globalResultOptions || []).map((opt) => (
              <option
                key={opt.value}
                value={opt.value}
                disabled={opt.value === "apto" && !canMarkApto}
              >
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <div className="mt-3">
          <label className="block text-sm text-gray-600 mb-1">Instrumentos utilizados</label>
          <textarea
            className="border rounded p-2 w-full min-h-[70px]"
            value={instrumentos}
            onChange={(e) => setInstrumentos(e.target.value)}
            placeholder="Ej.: Analizador, vacuómetro, flowmeter, etc."
          />
        </div>
        <div className="mt-3">
          <label className="block text-sm text-gray-600 mb-1">Observaciones</label>
          <textarea
            className="border rounded p-2 w-full min-h-[80px]"
            value={conclusion}
            onChange={(e) => setConclusion(e.target.value)}
            placeholder="Conclusiones y observaciones finales"
          />
        </div>
      </div>
    </div>
  );
}
