import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Link2,
  Loader2,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import {
  deleteCliente,
  getClienteBejermanCandidates,
  getClientes,
  patchCliente,
  postClienteBejermanSync,
  postCliente,
  postClienteMerge,
} from "../lib/api";

const BLANK_CUSTOMER = {
  razon_social: "",
  cod_empresa: "",
  alias_interno: "",
  cuit: "",
  contacto: "",
  telefono: "",
  telefono_2: "",
  email: "",
};

const SYNC_META = {
  synced: { label: "Sincronizado", cls: "border-emerald-200 bg-emerald-50 text-emerald-800" },
  review: { label: "Revisar", cls: "border-amber-200 bg-amber-50 text-amber-800" },
  code_mismatch: { label: "Código posible", cls: "border-amber-200 bg-amber-50 text-amber-800" },
  not_found: { label: "Código no encontrado", cls: "border-red-200 bg-red-50 text-red-800" },
  missing_code: { label: "Sin código", cls: "border-sky-200 bg-sky-50 text-sky-800" },
  unlinked: { label: "Sin vincular", cls: "border-gray-200 bg-gray-50 text-gray-700" },
  unavailable: { label: "Bejerman no disponible", cls: "border-gray-200 bg-gray-50 text-gray-700" },
  unchecked: { label: "Sin verificar", cls: "border-gray-200 bg-gray-50 text-gray-700" },
};

const BEJERMAN_DETAIL_FIELDS = [
  ["condicionIva", "Cond. IVA"],
  ["numeroIibb", "IIBB"],
  ["condicionVenta", "Cond. venta"],
  ["provincia", "Provincia"],
  ["localidad", "Localidad"],
  ["domicilio", "Domicilio"],
  ["codigoPostal", "CP"],
  ["pais", "País"],
  ["vendedor", "Vendedor"],
  ["listaPrecio", "Lista"],
  ["nombreFantasia", "Nombre fantasía"],
  ["tipoDocumento", "Tipo doc."],
  ["contacto", "Contacto Bejerman"],
  ["telefono", "Tel. Bejerman"],
  ["telefono2", "Tel. 2 Bejerman"],
  ["email", "Email Bejerman"],
];

const Input = ({ className = "", ...props }) => (
  <input
    {...props}
    className={`h-9 w-full rounded border border-gray-300 px-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 ${className}`}
  />
);

const Label = ({ children }) => (
  <span className="mb-1 block text-xs font-medium uppercase text-gray-500">{children}</span>
);

function clean(value) {
  return String(value ?? "").trim();
}

