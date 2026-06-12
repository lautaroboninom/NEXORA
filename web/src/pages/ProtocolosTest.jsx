import { useEffect, useMemo, useState } from "react";
import {
  deleteTestProtocol,
  getTestProtocol,
  getTestProtocols,
  patchTestProtocol,
  postTestProtocol,
} from "@/lib/api";

const TYPE_KEY_RE = /^[a-z0-9_]+$/;
const TEMPLATE_KEY_RE = /^[a-z0-9_]+$/;
const VALID_ENTRY_MODES = new Set(["", "result_only", "measured_only"]);

function trimText(value) {
  return String(value ?? "").trim();
}

function normKey(value) {
  return trimText(value)
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9\s/+-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function uniqStrings(values) {
  const seen = new Set();
  const out = [];
  (Array.isArray(values) ? values : []).forEach((value) => {
    const text = trimText(value);
    const key = normKey(text);
    if (!text || !key || seen.has(key)) return;
    seen.add(key);
    out.push(text);
  });
  return out;
}

function parseList(value) {
  return uniqStrings(
    String(value || "")
      .split(/[\n\r,]+/)
      .map((x) => x.trim())
      .filter(Boolean)
  );
}

function parseYear(value) {
  const text = trimText(value);
  if (!text) return null;
  const n = Number.parseInt(text, 10);
  return Number.isFinite(n) ? n : text;
}

function deepCopy(value) {
  return JSON.parse(JSON.stringify(value));
}

function moveInArray(list, index, delta) {
  const arr = Array.isArray(list) ? [...list] : [];
  const target = index + delta;
  if (index < 0 || index >= arr.length || target < 0 || target >= arr.length) return arr;
  const [row] = arr.splice(index, 1);
  arr.splice(target, 0, row);
  return arr;
}

function emptyReference() {
  return {
    ref_id: "",
    tipo: "",
    titulo: "",
    edicion: "",
    anio: "",
    organismo_o_fabricante: "",
    url: "",
    aplica_a: "",
  };
}

function emptyItem() {
  return {
    key: "",
    label: "",
    target: "",
    unit: "",
    ref_ids: [],
  };
}

function emptySection() {
  return {
    id: "",
    title: "",
    entry_mode: "",
    items: [emptyItem()],
  };
}

function emptyOverride() {
  return {
    name: "",
    active: true,
    priority: 0,
    match: {
      marca_contains: "",
      modelo_contains: "",
    },
    set_fields: {
      template_key: "",
      template_version: "",
      display_name: "",
      default_instrumentos: "",
      references: [],
      sections: [],
    },
    references: [],
    append_ref_to_all_items: "",
    item_ref_rows: [],
  };
}

function emptyDraft() {
  return {
    id: null,
    type_key: "",
    template_key: "",
    template_version: "1.0.0",
    display_name: "",
    default_instrumentos: "",
    active: true,
    aliases: [],
    references: [],
    sections: [emptySection()],
    overrides: [],
  };
}

function normalizeReference(ref) {
  return {
    ref_id: trimText(ref?.ref_id),
    tipo: trimText(ref?.tipo),
    titulo: trimText(ref?.titulo),
    edicion: trimText(ref?.edicion),
    anio: ref?.anio ?? "",
    organismo_o_fabricante: trimText(ref?.organismo_o_fabricante),
    url: trimText(ref?.url),
    aplica_a: trimText(ref?.aplica_a),
  };
}

function normalizeItem(item) {
  return {
    key: trimText(item?.key),
    label: trimText(item?.label),
    target: trimText(item?.target),
    unit: trimText(item?.unit),
    ref_ids: uniqStrings(item?.ref_ids),
  };
}

function normalizeSection(section) {
  const items = Array.isArray(section?.items) ? section.items.map(normalizeItem) : [];
  return {
    id: trimText(section?.id),
    title: trimText(section?.title),
    entry_mode: trimText(section?.entry_mode).toLowerCase(),
    items: items.length ? items : [emptyItem()],
  };
}

function itemRefRowsFromObject(itemRefIds) {
  if (!itemRefIds || typeof itemRefIds !== "object" || Array.isArray(itemRefIds)) return [];
  return Object.entries(itemRefIds).map(([itemKey, refs]) => ({
    item_key: trimText(itemKey),
    ref_ids: uniqStrings(refs),
  }));
}

function itemRefObjectFromRows(rows) {
  const out = {};
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const itemKey = trimText(row?.item_key);
    if (!itemKey) return;
    out[itemKey] = uniqStrings(row?.ref_ids);
  });
  return out;
}

function normalizeOverride(override) {
  const sf = override?.set_fields && typeof override.set_fields === "object" ? override.set_fields : {};
  return {
    name: trimText(override?.name),
    active: override?.active !== false,
    priority: Number.isFinite(Number(override?.priority)) ? Number.parseInt(override.priority, 10) : 0,
    match: {
      marca_contains: trimText(override?.match?.marca_contains),
      modelo_contains: trimText(override?.match?.modelo_contains),
    },
    set_fields: {
      template_key: trimText(sf?.template_key),
      template_version: trimText(sf?.template_version),
      display_name: trimText(sf?.display_name),
      default_instrumentos: trimText(sf?.default_instrumentos),
      references: Array.isArray(sf?.references) ? sf.references.map(normalizeReference) : [],
      sections: Array.isArray(sf?.sections) ? sf.sections.map(normalizeSection) : [],
    },
    references: Array.isArray(override?.references) ? override.references.map(normalizeReference) : [],
    append_ref_to_all_items: trimText(override?.append_ref_to_all_items),
    item_ref_rows: itemRefRowsFromObject(override?.item_ref_ids),
  };
}

