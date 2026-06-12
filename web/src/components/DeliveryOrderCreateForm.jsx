import { useEffect, useMemo, useState } from "react";
import { getClientesBasico, getDeliveryOrderBejermanArticles, postDeliveryOrder } from "../lib/api";

const OPERATION_COMPANIES = ["SEPID", "MGBIO", "ALTA ALQUILER", "ALQUILERES", "REPARACION", "MARKETING"];

const emptyItem = () => ({
  quantity: "1",
  articleCode: "",
  articleName: "",
  description: "",
  unitPrice: "",
  partida: "",
  assignedQuantity: "1",
  partidaExpirationDate: "",
  stockDepositCode: "",
});

const initialForm = () => ({
  customerId: "",
  customerName: "",
  customerSearch: "",
  deliveryType: "sale",
  priority: "normal",
  sellerName: "",
  orderDate: new Date().toISOString().slice(0, 10),
  operationCompanyLabel: "SEPID",
  commercialExchangeRate: "",
  commercialCondition: "",
  rawPedido: "",
  items: [emptyItem()],
});

const clean = (value) => String(value || "").trim();

function quantityOf(value, fallback = "1") {
  const parsed = Number.parseFloat(String(value || "").replace(",", "."));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function quantityNumber(value, fallback = 1) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function formatQuantity(value, fallback = 1) {
  const number = quantityNumber(value, fallback);
  if (Number.isInteger(number)) return String(number);
  return number.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

function partidaStats(item) {
  const total = quantityNumber(item.quantity);
  const assigned = clean(item.partida) ? quantityNumber(item.assignedQuantity, 0) : 0;
  const pending = Math.max(0, total - assigned);
  return {
    assigned: formatQuantity(assigned),
    total: formatQuantity(total),
    pending: formatQuantity(pending),
    lots: clean(item.partida) ? 1 : 0,
  };
}

function decimalTextOrNull(value) {
  const raw = clean(value);
  return raw ? raw.replace(",", ".") : null;
}

export default function DeliveryOrderCreateForm({
  onCreated,
  onCancel,
  submitLabel = "Crear entrega",
  compact = false,
}) {
  const [customers, setCustomers] = useState([]);
  const [form, setForm] = useState(initialForm);
  const [customerSearchOpen, setCustomerSearchOpen] = useState(false);
  const [loadingCustomers, setLoadingCustomers] = useState(false);
  const [articleOptions, setArticleOptions] = useState({});
  const [articleUnavailable, setArticleUnavailable] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoadingCustomers(true);
    getClientesBasico()
      .then((data) => {
        if (active) setCustomers(Array.isArray(data) ? data : []);
      })
      .catch((err) => {
        if (!active) return;
        setCustomers([]);
        setError(err?.message || "No se pudieron cargar los clientes.");
      })
      .finally(() => {
        if (active) setLoadingCustomers(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const customerById = useMemo(() => {
    const out = new Map();
    customers.forEach((customer) => out.set(String(customer.id), customer));
    return out;
  }, [customers]);

  const filteredCustomers = useMemo(() => {
    const q = clean(form.customerSearch).toLowerCase();
    if (!q) return customers.slice(0, 30);
    return customers
      .filter((customer) =>
        [customer.razon_social, customer.nombre, customer.cod_empresa, customer.cuit, customer.telefono]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(q)
      )
      .slice(0, 30);
  }, [customers, form.customerSearch]);

  const update = (field) => (event) => {
    const value = event.target.value;
    setForm((current) => {
      if (field === "deliveryType") {
        return {
          ...current,
          deliveryType: value,
          operationCompanyLabel:
            value === "rental"
              ? "ALQUILERES"
              : current.operationCompanyLabel === "ALQUILERES"
                ? "SEPID"
                : current.operationCompanyLabel,
        };
      }
      return { ...current, [field]: value };
    });
  };

  const selectCustomer = (customer) => {
    setForm((current) => ({
      ...current,
      customerId: String(customer.id || ""),
      customerName: customer.razon_social || customer.nombre || "",
      customerSearch: customer.razon_social || customer.nombre || "",
    }));
    setCustomerSearchOpen(false);
  };

  const updateItem = (index, changes) => {
    setForm((current) => ({
      ...current,
      items: current.items.map((item, itemIndex) => (itemIndex === index ? { ...item, ...changes } : item)),
    }));
  };

  const addItem = () => {
    setForm((current) => ({ ...current, items: [...current.items, emptyItem()] }));
  };

  const removeItem = (index) => {
    setForm((current) => ({
      ...current,
      items: current.items.length > 1 ? current.items.filter((_, itemIndex) => itemIndex !== index) : current.items,
    }));
    setArticleOptions((current) => {
      const next = {};
      Object.entries(current).forEach(([key, value]) => {
        const numericKey = Number.parseInt(key, 10);
        if (!Number.isSafeInteger(numericKey) || numericKey === index) return;
        next[numericKey > index ? numericKey - 1 : numericKey] = value;
      });
      return next;
    });
  };

  const searchArticles = async (index, value) => {
    updateItem(index, {
      articleCode: "",
      articleName: value,
      description: value,
      partida: "",
      assignedQuantity: "1",
      partidaExpirationDate: "",
      stockDepositCode: "",
    });

    const q = clean(value);
    if (q.length < 2) {
      setArticleOptions((current) => ({ ...current, [index]: [] }));
      return;
    }

    try {
      const data = await getDeliveryOrderBejermanArticles({ q, search: q, limit: 8 });
      setArticleUnavailable(Boolean(data?.unavailable));
      setArticleOptions((current) => ({ ...current, [index]: Array.isArray(data?.items) ? data.items : [] }));
    } catch {
      setArticleUnavailable(true);
      setArticleOptions((current) => ({ ...current, [index]: [] }));
    }
  };

  const chooseArticle = (index, article) => {
    const articleCode = clean(article?.code);
    const articleName = clean(article?.name || article?.description || articleCode);
    updateItem(index, {
      articleCode,
      articleName,
      description: clean(article?.description) || articleName || articleCode,
      partida: "",
      assignedQuantity: "1",
      partidaExpirationDate: "",
      stockDepositCode: "",
    });
    setArticleOptions((current) => ({ ...current, [index]: [] }));
  };

  const createOrder = async (event) => {
    event.preventDefault();
    const customer = customerById.get(String(form.customerId));
    const items = form.items
      .map((item) => {
        const description = clean(item.description) || clean(item.articleName) || clean(item.articleCode);
        const partida = clean(item.partida);
        return {
          articleCode: clean(item.articleCode) || null,
          articleName: clean(item.articleName) || null,
          description,
          quantity: quantityOf(item.quantity),
          unitPrice: decimalTextOrNull(item.unitPrice),
          partida: partida || null,
          partidaExpirationDate: clean(item.partidaExpirationDate) || null,
          stockDepositCode: clean(item.stockDepositCode) || null,
          partidas: partida
            ? [
                {
                  partida,
                  assignedQuantity: quantityOf(item.assignedQuantity),
                  partidaExpirationDate: clean(item.partidaExpirationDate) || null,
                  stockDepositCode: clean(item.stockDepositCode) || null,
                },
              ]
            : [],
        };
      })
      .filter((item) => item.description);
    const rawPedido = clean(form.rawPedido) || items.map((item) => item.description).join("\n");

    if (!clean(form.customerName)) {
      setError("Cliente en remito es requerido.");
      return;
    }
    if (!clean(form.sellerName)) {
      setError("Vendedor es requerido.");
      return;
    }
    if (!clean(form.operationCompanyLabel)) {
      setError("Empresa operativa es requerida.");
      return;
    }
    if (!rawPedido || !items.length) {
      setError("Cargue al menos un renglón de artículo o detalle.");
      return;
    }

    setSaving(true);
    setError("");
    try {
      const created = await postDeliveryOrder({
        customerId: form.customerId ? Number(form.customerId) : null,
        customerName: clean(form.customerName),
        bejermanCustomerCode: customer?.cod_empresa || "",
        deliveryType: form.deliveryType,
        priority: form.priority,
        sellerName: clean(form.sellerName),
        orderDate: form.orderDate,
        operationCompanyLabel: clean(form.operationCompanyLabel),
        rawPedido,
        commercialExchangeRate: clean(form.commercialExchangeRate) || null,
        commercialCondition: clean(form.commercialCondition) || null,
        items,
      });
      setForm(initialForm());
      setCustomerSearchOpen(false);
      setArticleOptions({});
      setArticleUnavailable(false);
      if (onCreated) onCreated(created);
    } catch (err) {
      setError(err?.message || "No se pudo crear la entrega.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={createOrder} className={compact ? "space-y-4" : "space-y-5"}>
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        <select value={form.deliveryType} onChange={update("deliveryType")} className="rounded border px-3 py-2 bg-white">
          <option value="sale">Venta</option>
          <option value="rental">Alquiler</option>
        </select>

        <div className="relative md:col-span-2">
          <input
            value={form.customerSearch}
            onFocus={() => setCustomerSearchOpen(true)}
            onChange={(event) => {
              setForm((current) => ({
                ...current,
                customerId: "",
                customerSearch: event.target.value,
              }));
              setCustomerSearchOpen(true);
            }}
            placeholder="Cliente"
            className="w-full rounded border px-3 py-2"
            disabled={loadingCustomers || saving}
          />
          {customerSearchOpen && (
            <div className="absolute z-30 mt-1 max-h-64 w-full overflow-y-auto rounded border bg-white shadow-lg">
              {filteredCustomers.length ? (
                filteredCustomers.map((customer) => (
                  <button
                    key={customer.id}
                    type="button"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => selectCustomer(customer)}
                    className="w-full px-3 py-2 text-left text-sm hover:bg-gray-50"
                  >
                    <span className="block truncate font-medium">{customer.razon_social || customer.nombre}</span>
                    <span className="block truncate text-xs text-gray-500">
                      Código {customer.cod_empresa || "-"} {customer.cuit ? `· CUIT ${customer.cuit}` : ""}
                    </span>
                  </button>
                ))
              ) : (
                <div className="px-3 py-2 text-xs text-gray-500">Sin coincidencias.</div>
              )}
            </div>
          )}
        </div>

        <input
          value={form.customerName}
          onChange={update("customerName")}
          placeholder="Cliente en remito"
          className="rounded border px-3 py-2"
          required
        />

        <input value={form.sellerName} onChange={update("sellerName")} placeholder="Vendedor" className="rounded border px-3 py-2" required />
        <input type="date" value={form.orderDate} onChange={update("orderDate")} className="rounded border px-3 py-2" required />
        <select value={form.operationCompanyLabel} onChange={update("operationCompanyLabel")} className="rounded border px-3 py-2 bg-white" required>
          {OPERATION_COMPANIES.map((company) => (
            <option key={company} value={company}>
              {company}
            </option>
          ))}
        </select>
        <select value={form.priority} onChange={update("priority")} className="rounded border px-3 py-2 bg-white">
          <option value="normal">Normal</option>
          <option value="urgente">Urgente</option>
        </select>
        <input value={form.commercialExchangeRate} onChange={update("commercialExchangeRate")} placeholder="TC" className="rounded border px-3 py-2" />
        <input value={form.commercialCondition} onChange={update("commercialCondition")} placeholder="Condición" className="rounded border px-3 py-2 xl:col-span-2" />
      </div>

      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-900">Artículos</h3>
          <button type="button" onClick={addItem} className="rounded border px-3 py-1.5 text-sm hover:bg-gray-50">
            Agregar renglón
          </button>
        </div>
        {articleUnavailable && (
          <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            Catálogo Bejerman no disponible. Se puede cargar texto libre.
          </div>
        )}

        {form.items.map((item, index) => {
          const stats = partidaStats(item);
          return (
            <div key={index} className="rounded border bg-gray-50 p-2">
              <div className="grid grid-cols-1 gap-2 md:grid-cols-[82px_minmax(180px,1fr)_minmax(220px,1.25fr)_110px_42px]">
                <input
                  value={item.quantity}
                  onChange={(event) => updateItem(index, { quantity: event.target.value })}
                  placeholder="Cant."
                  className="rounded border bg-white px-3 py-2"
                />
                <div className="relative">
                  <input
                    value={item.articleCode || item.articleName}
                    onChange={(event) => searchArticles(index, event.target.value)}
                    placeholder="Buscar por código o detalle"
                    className="w-full rounded border bg-white px-3 py-2"
                    disabled={saving}
                  />
                  {item.articleCode && (
                    <p className="mt-1 truncate text-[11px] text-gray-500">{item.articleName || item.articleCode}</p>
                  )}
                  {(articleOptions[index] || []).length > 0 && (
                    <div className="absolute z-30 mt-1 max-h-56 w-full overflow-y-auto rounded border bg-white shadow-lg">
                      {(articleOptions[index] || []).map((article) => (
                        <button
                          key={`${article.code || article.id || ""}-${article.name || article.description || ""}`}
                          type="button"
                          onClick={() => chooseArticle(index, article)}
                          className="block w-full border-b px-3 py-2 text-left text-sm hover:bg-gray-50"
                          disabled={saving}
                        >
                          <span className="block font-mono text-xs text-sky-700">{article.code || "Sin código"}</span>
                          <span className="block truncate text-gray-900">{article.name || article.description || "-"}</span>
                          {article.description && article.description !== article.name && (
                            <span className="mt-0.5 block line-clamp-2 text-xs text-gray-500">{article.description}</span>
                          )}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <textarea
                  value={item.description}
                  onChange={(event) => updateItem(index, { description: event.target.value })}
                  placeholder="Detalle del renglón"
                  rows={2}
                  className="rounded border bg-white px-3 py-2"
                />
                <input
                  value={item.unitPrice}
                  onChange={(event) => updateItem(index, { unitPrice: event.target.value })}
                  placeholder="Precio"
                  className="rounded border bg-white px-3 py-2"
                />
                <button
                  type="button"
                  onClick={() => removeItem(index)}
                  disabled={form.items.length <= 1}
                  className="rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-40"
                  aria-label="Quitar renglón"
                >
                  X
                </button>
              </div>
              <div className="mt-2 rounded border border-gray-200 bg-white p-2">
                <div className="grid grid-cols-1 gap-2 lg:grid-cols-[minmax(210px,1fr)_120px_150px_120px_106px_106px_96px]">
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium uppercase text-gray-500">Partida / serie</span>
                    <input
                      value={item.partida}
                      onChange={(event) => updateItem(index, { partida: event.target.value })}
                      placeholder="Tipear partida"
                      className="h-10 w-full rounded border bg-white px-3 py-2"
                    />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium uppercase text-gray-500">Cantidad asignada</span>
                    <input
                      value={item.assignedQuantity}
                      onChange={(event) => updateItem(index, { assignedQuantity: event.target.value })}
                      placeholder="Asignado"
                      className="h-10 w-full rounded border bg-white px-3 py-2"
                    />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium uppercase text-gray-500">Vencimiento</span>
                    <input
                      type="date"
                      value={item.partidaExpirationDate}
                      onChange={(event) => updateItem(index, { partidaExpirationDate: event.target.value })}
                      className="h-10 w-full rounded border bg-white px-3 py-2"
                    />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium uppercase text-gray-500">Depósito</span>
                    <input
                      value={item.stockDepositCode}
                      onChange={(event) => updateItem(index, { stockDepositCode: event.target.value })}
                      placeholder="Depósito"
                      className="h-10 w-full rounded border bg-white px-3 py-2"
                    />
                  </label>
                  <div className="rounded border bg-gray-50 px-3 py-2">
                    <div className="text-[11px] font-medium uppercase text-gray-500">Asignado</div>
                    <div className="text-sm text-gray-900">{stats.assigned} / {stats.total}</div>
                  </div>
                  <div className="rounded border bg-gray-50 px-3 py-2">
                    <div className="text-[11px] font-medium uppercase text-gray-500">Pendiente</div>
                    <div className="text-sm text-gray-900">{stats.pending}</div>
                  </div>
                  <div className="rounded border bg-gray-50 px-3 py-2">
                    <div className="text-[11px] font-medium uppercase text-gray-500">Lotes</div>
                    <div className="text-sm text-gray-900">{stats.lots}</div>
                  </div>
                </div>
              </div>
              {item.articleName && (
                <div className="mt-2 text-xs text-gray-600">Artículo seleccionado: {item.articleName}</div>
              )}
            </div>
          );
        })}
      </div>

      <textarea
        value={form.rawPedido}
        onChange={update("rawPedido")}
        placeholder="Detalle completo de la entrega"
        rows={4}
        className="w-full rounded border px-3 py-2"
      />

      <div className="flex justify-end gap-2">
        {onCancel && (
          <button type="button" className="rounded border px-4 py-2 text-sm hover:bg-gray-50" onClick={onCancel} disabled={saving}>
            Cancelar
          </button>
        )}
        <button type="submit" className="btn" disabled={saving || loadingCustomers}>
          {saving ? "Creando..." : submitLabel}
        </button>
      </div>
    </form>
  );
}