function normalize(value) {
  return clean(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function candidateLabel(candidate) {
  const code = clean(candidate?.code);
  const name = clean(candidate?.name);
  if (code && name) return `${code} - ${name}`;
  return code || name || "-";
}

function bejermanDetails(source) {
  return source?.bejerman_details || source?.details || {};
}

function detailValue(details, key) {
  return clean(details?.[key]);
}

function visibleDetailFields(details, limit = BEJERMAN_DETAIL_FIELDS.length) {
  return BEJERMAN_DETAIL_FIELDS
    .map(([key, label]) => ({ key, label, value: detailValue(details, key) }))
    .filter((item) => item.value)
    .slice(0, limit);
}

function rawEntries(details) {
  const raw = details?.raw && typeof details.raw === "object" && !Array.isArray(details.raw) ? details.raw : {};
  return Object.entries(raw)
    .filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== "")
    .sort(([a], [b]) => a.localeCompare(b));
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return clean(value);
  return date.toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" });
}

function BejermanDetailChips({ details, limit = 4 }) {
  const items = visibleDetailFields(details, limit);
  if (!items.length) return null;
  return (
    <div className="mt-2 flex max-w-[420px] flex-wrap gap-1 text-[11px] text-gray-700">
      {items.map((item) => (
        <span key={item.key} className="rounded border border-gray-200 bg-white px-1.5 py-0.5">
          {item.label}: {item.value}
        </span>
      ))}
    </div>
  );
}

function BejermanDetailsPanel({ customer }) {
  const details = bejermanDetails(customer);
  const fields = visibleDetailFields(details);
  const raw = rawEntries(details);
  if (!fields.length && !raw.length) return null;
  return (
    <section className="mt-4 rounded border border-gray-200 bg-gray-50">
      <div className="flex flex-col gap-1 border-b px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-sm font-semibold text-gray-950">Datos Bejerman</div>
        {details.syncedAt && <div className="text-xs text-gray-500">Sincronizado {formatDateTime(details.syncedAt)}</div>}
      </div>
      {fields.length > 0 && (
        <div className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-2 lg:grid-cols-3">
          {fields.map((item) => (
            <div key={item.key} className="min-w-0">
              <div className="text-[11px] uppercase text-gray-500">{item.label}</div>
              <div className="break-words text-sm text-gray-950">{item.value}</div>
            </div>
          ))}
        </div>
      )}
      {raw.length > 0 && (
        <details className="border-t px-3 py-2 text-xs text-gray-700">
          <summary className="cursor-pointer font-medium text-gray-800">Ver todos los campos crudos</summary>
          <dl className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {raw.map(([key, value]) => (
              <div key={key} className="min-w-0">
                <dt className="font-mono text-[11px] text-gray-500">{key}</dt>
                <dd className="break-words">{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </section>
  );
}

function customerFormFrom(row, candidate = null) {
  const next = {
    razon_social: row?.razon_social || "",
    cod_empresa: row?.cod_empresa || "",
    alias_interno: row?.alias_interno || "",
    cuit: row?.cuit || "",
    contacto: row?.contacto || "",
    telefono: row?.telefono || "",
    telefono_2: row?.telefono_2 || "",
    email: row?.email || "",
  };
  if (candidate) {
    next.razon_social = candidate.name || next.razon_social;
    next.cod_empresa = candidate.code || next.cod_empresa;
    next.cuit = candidate.cuit || next.cuit;
  }
  return next;
}

function payloadFrom(form) {
  return {
    razon_social: clean(form.razon_social),
    cod_empresa: clean(form.cod_empresa),
    alias_interno: clean(form.alias_interno),
    cuit: clean(form.cuit),
    contacto: clean(form.contacto),
    telefono: clean(form.telefono),
    telefono_2: clean(form.telefono_2),
    email: clean(form.email),
  };
}

function StatusBadge({ status }) {
  const meta = SYNC_META[status] || SYNC_META.unchecked;
  const Icon = status === "synced" ? CheckCircle2 : status === "unchecked" ? Link2 : AlertTriangle;
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-medium ${meta.cls}`}>
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {meta.label}
    </span>
  );
}

function StatBox({ label, value, tone = "gray" }) {
  const toneClass = {
    gray: "border-gray-200 bg-white",
    green: "border-emerald-200 bg-emerald-50",
    amber: "border-amber-200 bg-amber-50",
    blue: "border-sky-200 bg-sky-50",
  }[tone];
  return (
    <div className={`rounded border px-3 py-2 ${toneClass}`}>
      <div className="text-xs uppercase text-gray-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-gray-950">{value}</div>
    </div>
  );
}

function CandidateList({ state, onUse }) {
  const items = Array.isArray(state?.items) ? state.items : [];
  if (state?.loading) {
    return (
      <div className="mt-2 flex items-center gap-2 rounded border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-600">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        Buscando en Bejerman...
      </div>
    );
  }
  if (state?.error) {
    return <div className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{state.error}</div>;
  }
  if (!items.length) return null;
  return (
    <div className="mt-2 space-y-2">
      {items.map((candidate) => (
        <div key={`${candidate.code}-${candidate.name}`} className="rounded border border-gray-200 bg-white px-3 py-2">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <div className="font-mono text-sm font-semibold text-gray-950">{candidate.code || "-"}</div>
              <div className="truncate text-sm text-gray-800">{candidate.name || "-"}</div>
              <div className="mt-1 flex flex-wrap gap-1 text-[11px] text-gray-600">
                {candidate.cuit && <span className="rounded border border-gray-200 px-1.5 py-0.5">CUIT {candidate.cuit}</span>}
                {candidate.score > 0 && <span className="rounded border border-gray-200 px-1.5 py-0.5">{Math.round(candidate.score * 100)}%</span>}
                {(candidate.reasons || []).map((reason) => (
                  <span key={reason} className="rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-emerald-800">
                    {reason}
                  </span>
                ))}
              </div>
              <BejermanDetailChips details={bejermanDetails(candidate)} limit={5} />
            </div>
            <button
              type="button"
              className="inline-flex h-8 shrink-0 items-center justify-center gap-1 rounded bg-emerald-700 px-3 text-xs font-medium text-white hover:bg-emerald-800"
              onClick={() => onUse(candidate)}
            >
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              Usar
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function CustomerFields({ form, setForm, disabled = false, showCode = true }) {
  const on = (key) => (event) => setForm((prev) => ({ ...prev, [key]: event.target.value }));
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-6">
      <label className="md:col-span-3">
        <Label>Razón social</Label>
        <Input value={form.razon_social} onChange={on("razon_social")} disabled={disabled} required />
      </label>
      {showCode && (
        <label>
          <Label>Código Bejerman</Label>
          <Input value={form.cod_empresa} onChange={on("cod_empresa")} disabled={disabled} placeholder="Ej: SIM" />
        </label>
      )}
      <label>
        <Label>Alias interno</Label>
        <Input value={form.alias_interno} onChange={on("alias_interno")} disabled={disabled} placeholder="Ej: TMD" />
      </label>
      <label>
        <Label>CUIT</Label>
        <Input value={form.cuit} onChange={on("cuit")} disabled={disabled} />
      </label>
      <label>
        <Label>Contacto</Label>
        <Input value={form.contacto} onChange={on("contacto")} disabled={disabled} />
      </label>
      <label>
        <Label>Teléfono</Label>
        <Input value={form.telefono} onChange={on("telefono")} disabled={disabled} />
      </label>
      <label>
        <Label>Teléfono 2</Label>
        <Input value={form.telefono_2} onChange={on("telefono_2")} disabled={disabled} />
      </label>
      <label className="md:col-span-2">
        <Label>Email</Label>
        <Input type="email" value={form.email} onChange={on("email")} disabled={disabled} />
      </label>
    </div>
  );
}

export default function CatalogoClientes() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [f, setF] = useState(BLANK_CUSTOMER);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [edit, setEdit] = useState(null);
  const [ef, setEf] = useState(BLANK_CUSTOMER);
  const [savingEdit, setSavingEdit] = useState(false);
  const [mergeFrom, setMergeFrom] = useState("");
  const [mergeTo, setMergeTo] = useState("");
  const [merging, setMerging] = useState(false);
  const [syncingBejerman, setSyncingBejerman] = useState(false);
  const [filters, setFilters] = useState({ q: "", sync: "all" });
  const [addCandidates, setAddCandidates] = useState({ items: [] });
  const [editCandidates, setEditCandidates] = useState({ items: [] });

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const data = await getClientes({ include_stats: 1, include_bejerman: 1 });
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e.message || "No se pudieron cargar los clientes.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const stats = useMemo(() => {
    const total = rows.length;
    const synced = rows.filter((row) => row?.bejerman_sync?.status === "synced").length;
    const missingCode = rows.filter((row) => row?.bejerman_sync?.status === "missing_code").length;
    const review = rows.filter((row) => {
      const status = row?.bejerman_sync?.status;
      return status && !["synced", "missing_code", "unlinked"].includes(status);
    }).length;
    const withDetails = rows.filter((row) => row?.bejerman_details?.syncedAt).length;
    return { total, synced, missingCode, review, withDetails };
  }, [rows]);

  const filteredRows = useMemo(() => {
    const q = normalize(filters.q);
    return rows.filter((row) => {
      const status = row?.bejerman_sync?.status || "unchecked";
      if (filters.sync !== "all" && status !== filters.sync) return false;
      if (!q) return true;
      const candidates = (row?.bejerman_sync?.candidates || [])
        .map((candidate) => `${candidate.code || ""} ${candidate.name || ""} ${candidate.cuit || ""}`)
        .join(" ");
      const details = bejermanDetails(row);
      const haystack = normalize(
        [
          row.id,
          row.razon_social,
          row.cod_empresa,
          row.alias_interno,
          row.cuit,
          row.contacto,
          row.telefono,
          row.telefono_2,
          row.email,
          ...visibleDetailFields(details).map((item) => `${item.label} ${item.value}`),
          candidates,
        ].join(" ")
      );
      return haystack.includes(q);
    });
  }, [filters, rows]);

  const runCandidateSearch = async (target) => {
    const isEdit = target === "edit";
    const form = isEdit ? ef : f;
    const setter = isEdit ? setEditCandidates : setAddCandidates;
    const query = [form.cod_empresa, form.razon_social, form.cuit].map(clean).filter(Boolean).join(" ");
    if (!query && !(isEdit && edit?.id)) {
      setter({ items: [], error: "Ingrese razón social, código o CUIT." });
      return;
    }
    setter({ items: [], loading: true });
    try {
      const data = await getClienteBejermanCandidates({
        q: query,
        customer_id: isEdit ? edit?.id : undefined,
        limit: 8,
      });
      setter({ items: Array.isArray(data?.items) ? data.items : [] });
    } catch (e) {
      setter({ items: [], error: e.message || "No se pudo consultar Bejerman." });
    }
  };

  const applyCandidate = (target, candidate) => {
    const setter = target === "edit" ? setEf : setF;
    setter((prev) => ({
      ...prev,
      razon_social: candidate.name || prev.razon_social,
      cod_empresa: candidate.code || prev.cod_empresa,
      cuit: candidate.cuit || prev.cuit,
    }));
  };

  const add = async (event) => {
    event.preventDefault();
    try {
      setErr("");
      setMsg("");
      await postCliente(payloadFrom(f));
      setF(BLANK_CUSTOMER);
      setAddCandidates({ items: [] });
      setMsg("Cliente agregado");
      await load();
    } catch (e) {
      setErr(e.message || "No se pudo agregar el cliente.");
    }
  };

  const del = async (row) => {
    if (!confirm(`Eliminar cliente ${row.razon_social}?`)) return;
    try {
      setErr("");
      setMsg("");
      await deleteCliente(row.id);
      setMsg("Cliente eliminado");
      await load();
    } catch (e) {
      setErr(e.message || "No se pudo eliminar el cliente.");
    }
  };

  const openEdit = (cliente, candidate = null) => {
    setErr("");
    setMsg("");
    setEdit(cliente);
    setEf(customerFormFrom(cliente, candidate));
    const candidates = candidate ? [candidate] : cliente?.bejerman_sync?.candidates || [];
    setEditCandidates({ items: candidates });
  };

  const saveEdit = async (event) => {
    event.preventDefault();
    if (!edit) return;
    try {
      setSavingEdit(true);
      setErr("");
      setMsg("");
      await patchCliente(edit.id, payloadFrom(ef));
      setMsg("Cliente actualizado");
      setEdit(null);
      setEf(BLANK_CUSTOMER);
      setEditCandidates({ items: [] });
      await load();
    } catch (e) {
      setErr(e.message || "No se pudo actualizar el cliente.");
    } finally {
      setSavingEdit(false);
    }
  };

  const syncBejerman = async () => {
    if (!confirm("¿Traer clientes desde Bejerman y actualizar Nexora?")) return;
    try {
      setSyncingBejerman(true);
      setErr("");
      setMsg("");
      const result = await postClienteBejermanSync();
      const skipped = Number(result?.skipped || 0);
      setMsg(
        `Bejerman sincronizado: ${Number(result?.updated || 0)} actualizados, ${Number(result?.created || 0)} creados` +
          (skipped ? `, ${skipped} omitidos` : "")
      );
      await load();
    } catch (e) {
      setErr(e.message || "No se pudo sincronizar Bejerman.");
    } finally {
      setSyncingBejerman(false);
    }
  };

  const merge = async (event) => {
    event.preventDefault();
    if (!mergeFrom || !mergeTo) {
      setErr("Seleccione origen y destino para unificar.");
      return;
    }
    if (mergeFrom === mergeTo) {
      setErr("El origen y el destino no pueden ser el mismo cliente.");
      return;
    }
    const src = rows.find((row) => String(row.id) === String(mergeFrom));
    const dst = rows.find((row) => String(row.id) === String(mergeTo));
    const srcLabel = src ? `${src.razon_social} (#${src.id})` : mergeFrom;
    const dstLabel = dst ? `${dst.razon_social} (#${dst.id})` : mergeTo;
    if (!confirm(`Unificar ${srcLabel} dentro de ${dstLabel}?`)) return;
    try {
      setMerging(true);
      setErr("");
      setMsg("");
      await postClienteMerge(mergeFrom, mergeTo);
      setMsg("Clientes unificados");
      setMergeFrom("");
      setMergeTo("");
      await load();
    } catch (e) {
      setErr(e.message || "No se pudieron unificar los clientes.");
    } finally {
      setMerging(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-gray-950">Clientes</h1>
          <p className="mt-1 text-sm text-gray-600">Catálogo operativo y vinculación Bejerman.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={syncBejerman}
            disabled={syncingBejerman || loading}
            className="inline-flex h-9 items-center justify-center gap-2 rounded bg-emerald-700 px-3 text-sm text-white hover:bg-emerald-800 disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${syncingBejerman ? "animate-spin" : ""}`} aria-hidden="true" />
            Sincronizar Bejerman
          </button>
          <button
            type="button"
            onClick={load}
            disabled={loading || syncingBejerman}
            className="inline-flex h-9 items-center justify-center gap-2 rounded border px-3 text-sm hover:bg-gray-50 disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            Actualizar
          </button>
        </div>
      </div>

      {err && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      {msg && <div className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">{msg}</div>}

      <div className="grid gap-3 md:grid-cols-5">
        <StatBox label="Total" value={stats.total} />
        <StatBox label="Sincronizados" value={stats.synced} tone="green" />
        <StatBox label="Con datos Bejerman" value={stats.withDetails} tone="blue" />
        <StatBox label="Sin código" value={stats.missingCode} tone="blue" />
        <StatBox label="A revisar" value={stats.review} tone="amber" />
      </div>

      <section className="rounded border border-gray-200 bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold">Buscar</div>
        <div className="flex flex-col gap-3 p-3 lg:flex-row lg:items-end">
          <label className="min-w-[260px] flex-1">
            <Label>Texto</Label>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-2.5 h-4 w-4 text-gray-400" aria-hidden="true" />
              <Input
                value={filters.q}
                onChange={(event) => setFilters((prev) => ({ ...prev, q: event.target.value }))}
                className="pl-8"
                placeholder="Razón social, alias, código, CUIT, email o teléfono"
              />
            </div>
          </label>
          <label className="lg:w-64">
            <Label>Estado Bejerman</Label>
            <select
              value={filters.sync}
              onChange={(event) => setFilters((prev) => ({ ...prev, sync: event.target.value }))}
              className="h-9 w-full rounded border border-gray-300 px-2 text-sm"
            >
              <option value="all">Todos</option>
              <option value="synced">Sincronizado</option>
              <option value="missing_code">Sin código</option>
              <option value="review">Revisar</option>
              <option value="code_mismatch">Código posible</option>
              <option value="not_found">Código no encontrado</option>
              <option value="unlinked">Sin vincular</option>
              <option value="unavailable">Bejerman no disponible</option>
            </select>
          </label>
          <div className="text-sm text-gray-500 lg:w-40">{filteredRows.length} resultados</div>
        </div>
      </section>

      <section className="rounded border border-gray-200 bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold">Nuevo cliente</div>
        <form onSubmit={add} className="p-3">
          <CustomerFields form={f} setForm={setF} />
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => runCandidateSearch("add")}
              className="inline-flex h-9 items-center gap-2 rounded border px-3 text-sm hover:bg-gray-50"
            >
              <Search className="h-4 w-4" aria-hidden="true" />
              Buscar en Bejerman
            </button>
            <button className="inline-flex h-9 items-center gap-2 rounded bg-blue-600 px-3 text-sm text-white hover:bg-blue-700">
              <UserPlus className="h-4 w-4" aria-hidden="true" />
              Agregar
            </button>
          </div>
          <CandidateList state={addCandidates} onUse={(candidate) => applyCandidate("add", candidate)} />
        </form>
      </section>

      <section className="rounded border border-gray-200 bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold">Unificar duplicados</div>
        <form onSubmit={merge} className="grid grid-cols-1 gap-3 p-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
          <label>
            <Label>Origen</Label>
            <select className="h-9 w-full rounded border border-gray-300 px-2 text-sm" value={mergeFrom} onChange={(e) => setMergeFrom(e.target.value)}>
              <option value="">Cliente a eliminar</option>
              {rows.map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.id} - {c.cod_empresa || "sin código"} - {c.razon_social}
                </option>
              ))}
            </select>
          </label>
          <label>
            <Label>Destino</Label>
            <select className="h-9 w-full rounded border border-gray-300 px-2 text-sm" value={mergeTo} onChange={(e) => setMergeTo(e.target.value)}>
              <option value="">Cliente a conservar</option>
              {rows.map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.id} - {c.cod_empresa || "sin código"} - {c.razon_social}
                </option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            className="inline-flex h-9 items-center justify-center gap-2 rounded bg-amber-600 px-3 text-sm text-white hover:bg-amber-700 disabled:opacity-60"
            disabled={merging}
          >
            {merging ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Users className="h-4 w-4" aria-hidden="true" />}
            Unificar
          </button>
        </form>
      </section>

      <section className="rounded border border-gray-200 bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold">Listado</div>
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
              <tr>
                <th className="px-3 py-2">Cliente</th>
                <th className="px-3 py-2">Código</th>
                <th className="px-3 py-2">Bejerman</th>
                <th className="px-3 py-2">Contacto</th>
                <th className="px-3 py-2 text-right">Actividad</th>
                <th className="px-3 py-2 text-right">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan="6" className="px-3 py-8 text-center text-gray-500">
                    Cargando...
                  </td>
                </tr>
              ) : (
                filteredRows.map((c) => {
                  const sync = c.bejerman_sync || { status: "unchecked", candidates: [] };
                  const candidates = Array.isArray(sync.candidates) ? sync.candidates : [];
                  const details = bejermanDetails(c);
                  return (
                    <tr key={c.id} className="border-t align-top">
                      <td className="px-3 py-3">
                        <div className="font-medium text-gray-950">{c.razon_social}</div>
                        <div className="mt-1 text-xs text-gray-500">
                          ID {c.id}{c.alias_interno ? ` · Alias ${c.alias_interno}` : ""}{c.cuit ? ` · CUIT ${c.cuit}` : ""}
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="font-mono text-sm text-gray-950">{c.cod_empresa || "-"}</div>
                      </td>
                      <td className="px-3 py-3">
                        <StatusBadge status={sync.status || "unchecked"} />
                        {sync.message && <div className="mt-1 max-w-[360px] text-xs text-gray-500">{sync.message}</div>}
                        <BejermanDetailChips details={details} limit={5} />
                        {details.syncedAt && <div className="mt-1 text-[11px] text-gray-500">Datos {formatDateTime(details.syncedAt)}</div>}
                        {sync.status !== "synced" && candidates.length > 0 && (
                          <div className="mt-2 flex flex-wrap gap-1">
                            {candidates.slice(0, 3).map((candidate) => (
                              <button
                                key={`${c.id}-${candidate.code}-${candidate.name}`}
                                type="button"
                                onClick={() => openEdit(c, candidate)}
                                className="rounded border border-emerald-200 bg-emerald-50 px-2 py-1 text-left text-xs text-emerald-800 hover:bg-emerald-100"
                                title={candidateLabel(candidate)}
                              >
                                {candidate.code || "sin código"}
                              </button>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <div>{c.contacto || "-"}</div>
                        <div className="mt-1 text-xs text-gray-500">{[c.telefono, c.telefono_2].filter(Boolean).join(" / ") || "-"}</div>
                        <div className="mt-1 text-xs text-gray-500">{c.email || "-"}</div>
                        {(details.telefono || details.email || details.contacto) && (
                          <div className="mt-2 border-t border-gray-100 pt-2 text-xs text-gray-500">
                            <div>Bejerman: {details.contacto || "-"}</div>
                            <div>{[details.telefono, details.telefono2].filter(Boolean).join(" / ") || "-"}</div>
                            <div>{details.email || "-"}</div>
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-3 text-right">
                        <div>{Number(c.equipos_count || 0)} equipos</div>
                        <div className="mt-1 text-xs text-gray-500">{Number(c.ingresos_count || 0)} ingresos</div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => openEdit(c)}
                            className="inline-flex h-8 w-8 items-center justify-center rounded border hover:bg-gray-50"
                            title="Editar"
                            aria-label={`Editar ${c.razon_social}`}
                          >
                            <Pencil className="h-4 w-4" aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            onClick={() => del(c)}
                            className="inline-flex h-8 w-8 items-center justify-center rounded border text-red-700 hover:bg-red-50"
                            title="Eliminar"
                            aria-label={`Eliminar ${c.razon_social}`}
                          >
                            <Trash2 className="h-4 w-4" aria-hidden="true" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
              {!loading && !filteredRows.length && (
                <tr>
                  <td colSpan="6" className="px-3 py-8 text-center text-gray-500">
                    Sin resultados
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {edit && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => !savingEdit && setEdit(null)}
        >
          <div className="w-full max-w-4xl rounded bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div>
                <h2 className="text-lg font-semibold text-gray-950">Editar cliente</h2>
                <div className="text-xs text-gray-500">ID {edit.id}</div>
              </div>
              <button
                type="button"
                className="inline-flex h-8 w-8 items-center justify-center rounded border hover:bg-gray-50"
                onClick={() => !savingEdit && setEdit(null)}
                aria-label="Cerrar"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <form onSubmit={saveEdit} className="p-4">
              <div className="mb-3 rounded border border-sky-200 bg-sky-50 p-3">
                <label className="block max-w-sm">
                  <Label>Código Bejerman</Label>
                  <Input
                    value={ef.cod_empresa}
                    onChange={(event) => setEf((prev) => ({ ...prev, cod_empresa: event.target.value }))}
                    disabled={savingEdit}
                    placeholder="Código del cliente en Bejerman"
                    className="border-sky-300 bg-white font-mono"
                  />
                </label>
              </div>
              <CustomerFields form={ef} setForm={setEf} disabled={savingEdit} showCode={false} />
              <BejermanDetailsPanel customer={edit} />
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => runCandidateSearch("edit")}
                  className="inline-flex h-9 items-center gap-2 rounded border px-3 text-sm hover:bg-gray-50"
                  disabled={savingEdit}
                >
                  <Search className="h-4 w-4" aria-hidden="true" />
                  Buscar en Bejerman
                </button>
                <button type="button" className="h-9 rounded border px-3 text-sm hover:bg-gray-50" onClick={() => setEdit(null)} disabled={savingEdit}>
                  Cancelar
                </button>
                <button type="submit" className="inline-flex h-9 items-center gap-2 rounded bg-blue-600 px-3 text-sm text-white hover:bg-blue-700 disabled:opacity-60" disabled={savingEdit}>
                  {savingEdit && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
                  Guardar
                </button>
              </div>
              <CandidateList state={editCandidates} onUse={(candidate) => applyCandidate("edit", candidate)} />
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