function draftFromDetail(detail, currentId = null) {
  const data = detail && typeof detail === "object" ? detail : {};
  const sections = Array.isArray(data.sections) ? data.sections.map(normalizeSection) : [];
  return {
    id: data.id ?? currentId ?? null,
    type_key: trimText(data.type_key).toLowerCase(),
    template_key: trimText(data.template_key).toLowerCase(),
    template_version: trimText(data.template_version) || "1.0.0",
    display_name: trimText(data.display_name),
    default_instrumentos: trimText(data.default_instrumentos),
    active: data.active !== false,
    aliases: uniqStrings(data.aliases),
    references: Array.isArray(data.references) ? data.references.map(normalizeReference) : [],
    sections: sections.length ? sections : [emptySection()],
    overrides: Array.isArray(data.overrides) ? data.overrides.map(normalizeOverride) : [],
  };
}

function payloadFromDraft(draft) {
  return {
    type_key: trimText(draft.type_key).toLowerCase(),
    template_key: trimText(draft.template_key).toLowerCase(),
    template_version: trimText(draft.template_version),
    display_name: trimText(draft.display_name),
    default_instrumentos: trimText(draft.default_instrumentos),
    active: !!draft.active,
    aliases: uniqStrings(draft.aliases),
    references: (Array.isArray(draft.references) ? draft.references : []).map((ref) => ({
      ref_id: trimText(ref.ref_id),
      tipo: trimText(ref.tipo),
      titulo: trimText(ref.titulo),
      edicion: trimText(ref.edicion),
      anio: parseYear(ref.anio),
      organismo_o_fabricante: trimText(ref.organismo_o_fabricante),
      url: trimText(ref.url),
      aplica_a: trimText(ref.aplica_a),
    })),
    sections: (Array.isArray(draft.sections) ? draft.sections : []).map((section) => ({
      id: trimText(section.id),
      title: trimText(section.title),
      entry_mode: trimText(section.entry_mode).toLowerCase(),
      items: (Array.isArray(section.items) ? section.items : []).map((item) => ({
        key: trimText(item.key),
        label: trimText(item.label),
        target: trimText(item.target),
        unit: trimText(item.unit),
        ref_ids: uniqStrings(item.ref_ids),
      })),
    })),
    overrides: (Array.isArray(draft.overrides) ? draft.overrides : []).map((override) => {
      const out = {
        name: trimText(override.name),
        active: !!override.active,
        priority: Number.isFinite(Number(override.priority)) ? Number.parseInt(override.priority, 10) : 0,
        match: {
          marca_contains: trimText(override?.match?.marca_contains),
          modelo_contains: trimText(override?.match?.modelo_contains),
        },
        set_fields: {},
        references: (Array.isArray(override.references) ? override.references : []).map((ref) => ({
          ref_id: trimText(ref.ref_id),
          tipo: trimText(ref.tipo),
          titulo: trimText(ref.titulo),
          edicion: trimText(ref.edicion),
          anio: parseYear(ref.anio),
          organismo_o_fabricante: trimText(ref.organismo_o_fabricante),
          url: trimText(ref.url),
          aplica_a: trimText(ref.aplica_a),
        })),
        append_ref_to_all_items: trimText(override.append_ref_to_all_items),
        item_ref_ids: itemRefObjectFromRows(override.item_ref_rows),
      };
      const sf = override?.set_fields || {};
      if (trimText(sf.template_key)) out.set_fields.template_key = trimText(sf.template_key);
      if (trimText(sf.template_version)) out.set_fields.template_version = trimText(sf.template_version);
      if (trimText(sf.display_name)) out.set_fields.display_name = trimText(sf.display_name);
      if (trimText(sf.default_instrumentos)) out.set_fields.default_instrumentos = trimText(sf.default_instrumentos);
      if (Array.isArray(sf.references) && sf.references.length) {
        out.set_fields.references = sf.references.map((ref) => ({
          ref_id: trimText(ref.ref_id),
          tipo: trimText(ref.tipo),
          titulo: trimText(ref.titulo),
          edicion: trimText(ref.edicion),
          anio: parseYear(ref.anio),
          organismo_o_fabricante: trimText(ref.organismo_o_fabricante),
          url: trimText(ref.url),
          aplica_a: trimText(ref.aplica_a),
        }));
      }
      if (Array.isArray(sf.sections) && sf.sections.length) {
        out.set_fields.sections = sf.sections.map((section) => ({
          id: trimText(section.id),
          title: trimText(section.title),
          entry_mode: trimText(section.entry_mode).toLowerCase(),
          items: (Array.isArray(section.items) ? section.items : []).map((item) => ({
            key: trimText(item.key),
            label: trimText(item.label),
            target: trimText(item.target),
            unit: trimText(item.unit),
            ref_ids: uniqStrings(item.ref_ids),
          })),
        }));
      }
      return out;
    }),
  };
}

function validateSections(sections, validRefIds, itemKeyGlobal, pathPrefix) {
  const errors = [];
  const sectionIds = new Set();
  const localItemKeys = new Set();

  (Array.isArray(sections) ? sections : []).forEach((section, sectionIndex) => {
    const basePath = `${pathPrefix}[${sectionIndex}]`;
    const secId = trimText(section?.id);
    const title = trimText(section?.title);
    const entryMode = trimText(section?.entry_mode).toLowerCase();

    if (!secId) errors.push(`${basePath}.id es requerido`);
    if (secId && sectionIds.has(secId)) errors.push(`${pathPrefix} tiene id duplicado: ${secId}`);
    if (secId) sectionIds.add(secId);

    if (!title) errors.push(`${basePath}.title es requerido`);
    if (!VALID_ENTRY_MODES.has(entryMode)) errors.push(`${basePath}.entry_mode inválido`);

    if (!Array.isArray(section?.items) || section.items.length === 0) {
      errors.push(`${basePath}.items debe tener al menos un item`);
      return;
    }

    section.items.forEach((item, itemIndex) => {
      const itemPath = `${basePath}.items[${itemIndex}]`;
      const itemKey = trimText(item?.key);
      const itemLabel = trimText(item?.label);
      const refIds = uniqStrings(item?.ref_ids);

      if (!itemKey) errors.push(`${itemPath}.key es requerido`);
      if (itemKey) {
        if (localItemKeys.has(itemKey)) errors.push(`key de item duplicada en ${pathPrefix}: ${itemKey}`);
        localItemKeys.add(itemKey);
        if (itemKeyGlobal) {
          if (itemKeyGlobal.has(itemKey)) errors.push(`key de item duplicada global: ${itemKey}`);
          itemKeyGlobal.add(itemKey);
        }
      }
      if (!itemLabel) errors.push(`${itemPath}.label es requerido`);

      refIds.forEach((refId) => {
        if (validRefIds && validRefIds.size > 0 && !validRefIds.has(refId)) {
          errors.push(`${itemPath}.ref_ids contiene ref inexistente: ${refId}`);
        }
      });
    });
  });

  return errors;
}

function validatePayload(payload) {
  const errors = [];
  const typeKey = trimText(payload?.type_key).toLowerCase();
  const templateKey = trimText(payload?.template_key).toLowerCase();

  if (!typeKey) errors.push("type_key es requerido");
  if (typeKey && !TYPE_KEY_RE.test(typeKey)) errors.push("type_key inválido (solo a-z, 0-9 y _)");
  if (!templateKey) errors.push("template_key es requerido");
  if (templateKey && !TEMPLATE_KEY_RE.test(templateKey)) {
    errors.push("template_key inválido (solo a-z, 0-9 y _)");
  }
  if (!trimText(payload?.template_version)) errors.push("template_version es requerido");
  if (!trimText(payload?.display_name)) errors.push("display_name es requerido");

  const references = Array.isArray(payload?.references) ? payload.references : [];
  const refIds = new Set();
  references.forEach((ref, index) => {
    const refId = trimText(ref?.ref_id);
    if (!refId) {
      errors.push(`references[${index}].ref_id es requerido`);
      return;
    }
    if (refIds.has(refId)) errors.push(`references tiene ref_id duplicado: ${refId}`);
    refIds.add(refId);
  });

  if (!Array.isArray(payload?.sections) || payload.sections.length === 0) {
    errors.push("sections debe tener al menos una seccion");
  } else {
    const itemKeyGlobal = new Set();
    errors.push(...validateSections(payload.sections, refIds, itemKeyGlobal, "sections"));
  }

  if (!Array.isArray(payload?.overrides)) {
    errors.push("overrides debe ser lista");
  } else {
    payload.overrides.forEach((override, overrideIndex) => {
      const path = `overrides[${overrideIndex}]`;
      const match = override?.match && typeof override.match === "object" ? override.match : null;
      if (!match) {
        errors.push(`${path}.match debe ser objeto`);
        return;
      }
      const marca = trimText(match.marca_contains);
      const modelo = trimText(match.modelo_contains);
      if (!marca && !modelo) errors.push(`${path} requiere marca_contains o modelo_contains`);
      if (!Number.isInteger(Number(override?.priority))) errors.push(`${path}.priority debe ser entero`);

      const setFields = override?.set_fields && typeof override.set_fields === "object" ? override.set_fields : {};
      const validRefIds = new Set([...refIds]);

      const mergedRefs = [
        ...(Array.isArray(override?.references) ? override.references : []),
        ...(Array.isArray(setFields.references) ? setFields.references : []),
      ];

      mergedRefs.forEach((ref, refIndex) => {
        const refId = trimText(ref?.ref_id);
        if (!refId) {
          errors.push(`${path}.references/set_fields.references[${refIndex}].ref_id es requerido`);
          return;
        }
        validRefIds.add(refId);
      });

      const appendRef = trimText(override?.append_ref_to_all_items);
      if (appendRef && !validRefIds.has(appendRef)) {
        errors.push(`${path}.append_ref_to_all_items no existe en referencias`);
      }

      if (setFields.sections) {
        if (!Array.isArray(setFields.sections) || setFields.sections.length === 0) {
          errors.push(`${path}.set_fields.sections debe tener al menos una seccion`);
        } else {
          errors.push(...validateSections(setFields.sections, validRefIds, null, `${path}.set_fields.sections`));
        }
      }

      const itemRefIds =
        override?.item_ref_ids && typeof override.item_ref_ids === "object" && !Array.isArray(override.item_ref_ids)
          ? override.item_ref_ids
          : {};

      Object.entries(itemRefIds).forEach(([itemKey, refs]) => {
        const key = trimText(itemKey);
        if (!key) return;
        if (!Array.isArray(refs)) {
          errors.push(`${path}.item_ref_ids[${key}] debe ser lista`);
          return;
        }
        uniqStrings(refs).forEach((refId) => {
          if (!validRefIds.has(refId)) {
            errors.push(`${path}.item_ref_ids[${key}] usa ref inexistente: ${refId}`);
          }
        });
      });
    });
  }

  return errors;
}

function payloadKeyFromDraft(draft) {
  return JSON.stringify(payloadFromDraft(draft));
}

function Field({ label, children }) {
  return (
    <label className="block">
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-500">{label}</div>
      {children}
    </label>
  );
}
function ReferencesEditor({ title, references, onChange }) {
  const rows = Array.isArray(references) ? references : [];

  const setCell = (index, field, value) => {
    onChange(rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  };

  const addRow = () => onChange([...rows, emptyReference()]);
  const removeRow = (index) => onChange(rows.filter((_, i) => i !== index));

  return (
    <div className="space-y-2 rounded border p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-semibold">{title}</div>
        <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={addRow}>
          Agregar referencia
        </button>
      </div>
      {!rows.length ? <div className="text-xs text-gray-500">Sin referencias.</div> : null}

      {rows.map((ref, index) => (
        <div key={`${title}-ref-${index}`} className="rounded border p-2">
          <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
            <Field label="ref_id">
              <input className="w-full rounded border p-2" value={ref.ref_id || ""} onChange={(e) => setCell(index, "ref_id", e.target.value)} />
            </Field>
            <Field label="tipo">
              <input className="w-full rounded border p-2" value={ref.tipo || ""} onChange={(e) => setCell(index, "tipo", e.target.value)} />
            </Field>
            <Field label="anio">
              <input className="w-full rounded border p-2" value={ref.anio ?? ""} onChange={(e) => setCell(index, "anio", e.target.value)} />
            </Field>
            <Field label="edicion">
              <input className="w-full rounded border p-2" value={ref.edicion || ""} onChange={(e) => setCell(index, "edicion", e.target.value)} />
            </Field>
          </div>

          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
            <Field label="titulo">
              <input className="w-full rounded border p-2" value={ref.titulo || ""} onChange={(e) => setCell(index, "titulo", e.target.value)} />
            </Field>
            <Field label="organismo_o_fabricante">
              <input className="w-full rounded border p-2" value={ref.organismo_o_fabricante || ""} onChange={(e) => setCell(index, "organismo_o_fabricante", e.target.value)} />
            </Field>
          </div>

          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
            <Field label="url">
              <input className="w-full rounded border p-2" value={ref.url || ""} onChange={(e) => setCell(index, "url", e.target.value)} />
            </Field>
            <Field label="aplica_a">
              <input className="w-full rounded border p-2" value={ref.aplica_a || ""} onChange={(e) => setCell(index, "aplica_a", e.target.value)} />
            </Field>
          </div>

          <div className="mt-2">
            <button type="button" className="rounded bg-red-600 px-2 py-1 text-xs text-white hover:bg-red-700" onClick={() => removeRow(index)}>
              Quitar referencia
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function SectionsEditor({ title, sections, onChange }) {
  const rows = Array.isArray(sections) ? sections : [];

  const setSectionField = (sectionIndex, field, value) => {
    onChange(rows.map((section, i) => (i === sectionIndex ? { ...section, [field]: value } : section)));
  };

  const addSection = () => onChange([...rows, emptySection()]);
  const removeSection = (sectionIndex) => onChange(rows.filter((_, i) => i !== sectionIndex));
  const moveSection = (sectionIndex, delta) => onChange(moveInArray(rows, sectionIndex, delta));

  const setItemField = (sectionIndex, itemIndex, field, value) => {
    const next = deepCopy(rows);
    next[sectionIndex].items[itemIndex][field] = value;
    onChange(next);
  };

  const addItem = (sectionIndex) => {
    const next = deepCopy(rows);
    next[sectionIndex].items = [...(next[sectionIndex].items || []), emptyItem()];
    onChange(next);
  };

  const removeItem = (sectionIndex, itemIndex) => {
    const next = deepCopy(rows);
    next[sectionIndex].items = (next[sectionIndex].items || []).filter((_, i) => i !== itemIndex);
    if (!next[sectionIndex].items.length) next[sectionIndex].items = [emptyItem()];
    onChange(next);
  };

  const moveItem = (sectionIndex, itemIndex, delta) => {
    const next = deepCopy(rows);
    next[sectionIndex].items = moveInArray(next[sectionIndex].items || [], itemIndex, delta);
    onChange(next);
  };

  return (
    <div className="space-y-3 rounded border p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-semibold">{title}</div>
        <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={addSection}>
          Agregar seccion
        </button>
      </div>
      {!rows.length ? <div className="text-xs text-gray-500">Sin secciones.</div> : null}

      {rows.map((section, sectionIndex) => (
        <div key={`${title}-section-${sectionIndex}`} className="rounded border p-3">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">Seccion #{sectionIndex + 1}</div>
            <div className="flex gap-1">
              <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => moveSection(sectionIndex, -1)} disabled={sectionIndex === 0}>Subir</button>
              <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => moveSection(sectionIndex, 1)} disabled={sectionIndex >= rows.length - 1}>Bajar</button>
              <button type="button" className="rounded bg-red-600 px-2 py-1 text-xs text-white hover:bg-red-700" onClick={() => removeSection(sectionIndex)}>Quitar</button>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
            <Field label="id">
              <input className="w-full rounded border p-2" value={section.id || ""} onChange={(e) => setSectionField(sectionIndex, "id", e.target.value)} />
            </Field>
            <Field label="title">
              <input className="w-full rounded border p-2" value={section.title || ""} onChange={(e) => setSectionField(sectionIndex, "title", e.target.value)} />
            </Field>
            <Field label="entry_mode">
              <select className="w-full rounded border p-2" value={section.entry_mode || ""} onChange={(e) => setSectionField(sectionIndex, "entry_mode", e.target.value)}>
                <option value="">(vacio)</option>
                <option value="result_only">result_only</option>
                <option value="measured_only">measured_only</option>
              </select>
            </Field>
          </div>

          <div className="mt-3 overflow-x-auto">
            <table className="min-w-full border text-sm">
              <thead className="bg-gray-100">
                <tr className="text-left">
                  <th className="border p-2">key</th>
                  <th className="border p-2">label</th>
                  <th className="border p-2">target</th>
                  <th className="border p-2">unit</th>
                  <th className="border p-2">ref_ids (coma)</th>
                  <th className="border p-2">Acciones</th>
                </tr>
              </thead>
              <tbody>
                {(Array.isArray(section.items) ? section.items : []).map((item, itemIndex) => (
                  <tr key={`${title}-section-${sectionIndex}-item-${itemIndex}`}>
                    <td className="border p-2"><input className="w-full rounded border p-1" value={item.key || ""} onChange={(e) => setItemField(sectionIndex, itemIndex, "key", e.target.value)} /></td>
                    <td className="border p-2"><input className="w-full rounded border p-1" value={item.label || ""} onChange={(e) => setItemField(sectionIndex, itemIndex, "label", e.target.value)} /></td>
                    <td className="border p-2"><input className="w-full rounded border p-1" value={item.target || ""} onChange={(e) => setItemField(sectionIndex, itemIndex, "target", e.target.value)} /></td>
                    <td className="border p-2"><input className="w-full rounded border p-1" value={item.unit || ""} onChange={(e) => setItemField(sectionIndex, itemIndex, "unit", e.target.value)} /></td>
                    <td className="border p-2"><input className="w-full rounded border p-1" value={(item.ref_ids || []).join(", ")} onChange={(e) => setItemField(sectionIndex, itemIndex, "ref_ids", parseList(e.target.value))} /></td>
                    <td className="border p-2">
                      <div className="flex flex-wrap gap-1">
                        <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => moveItem(sectionIndex, itemIndex, -1)} disabled={itemIndex === 0}>Subir</button>
                        <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => moveItem(sectionIndex, itemIndex, 1)} disabled={itemIndex >= (section.items || []).length - 1}>Bajar</button>
                        <button type="button" className="rounded bg-red-600 px-2 py-1 text-xs text-white hover:bg-red-700" onClick={() => removeItem(sectionIndex, itemIndex)}>Quitar</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-2">
            <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => addItem(sectionIndex)}>
              Agregar item
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function ItemRefRowsEditor({ rows, onChange }) {
  const list = Array.isArray(rows) ? rows : [];

  const updateRow = (index, patch) => {
    onChange(list.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  };

  const addRow = () => onChange([...list, { item_key: "", ref_ids: [] }]);
  const removeRow = (index) => onChange(list.filter((_, i) => i !== index));

  return (
    <div className="rounded border p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-semibold">item_ref_ids</div>
        <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={addRow}>
          Agregar fila
        </button>
      </div>
      {!list.length ? <div className="text-xs text-gray-500">Sin filas.</div> : null}

      {list.map((row, index) => (
        <div key={`item-ref-row-${index}`} className="mb-2 grid grid-cols-1 gap-2 rounded border p-2 md:grid-cols-3">
          <Field label="item_key">
            <input className="w-full rounded border p-2" value={row.item_key || ""} onChange={(e) => updateRow(index, { item_key: e.target.value })} />
          </Field>
          <Field label="ref_ids (coma)">
            <input className="w-full rounded border p-2" value={(row.ref_ids || []).join(", ")} onChange={(e) => updateRow(index, { ref_ids: parseList(e.target.value) })} />
          </Field>
          <div className="flex items-end">
            <button type="button" className="rounded bg-red-600 px-2 py-2 text-xs text-white hover:bg-red-700" onClick={() => removeRow(index)}>
              Quitar
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
export default function ProtocolosTest() {
  const [rows, setRows] = useState([]);
  const [draft, setDraft] = useState(emptyDraft());
  const [activeTab, setActiveTab] = useState("visual");
  const [aliasInput, setAliasInput] = useState("");
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState("");
  const [loadingList, setLoadingList] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [validationErrors, setValidationErrors] = useState([]);
  const [baselineKey, setBaselineKey] = useState(payloadKeyFromDraft(emptyDraft()));

  const selectedId = draft.id;
  const rowCount = useMemo(() => (Array.isArray(rows) ? rows.length : 0), [rows]);
  const hasUnsavedChanges = useMemo(() => payloadKeyFromDraft(draft) !== baselineKey, [draft, baselineKey]);

  useEffect(() => {
    const onBeforeUnload = (event) => {
      if (!hasUnsavedChanges) return undefined;
      event.preventDefault();
      event.returnValue = "";
      return "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasUnsavedChanges]);

  async function loadList() {
    try {
      setLoadingList(true);
      const data = await getTestProtocols();
      setRows(Array.isArray(data) ? data : []);
    } catch (error) {
      setErr(error?.message || "No se pudo cargar el catalogo de protocolos");
      setRows([]);
    } finally {
      setLoadingList(false);
    }
  }

  async function loadDetail(protocolId) {
    if (!protocolId) return;
    try {
      setLoadingDetail(true);
      setErr("");
      setMsg("");
      const detail = await getTestProtocol(protocolId);
      const next = draftFromDetail(detail, protocolId);
      setDraft(next);
      setBaselineKey(payloadKeyFromDraft(next));
      setAliasInput("");
      setJsonError("");
      setValidationErrors([]);
    } catch (error) {
      setErr(error?.message || "No se pudo cargar el detalle del protocolo");
    } finally {
      setLoadingDetail(false);
    }
  }

  useEffect(() => {
    void loadList();
  }, []);

  function confirmDiscardChanges() {
    if (!hasUnsavedChanges) return true;
    return window.confirm("Hay cambios sin guardar. Deseas descartarlos?");
  }

  function startNew() {
    if (!confirmDiscardChanges()) return;
    const next = emptyDraft();
    setDraft(next);
    setBaselineKey(payloadKeyFromDraft(next));
    setAliasInput("");
    setJsonError("");
    setValidationErrors([]);
    setErr("");
    setMsg("");
  }

  async function selectProtocol(protocolId) {
    if (!protocolId || Number(protocolId) === Number(selectedId || 0)) return;
    if (!confirmDiscardChanges()) return;
    await loadDetail(protocolId);
  }

  function updateDraft(mutator) {
    setDraft((prev) => {
      const next = deepCopy(prev);
      mutator(next);
      return next;
    });
    setValidationErrors([]);
    setErr("");
    setMsg("");
  }

  function addAliasFromInput() {
    const aliases = parseList(aliasInput);
    if (!aliases.length) return;
    updateDraft((next) => {
      next.aliases = uniqStrings([...(next.aliases || []), ...aliases]);
    });
    setAliasInput("");
  }

  function removeAlias(alias) {
    updateDraft((next) => {
      next.aliases = (next.aliases || []).filter((item) => item !== alias);
    });
  }

  function openJsonTab() {
    setActiveTab("json");
    setJsonError("");
    setJsonText(JSON.stringify(payloadFromDraft(draft), null, 2));
  }

  function applyJson() {
    try {
      const parsed = JSON.parse(jsonText || "{}");
      const nextDraft = draftFromDetail(parsed, selectedId);
      const payload = payloadFromDraft(nextDraft);
      const errors = validatePayload(payload);
      if (errors.length) {
        setValidationErrors(errors);
        setJsonError("El JSON es válido pero no cumple validaciones.");
        return;
      }
      setDraft(nextDraft);
      setValidationErrors([]);
      setJsonError("");
      setMsg("JSON aplicado al editor visual.");
      setActiveTab("visual");
    } catch (_) {
      setJsonError("JSON inválido.");
    }
  }

  async function save() {
    try {
      setSaving(true);
      setErr("");
      setMsg("");
      const payload = payloadFromDraft(draft);
      const errors = validatePayload(payload);
      if (errors.length) {
        setValidationErrors(errors);
        setErr("Hay errores en los datos. Revise los campos antes de guardar.");
        return;
      }
      setValidationErrors([]);

      if (selectedId) {
        const updated = await patchTestProtocol(selectedId, payload);
        const next = draftFromDetail(updated || payload, selectedId);
        setDraft(next);
        setBaselineKey(payloadKeyFromDraft(next));
        setMsg("Protocolo actualizado.");
      } else {
        const created = await postTestProtocol(payload);
        const createdId = created?.id;
        const next = draftFromDetail(created || payload, createdId || null);
        setDraft(next);
        setBaselineKey(payloadKeyFromDraft(next));
        setMsg("Protocolo creado.");
      }

      await loadList();
    } catch (error) {
      setErr(error?.message || "No se pudo guardar el protocolo");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!selectedId) return;
    if (!window.confirm("Desactivar protocolo?")) return;
    try {
      setDeleting(true);
      setErr("");
      setMsg("");
      await deleteTestProtocol(selectedId);
      setMsg("Protocolo desactivado.");
      await loadList();
      const next = emptyDraft();
      setDraft(next);
      setBaselineKey(payloadKeyFromDraft(next));
    } catch (error) {
      setErr(error?.message || "No se pudo desactivar el protocolo");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold">Protocolos de test</h1>
        <p className="text-sm text-gray-600">Editor visual de protocolos con validaciones antes de guardar.</p>
      </div>

      {err ? <div className="rounded border border-red-300 bg-red-100 p-2 text-red-700">{err}</div> : null}
      {msg ? <div className="rounded border border-green-300 bg-green-100 p-2 text-green-700">{msg}</div> : null}

      {validationErrors.length ? (
        <div className="rounded border border-amber-300 bg-amber-100 p-3 text-amber-800">
          <div className="mb-1 text-sm font-semibold">Errores en los datos</div>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {validationErrors.map((validationError, index) => (
              <li key={`validation-${index}`}>{validationError}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded border p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-semibold">Catalogo ({rowCount})</div>
            <button type="button" className="rounded border px-2 py-1 text-sm hover:bg-gray-50" onClick={startNew}>
              Nuevo
            </button>
          </div>

          {loadingList ? (
            <div className="text-sm text-gray-500">Cargando...</div>
          ) : (
            <div className="max-h-[70vh] space-y-2 overflow-auto pr-1">
              {(rows || []).map((row) => {
                const selected = Number(selectedId || 0) === Number(row?.id || 0);
                const active = !!row?.active;
                return (
                  <button
                    key={row?.id}
                    type="button"
                    className={`w-full rounded border p-2 text-left text-sm ${selected ? "border-blue-500 bg-blue-50" : "hover:bg-gray-50"}`}
                    onClick={() => selectProtocol(row?.id)}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-medium">{row?.display_name || row?.type_key || "-"}</div>
                      <span className={`rounded px-2 py-0.5 text-xs ${active ? "bg-green-100 text-green-700" : "bg-gray-200 text-gray-700"}`}>
                        {active ? "Activo" : "Inactivo"}
                      </span>
                    </div>
                    <div className="text-xs text-gray-600">{row?.type_key || "-"}</div>
                    <div className="text-xs text-gray-500">{row?.template_key || "-"}</div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="space-y-3 rounded border p-3 lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">{selectedId ? `Editar protocolo #${selectedId}` : "Nuevo protocolo"}</div>
            <div className="flex items-center gap-2 text-xs">
              {loadingDetail ? <span className="text-gray-500">Cargando detalle...</span> : null}
              <span className={`rounded px-2 py-1 ${hasUnsavedChanges ? "bg-amber-100 text-amber-700" : "bg-gray-100 text-gray-600"}`}>
                {hasUnsavedChanges ? "Cambios sin guardar" : "Sin cambios pendientes"}
              </span>
            </div>
          </div>

          <div className="flex gap-2 border-b pb-2">
            <button type="button" className={`rounded px-3 py-1 text-sm ${activeTab === "visual" ? "bg-blue-600 text-white" : "border hover:bg-gray-50"}`} onClick={() => setActiveTab("visual")}>Editor visual</button>
            <button type="button" className={`rounded px-3 py-1 text-sm ${activeTab === "json" ? "bg-blue-600 text-white" : "border hover:bg-gray-50"}`} onClick={openJsonTab}>JSON avanzado</button>
          </div>

          {activeTab === "visual" ? (
            <div className="space-y-4">
              <div className="rounded border p-3">
                <div className="mb-2 text-sm font-semibold">Datos generales</div>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  <Field label="type_key"><input className="w-full rounded border p-2" value={draft.type_key} onChange={(e) => updateDraft((n) => { n.type_key = e.target.value; })} /></Field>
                  <Field label="template_key"><input className="w-full rounded border p-2" value={draft.template_key} onChange={(e) => updateDraft((n) => { n.template_key = e.target.value; })} /></Field>
                  <Field label="template_version"><input className="w-full rounded border p-2" value={draft.template_version} onChange={(e) => updateDraft((n) => { n.template_version = e.target.value; })} /></Field>
                  <Field label="display_name"><input className="w-full rounded border p-2" value={draft.display_name} onChange={(e) => updateDraft((n) => { n.display_name = e.target.value; })} /></Field>
                </div>
                <div className="mt-3"><Field label="default_instrumentos"><textarea className="min-h-[72px] w-full rounded border p-2" value={draft.default_instrumentos} onChange={(e) => updateDraft((n) => { n.default_instrumentos = e.target.value; })} /></Field></div>
                <div className="mt-3"><label className="inline-flex items-center gap-2 text-sm"><input type="checkbox" checked={!!draft.active} onChange={(e) => updateDraft((n) => { n.active = !!e.target.checked; })} />Activo</label></div>
              </div>

              <div className="rounded border p-3">
                <div className="mb-2 text-sm font-semibold">Aliases</div>
                <div className="flex gap-2">
                  <input
                    className="w-full rounded border p-2"
                    value={aliasInput}
                    placeholder="Alias nuevo (Enter o coma para agregar)"
                    onChange={(e) => setAliasInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === ",") {
                        e.preventDefault();
                        addAliasFromInput();
                      }
                    }}
                  />
                  <button type="button" className="rounded border px-3 py-2 hover:bg-gray-50" onClick={addAliasFromInput}>Agregar</button>
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {(draft.aliases || []).map((alias) => (
                    <span key={alias} className="inline-flex items-center gap-1 rounded bg-gray-100 px-2 py-1 text-sm">
                      {alias}
                      <button type="button" className="rounded border px-1 text-xs hover:bg-red-100" onClick={() => removeAlias(alias)}>x</button>
                    </span>
                  ))}
                  {!draft.aliases.length ? <span className="text-xs text-gray-500">Sin aliases.</span> : null}
                </div>
              </div>

              <ReferencesEditor title="References" references={draft.references} onChange={(nextRefs) => updateDraft((next) => { next.references = nextRefs; })} />

              <SectionsEditor title="Sections" sections={draft.sections} onChange={(nextSections) => updateDraft((next) => { next.sections = nextSections; })} />

              <div className="space-y-3 rounded border p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-sm font-semibold">Overrides</div>
                  <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => updateDraft((n) => { n.overrides = [...(n.overrides || []), emptyOverride()]; })}>Agregar override</button>
                </div>

                {!draft.overrides.length ? <div className="text-xs text-gray-500">Sin overrides.</div> : null}

                {(draft.overrides || []).map((override, overrideIndex) => (
                  <div key={`override-${overrideIndex}`} className="rounded border p-3">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <div className="text-sm font-semibold">Override #{overrideIndex + 1}</div>
                      <div className="flex gap-1">
                        <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => updateDraft((n) => { n.overrides = moveInArray(n.overrides || [], overrideIndex, -1); })} disabled={overrideIndex === 0}>Subir</button>
                        <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-gray-50" onClick={() => updateDraft((n) => { n.overrides = moveInArray(n.overrides || [], overrideIndex, 1); })} disabled={overrideIndex >= (draft.overrides || []).length - 1}>Bajar</button>
                        <button type="button" className="rounded bg-red-600 px-2 py-1 text-xs text-white hover:bg-red-700" onClick={() => updateDraft((n) => { n.overrides = (n.overrides || []).filter((_, i) => i !== overrideIndex); })}>Quitar</button>
                      </div>
                    </div>

                    <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                      <Field label="name"><input className="w-full rounded border p-2" value={override.name || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].name = e.target.value; })} /></Field>
                      <Field label="priority"><input className="w-full rounded border p-2" value={override.priority ?? 0} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].priority = e.target.value; })} /></Field>
                      <div className="flex items-end"><label className="inline-flex items-center gap-2 text-sm"><input type="checkbox" checked={!!override.active} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].active = !!e.target.checked; })} />Activo</label></div>
                    </div>

                    <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
                      <Field label="match.marca_contains"><input className="w-full rounded border p-2" value={override?.match?.marca_contains || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].match.marca_contains = e.target.value; })} /></Field>
                      <Field label="match.modelo_contains"><input className="w-full rounded border p-2" value={override?.match?.modelo_contains || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].match.modelo_contains = e.target.value; })} /></Field>
                    </div>

                    <div className="mt-3 rounded border p-3">
                      <div className="mb-2 text-sm font-semibold">set_fields</div>
                      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                        <Field label="template_key"><input className="w-full rounded border p-2" value={override?.set_fields?.template_key || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].set_fields.template_key = e.target.value; })} /></Field>
                        <Field label="template_version"><input className="w-full rounded border p-2" value={override?.set_fields?.template_version || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].set_fields.template_version = e.target.value; })} /></Field>
                        <Field label="display_name"><input className="w-full rounded border p-2" value={override?.set_fields?.display_name || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].set_fields.display_name = e.target.value; })} /></Field>
                        <Field label="default_instrumentos"><textarea className="min-h-[60px] w-full rounded border p-2" value={override?.set_fields?.default_instrumentos || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].set_fields.default_instrumentos = e.target.value; })} /></Field>
                      </div>

                      <div className="mt-3 space-y-3">
                        <ReferencesEditor title="set_fields.references" references={override?.set_fields?.references || []} onChange={(nextRefs) => updateDraft((n) => { n.overrides[overrideIndex].set_fields.references = nextRefs; })} />
                        <SectionsEditor title="set_fields.sections" sections={override?.set_fields?.sections || []} onChange={(nextSections) => updateDraft((n) => { n.overrides[overrideIndex].set_fields.sections = nextSections; })} />
                      </div>
                    </div>

                    <div className="mt-3">
                      <ReferencesEditor title="override.references" references={override.references || []} onChange={(nextRefs) => updateDraft((n) => { n.overrides[overrideIndex].references = nextRefs; })} />
                    </div>

                    <div className="mt-3">
                      <Field label="append_ref_to_all_items"><input className="w-full rounded border p-2" value={override.append_ref_to_all_items || ""} onChange={(e) => updateDraft((n) => { n.overrides[overrideIndex].append_ref_to_all_items = e.target.value; })} /></Field>
                    </div>

                    <div className="mt-3">
                      <ItemRefRowsEditor rows={override.item_ref_rows || []} onChange={(nextRows) => updateDraft((n) => { n.overrides[overrideIndex].item_ref_rows = nextRows; })} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="space-y-3 rounded border p-3">
              <div className="text-sm font-semibold">JSON avanzado</div>
              <p className="text-xs text-gray-600">Snapshot del borrador actual para casos avanzados.</p>
              <textarea className="min-h-[420px] w-full rounded border p-2 font-mono text-xs" value={jsonText} onChange={(e) => setJsonText(e.target.value)} />
              {jsonError ? <div className="text-sm text-red-700">{jsonError}</div> : null}
              <div className="flex flex-wrap gap-2">
                <button type="button" className="rounded border px-3 py-2 hover:bg-gray-50" onClick={() => setJsonText(JSON.stringify(payloadFromDraft(draft), null, 2))}>Recargar snapshot</button>
                <button type="button" className="rounded bg-blue-600 px-3 py-2 text-white hover:bg-blue-700" onClick={applyJson}>Aplicar JSON</button>
              </div>
            </div>
          )}

          <div className="flex flex-wrap gap-2 pt-2">
            <button type="button" className="rounded bg-blue-600 px-3 py-2 text-white hover:bg-blue-700 disabled:opacity-60" onClick={save} disabled={saving || deleting}>
              {saving ? "Guardando..." : "Guardar cambios"}
            </button>
            <button type="button" className="rounded border px-3 py-2 hover:bg-gray-50" onClick={startNew} disabled={saving || deleting}>Descartar y nuevo</button>
            {selectedId ? (
              <button type="button" className="rounded bg-red-600 px-3 py-2 text-white hover:bg-red-700 disabled:opacity-60" onClick={remove} disabled={saving || deleting}>
                {deleting ? "Desactivando..." : "Desactivar"}
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
