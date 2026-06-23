import { useEffect, useMemo, useRef, useState } from "react";
import {
  getClientesBasico,
  getDeliveryOrderBejermanArticleStock,
  getDeliveryOrderBejermanArticles,
  getDeliveryOrderBejermanDeposits,
  getDeliveryOrderRentalAvailableEquipment,
  patchDeliveryOrder,
  postDeliveryOrder,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";

const BEJERMAN_COMPANIES = [
  { key: "SEPID", label: "SEPID" },
  { key: "MGBIO", label: "MG BIO" },
];
const SELLERS = [
  { code: "ADM", name: "Administración", label: "ADM Administración" },
  { code: "EZE", name: "Ezequiel Merino", label: "EZE Ezequiel Merino" },
  { code: "MAX", name: "Maximiliano Pereletegui", label: "MAX Maximiliano Pereletegui" },
  { code: "MER", name: "Mercado Libre", label: "MER Mercado Libre" },
  { code: "TOM", name: "Tomas Perez Avila", label: "TOM Tomas Perez Avila" },
];
const RENTAL_SELLER_CODE = "ADM";
const REMITO_PROFILE_BY_TYPE = {
  sale: "RT / MC / VAL",
  rental: "RTA / ALQ / STL",
  demo: "RTN / DEMO / VAL",
};
const QUANTITY_TOLERANCE = 0.0001;
const FALLBACK_DEPOSITS = [
  { code: "VAL", label: "VAL" },
  { code: "STL", label: "STL" },
];

const emptyPartida = (assignedQuantity = "1", stockDepositCode = "") => ({
  partida: "",
  assignedQuantity,
  partidaExpirationDate: "",
  stockDepositCode,
  stockAvailableQuantity: "",
  stockCheckedAt: "",
});

const normalizePriceCurrency = (value, fallback = "ARS") => (
  String(value ?? "").trim().toUpperCase() === "USD" ? "USD" : fallback === "USD" ? "USD" : "ARS"
);

const emptyItem = (priceCurrency = "ARS") => ({
  id: "",
  ingresoId: "",
  deviceId: "",
  quantity: "1",
  articleCode: "",
  articleName: "",
  description: "",
  unitPrice: "",
  priceCurrency: normalizePriceCurrency(priceCurrency),
  discountPercent: "",
  partida: "",
  partidaExpirationDate: "",
  stockDepositCode: "",
  stockAvailableQuantity: "",
  stockCheckedAt: "",
  partidas: [emptyPartida()],
});

const initialForm = (defaultSellerCode = "") => {
  const seller = sellerByCode(defaultSellerCode);
  return {
    customerId: "",
    customerName: "",
    customerSearch: "",
    deliveryType: "sale",
    priority: "normal",
    sellerCode: seller.code,
    sellerName: seller.label,
    orderDate: new Date().toISOString().slice(0, 10),
    companyKey: "SEPID",
    operationCompanyLabel: "",
    priceCurrency: "ARS",
    commercialExchangeRate: "",
    commercialCondition: "",
    rawPedido: "",
    items: [emptyItem("ARS")],
  };
};

const customerDisplayMeta = (customer) =>
  [
    customer.alias_interno ? `Alias ${customer.alias_interno}` : "",
    `Código ${customer.cod_empresa || "-"}`,
    customer.cuit ? `CUIT ${customer.cuit}` : "",
  ].filter(Boolean).join(" · ");

const clean = (value) => String(value ?? "").trim();

function partidaInputKey(itemIndex, partidaIndex) {
  return `${itemIndex}:${partidaIndex}`;
}

function partidaMatchKey(value) {
  return clean(value).toLowerCase();
}

function formatServiceOrderReference(value) {
  const raw = clean(value);
  if (!raw) return "";
  const number = Number.parseInt(raw, 10);
  return Number.isFinite(number) ? `OS-${String(number).padStart(5, "0")}` : `OS-${raw}`;
}

function companyKeyForLabel(value) {
  const marker = clean(value).replace(/[-_\s]/g, "").toUpperCase();
  if (["MG", "MGB", "MGBI", "MGBIO", "MGBIOSA", "PORMG"].includes(marker) || marker.includes("MGBIO")) return "MGBIO";
  return "SEPID";
}

function normalizeCompanyKey(value) {
  const key = clean(value).toUpperCase();
  return BEJERMAN_COMPANIES.some((company) => company.key === key) ? key : "SEPID";
}

function normalizeSellerCode(value) {
  return clean(value).toUpperCase().slice(0, 4);
}

function sellerByCode(value) {
  const code = normalizeSellerCode(value);
  const known = SELLERS.find((seller) => seller.code === code);
  return known || { code, name: code, label: code };
}

function sellerNameForCode(value, fallback = "") {
  const code = normalizeSellerCode(value);
  if (!code) return clean(fallback);
  return sellerByCode(code).label;
}

function sellerOptionForCode(value, fallback = "") {
  const code = normalizeSellerCode(value);
  if (!code) return null;
  const seller = sellerByCode(code);
  return {
    code,
    label: seller.label || clean(fallback) || code,
  };
}

function sellerOptionsForCodes(...codes) {
  const options = SELLERS.map((seller) => ({ code: seller.code, label: seller.label }));
  const seen = new Set(options.map((seller) => seller.code));
  codes.forEach((code) => {
    const option = sellerOptionForCode(code);
    if (!option || seen.has(option.code)) return;
    seen.add(option.code);
    options.push(option);
  });
  return options;
}

function sellerFromOrder(order) {
  const code = normalizeSellerCode(order?.sellerCode || order?.seller_code);
  if (code) return sellerByCode(code);
  const name = clean(order?.sellerName);
  const marker = name.split(/\s+/)[0]?.toUpperCase();
  return SELLERS.find((seller) => seller.code === marker || seller.label === name || seller.name === name) || {
    code: "",
    name,
    label: name,
  };
}

function defaultDepositForDeliveryType(deliveryType) {
  return clean(deliveryType) === "rental" ? "STL" : "VAL";
}

function normalizeDepositOption(item) {
  const code = clean(item?.code || item?.depositCode || item?.id).toUpperCase();
  if (!code) return null;
  return {
    code,
    label: clean(item?.label || item?.name) || code,
  };
}

function quantityOf(value, fallback = "1") {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function quantityNumber(value, fallback = 1) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function discountNumber(value) {
  const raw = clean(value);
  if (!raw) return 0;
  const parsed = Number.parseFloat(raw.replace(",", "."));
  return Number.isFinite(parsed) ? parsed : null;
}

function formatQuantity(value, fallback = 1) {
  const number = quantityNumber(value, fallback);
  if (Number.isInteger(number)) return String(number);
  return number.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

function decimalTextOrNull(value) {
  const raw = clean(value);
  return raw ? raw.replace(",", ".") : null;
}

function lotAvailableQuantity(lot) {
  const value = lot?.stockAvailableQuantity ?? lot?.availableQuantity ?? lot?.realQuantity ?? 0;
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  return Number.isFinite(parsed) ? parsed : 0;
}

function lotExpiration(lot) {
  return clean(lot?.partidaExpirationDate || lot?.expirationDate);
}

function lotLabel(lot) {
  const pieces = [
    clean(lot?.partida),
    `Stock ${formatQuantity(lotAvailableQuantity(lot), 0)}`,
    lotExpiration(lot) ? `Vto. ${lotExpiration(lot)}` : "",
  ];
  return pieces.filter(Boolean).join(" - ");
}

function lotDraft(lot, assignedQuantity = "1") {
  return {
    partida: clean(lot?.partida),
    assignedQuantity: String(assignedQuantity),
    partidaExpirationDate: lotExpiration(lot),
    stockDepositCode: clean(lot?.stockDepositCode || lot?.depositCode) || "VAL",
    stockAvailableQuantity: String(lotAvailableQuantity(lot)),
    stockCheckedAt: clean(lot?.stockCheckedAt),
  };
}

function hasPartidaData(row) {
  return Boolean(
    clean(row?.partida) ||
      clean(row?.partidaExpirationDate) ||
      clean(row?.stockAvailableQuantity)
  );
}

function activePartidas(item) {
  return (Array.isArray(item?.partidas) ? item.partidas : []).filter(hasPartidaData);
}

function partidaStats(item) {
  const total = quantityNumber(item.quantity);
  const rows = activePartidas(item);
  const assigned = rows.reduce((sum, row) => sum + quantityNumber(row.assignedQuantity, 0), 0);
  const pending = Math.max(0, total - assigned);
  const over = Math.max(0, assigned - total);
  const mismatch = rows.length > 0 && Math.abs(assigned - total) > QUANTITY_TOLERANCE;
  return {
    assignedNumber: assigned,
    pendingNumber: pending,
    overNumber: over,
    assigned: formatQuantity(assigned),
    total: formatQuantity(total),
    pending: formatQuantity(pending),
    over: formatQuantity(over, 0),
    lots: rows.filter((row) => clean(row.partida)).length,
    hasRows: rows.length > 0,
    mismatch,
    valid: !mismatch,
  };
}

function shiftIndexedState(current, removedIndex) {
  const next = {};
  Object.entries(current || {}).forEach(([key, value]) => {
    const numericKey = Number.parseInt(key, 10);
    if (!Number.isSafeInteger(numericKey) || numericKey === removedIndex) return;
    next[numericKey > removedIndex ? numericKey - 1 : numericKey] = value;
  });
  return next;
}

function itemFromOrderItem(item) {
  const quantity = String(item?.quantity || 1);
  const partidas = Array.isArray(item?.partidas) && item.partidas.length
    ? item.partidas.map((partida) => ({
        id: clean(partida.id),
        partida: clean(partida.partida),
        assignedQuantity: String(partida.assignedQuantity || quantity),
        partidaExpirationDate: clean(partida.partidaExpirationDate),
        stockDepositCode: clean(partida.stockDepositCode),
        stockAvailableQuantity: partida.stockAvailableQuantity == null ? "" : String(partida.stockAvailableQuantity),
        stockCheckedAt: clean(partida.stockCheckedAt),
      }))
    : clean(item?.partida)
      ? [
          {
            partida: clean(item.partida),
            assignedQuantity: quantity,
            partidaExpirationDate: clean(item.partidaExpirationDate),
            stockDepositCode: clean(item.stockDepositCode),
            stockAvailableQuantity: item.stockAvailableQuantity == null ? "" : String(item.stockAvailableQuantity),
            stockCheckedAt: clean(item.stockCheckedAt),
          },
        ]
      : [emptyPartida()];
  return {
    id: clean(item?.id),
    ingresoId: item?.ingresoId == null ? "" : String(item.ingresoId),
    deviceId: item?.deviceId == null ? "" : String(item.deviceId),
    quantity,
    articleCode: clean(item?.articleCode),
    articleName: clean(item?.articleName),
    description: clean(item?.description || item?.sourceText || item?.articleName || item?.articleCode),
    unitPrice: item?.unitPrice == null ? "" : String(item.unitPrice),
    priceCurrency: normalizePriceCurrency(item?.priceCurrency),
    discountPercent: Number(item?.discountPercent || 0) > 0 ? String(item.discountPercent) : "",
    partida: clean(item?.partida),
    partidaExpirationDate: clean(item?.partidaExpirationDate),
    stockDepositCode: clean(item?.stockDepositCode),
    stockAvailableQuantity: item?.stockAvailableQuantity == null ? "" : String(item.stockAvailableQuantity),
    stockCheckedAt: clean(item?.stockCheckedAt),
    partidas,
  };
}

function equipmentItemFromOption(option) {
  const stock = option?.stockAvailableQuantity == null ? "" : String(option.stockAvailableQuantity);
  return {
    id: "",
    ingresoId: option?.ingresoId == null ? "" : String(option.ingresoId),
    deviceId: option?.deviceId == null ? "" : String(option.deviceId),
    quantity: "1",
    articleCode: clean(option?.articleCode),
    articleName: clean(option?.articleName),
    description: clean(option?.description || option?.equipmentDetail || option?.equipmentModel),
    unitPrice: "",
    priceCurrency: "ARS",
    discountPercent: "",
    partida: clean(option?.partida || option?.equipmentSerial),
    partidaExpirationDate: clean(option?.partidaExpirationDate),
    stockDepositCode: "STL",
    stockAvailableQuantity: stock,
    stockCheckedAt: clean(option?.stockCheckedAt),
    partidas: [],
  };
}

function formFromOrder(order, defaultSellerCode = "") {
  if (!order?.id) return initialForm(defaultSellerCode);
  const orderCurrency = normalizePriceCurrency(order.priceCurrency);
  const items = Array.isArray(order.items) && order.items.length
    ? order.items.map((item) => ({ ...itemFromOrderItem(item), priceCurrency: normalizePriceCurrency(item?.priceCurrency, orderCurrency) }))
    : [emptyItem(orderCurrency)];
  const deliveryType = clean(order.deliveryType) || "sale";
  const seller = deliveryType === "rental" ? sellerByCode(RENTAL_SELLER_CODE) : sellerFromOrder(order);
  return {
    customerId: order.customerId ? String(order.customerId) : "",
    customerName: clean(order.customerName),
    customerSearch: clean(order.customerName),
    deliveryType,
    priority: clean(order.priority) || "normal",
    sellerCode: seller.code,
    sellerName: seller.label,
    orderDate: clean(order.orderDate).slice(0, 10) || new Date().toISOString().slice(0, 10),
    companyKey: normalizeCompanyKey(order.companyKey || companyKeyForLabel(order.operationCompanyLabel || order.sourceCompanyId)),
    operationCompanyLabel: clean(order.operationCompanyLabel),
    priceCurrency: orderCurrency,
    commercialExchangeRate: clean(order.commercialExchangeRate),
    commercialCondition: clean(order.commercialCondition),
    rawPedido: clean(order.rawPedido),
    items,
  };
}

function partidasOpenStateForForm(form, forceOpen = false) {
  const next = {};
  (form?.items || []).forEach((item, index) => {
    if (forceOpen || activePartidas(item).length > 0) next[index] = true;
  });
  return next;
}

function depositFromForm(form) {
  for (const item of form?.items || []) {
    const itemDeposit = clean(item.stockDepositCode).toUpperCase();
    if (itemDeposit) return itemDeposit;
    for (const partida of item.partidas || []) {
      const partidaDeposit = clean(partida.stockDepositCode).toUpperCase();
      if (partidaDeposit) return partidaDeposit;
    }
  }
  return defaultDepositForDeliveryType(form?.deliveryType);
}

function suppressEnterSubmit(event) {
  if (event.key === "Enter") event.preventDefault();
}

export default function DeliveryOrderCreateForm({
  initialOrder = null,
  onCreated,
  onCancel,
  submitLabel,
  compact = false,
  readOnlyHeader = false,
  readOnlyItems = false,
  partidasRequired = false,
  partidasOpenByDefault = false,
  partidasSubmitRequiresAll = true,
  onPartidasSubmit = null,
  canEditItemDiscounts = false,
}) {
  const { user } = useAuth();
  const currentUserSellerCode =
    clean(user?.rol).toLowerCase() === "ventas" ? normalizeSellerCode(user?.bejermanSellerCode?.code) : "";
  const isEdit = Boolean(initialOrder?.id);
  const isPartidasMode = typeof onPartidasSubmit === "function";
  const initialFormState = formFromOrder(initialOrder, currentUserSellerCode);
  const [customers, setCustomers] = useState([]);
  const [form, setForm] = useState(() => initialFormState);
  const [depositCode, setDepositCode] = useState(() => depositFromForm(initialFormState));
  const [partidasOpenByItem, setPartidasOpenByItem] = useState(() =>
    partidasOpenStateForForm(initialFormState, partidasRequired || partidasOpenByDefault)
  );
  const [depositOptions, setDepositOptions] = useState(FALLBACK_DEPOSITS);
  const [loadingDeposits, setLoadingDeposits] = useState(false);
  const [depositWarning, setDepositWarning] = useState("");
  const [customerSearchOpen, setCustomerSearchOpen] = useState(false);
  const [loadingCustomers, setLoadingCustomers] = useState(false);
  const [customerActiveIndex, setCustomerActiveIndex] = useState(0);
  const [articleOptions, setArticleOptions] = useState({});
  const [articleLoading, setArticleLoading] = useState({});
  const [articleEmpty, setArticleEmpty] = useState({});
  const [articleStock, setArticleStock] = useState({});
  const [articleUnavailable, setArticleUnavailable] = useState(false);
  const [partidaScanWarnings, setPartidaScanWarnings] = useState({});
  const partidaInputRefs = useRef({});
  const [rentalSearch, setRentalSearch] = useState("");
  const [rentalEquipment, setRentalEquipment] = useState({ items: [], loading: false, unavailable: false, warning: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const effectiveCompanyKey = useMemo(() => normalizeCompanyKey(form.companyKey), [form.companyKey]);
  const isRental = form.deliveryType === "rental";
  const selectedRentalIngresoIds = useMemo(
    () => new Set((form.items || []).map((item) => clean(item.ingresoId)).filter(Boolean)),
    [form.items]
  );
  const sellerOptions = useMemo(
    () => sellerOptionsForCodes(currentUserSellerCode, form.sellerCode, initialOrder?.sellerCode),
    [currentUserSellerCode, form.sellerCode, initialOrder?.sellerCode]
  );
  const headerDisabled = saving || readOnlyHeader;
  const itemDisabled = saving || readOnlyItems;

  useEffect(() => {
    const nextForm = formFromOrder(initialOrder, currentUserSellerCode);
    setForm(nextForm);
    setDepositCode(depositFromForm(nextForm));
    setPartidasOpenByItem(partidasOpenStateForForm(nextForm, partidasRequired || partidasOpenByDefault));
    setDepositOptions(FALLBACK_DEPOSITS);
    setDepositWarning("");
    setLoadingDeposits(false);
    setCustomerSearchOpen(false);
    setCustomerActiveIndex(0);
    setArticleOptions({});
    setArticleLoading({});
    setArticleEmpty({});
    setArticleStock({});
    setArticleUnavailable(false);
    setPartidaScanWarnings({});
    partidaInputRefs.current = {};
    setRentalSearch("");
    setRentalEquipment({ items: [], loading: false, unavailable: false, warning: "" });
    setError("");
  }, [initialOrder?.id, currentUserSellerCode, partidasRequired, partidasOpenByDefault]);

  useEffect(() => {
    if (!isRental) return undefined;
    let active = true;
    setRentalEquipment((current) => ({ ...current, loading: true, warning: "" }));
    getDeliveryOrderRentalAvailableEquipment({
      q: rentalSearch,
      limit: 80,
      companyKey: effectiveCompanyKey,
    })
      .then((data) => {
        if (!active) return;
        setRentalEquipment({
          items: Array.isArray(data?.items) ? data.items : [],
          loading: false,
          unavailable: Boolean(data?.unavailable),
          warning: clean(data?.warning),
        });
      })
      .catch((err) => {
        if (!active) return;
        setRentalEquipment({
          items: [],
          loading: false,
          unavailable: true,
          warning: err?.message || "No se pudieron consultar equipos de alquiler disponibles.",
        });
      });
    return () => {
      active = false;
    };
  }, [isRental, rentalSearch, effectiveCompanyKey]);

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
        [customer.razon_social, customer.nombre, customer.alias_interno, customer.cod_empresa, customer.cuit, customer.telefono]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(q)
      )
      .slice(0, 30);
  }, [customers, form.customerSearch]);

  useEffect(() => {
    setCustomerActiveIndex((current) => {
      if (!filteredCustomers.length) return 0;
      return Math.min(current, filteredCustomers.length - 1);
    });
  }, [filteredCustomers.length]);

  const setAllItemDeposits = (nextDeposit) => {
    const normalized = clean(nextDeposit).toUpperCase() || defaultDepositForDeliveryType(form.deliveryType);
    setForm((current) => ({
      ...current,
      items: current.items.map((item) => ({
        ...item,
        stockDepositCode: normalized,
        stockAvailableQuantity: "",
        stockCheckedAt: "",
        partidas: (item.partidas || [emptyPartida()]).map((row) => ({
          ...row,
          stockDepositCode: normalized,
          stockAvailableQuantity: "",
          stockCheckedAt: "",
        })),
      })),
    }));
  };

  useEffect(() => {
    if (isRental) {
      setDepositCode("STL");
      setDepositOptions([{ code: "STL", label: "STL" }]);
      setDepositWarning("");
      setLoadingDeposits(false);
      return undefined;
    }
    let active = true;
    setLoadingDeposits(true);
    setDepositWarning("");
    getDeliveryOrderBejermanDeposits({ companyKey: effectiveCompanyKey })
      .then((data) => {
        if (!active) return;
        const parsed = (Array.isArray(data?.items) ? data.items : [])
          .map(normalizeDepositOption)
          .filter(Boolean);
        const nextOptions = parsed.length ? parsed : FALLBACK_DEPOSITS;
        setDepositOptions(nextOptions);
        setDepositWarning(clean(data?.warning));
        const normalized = clean(depositCode).toUpperCase();
        if (nextOptions.some((option) => option.code === normalized)) {
          setDepositCode(normalized);
        } else {
          const preferred = defaultDepositForDeliveryType(form.deliveryType);
          const next = nextOptions.some((option) => option.code === preferred) ? preferred : nextOptions[0]?.code || preferred;
          setAllItemDeposits(next);
          setArticleStock({});
          setDepositCode(next);
        }
      })
      .catch(() => {
        if (!active) return;
        setDepositOptions(FALLBACK_DEPOSITS);
        setDepositWarning("No fue posible consultar depósitos Bejerman. Se muestran depósitos estándar.");
        const normalized = clean(depositCode).toUpperCase();
        if (!FALLBACK_DEPOSITS.some((option) => option.code === normalized)) {
          const next = defaultDepositForDeliveryType(form.deliveryType);
          setAllItemDeposits(next);
          setArticleStock({});
          setDepositCode(next);
        }
      })
      .finally(() => {
        if (active) setLoadingDeposits(false);
      });
    return () => {
      active = false;
    };
  }, [effectiveCompanyKey, isRental]);

  const update = (field) => (event) => {
    if (readOnlyHeader) return;
    const value = event.target.value;
    if (field === "deliveryType") {
      const nextDeposit = defaultDepositForDeliveryType(value);
      setDepositCode(nextDeposit);
      if (value !== "rental") setAllItemDeposits(nextDeposit);
      setArticleStock({});
      setPartidaScanWarnings({});
      setRentalSearch("");
    } else if (field === "companyKey") {
      setArticleStock({});
      setPartidaScanWarnings({});
    }
    setForm((current) => {
      if (field === "deliveryType") {
        const currentSeller = sellerByCode(current.sellerCode);
        const nextSeller =
          value === "rental"
            ? sellerByCode(RENTAL_SELLER_CODE)
            : current.deliveryType === "rental"
              ? sellerByCode(currentUserSellerCode)
              : currentSeller;
        return {
          ...current,
          deliveryType: value,
          sellerCode: nextSeller.code,
          sellerName: nextSeller.label,
          items: value === "rental" ? [] : current.deliveryType === "rental" ? [emptyItem(current.priceCurrency)] : current.items,
        };
      }
      return { ...current, [field]: value };
    });
  };

  const changeSeller = (event) => {
    if (readOnlyHeader) return;
    if (isRental) return;
    const seller = sellerByCode(event.target.value);
    setForm((current) => ({
      ...current,
      sellerCode: seller.code,
      sellerName: seller.label,
    }));
  };

  const selectCustomer = (customer) => {
    if (readOnlyHeader) return;
    if (!customer) return;
    setForm((current) => ({
      ...current,
      customerId: String(customer.id || ""),
      customerName: customer.razon_social || customer.nombre || "",
      customerSearch: customer.razon_social || customer.nombre || "",
    }));
    setCustomerSearchOpen(false);
    setCustomerActiveIndex(0);
  };

  const handleCustomerSearchKeyDown = (event) => {
    if (readOnlyHeader) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCustomerSearchOpen(true);
      setCustomerActiveIndex((current) => (filteredCustomers.length ? (current + 1) % filteredCustomers.length : 0));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setCustomerSearchOpen(true);
      setCustomerActiveIndex((current) => (filteredCustomers.length ? (current - 1 + filteredCustomers.length) % filteredCustomers.length : 0));
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setCustomerSearchOpen(false);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (customerSearchOpen && filteredCustomers.length) {
        selectCustomer(filteredCustomers[customerActiveIndex] || filteredCustomers[0]);
      }
      return;
    }
    if (event.key === "Tab" && customerSearchOpen && filteredCustomers.length) {
      selectCustomer(filteredCustomers[customerActiveIndex] || filteredCustomers[0]);
    }
  };

  const updateItem = (index, changes) => {
    if (readOnlyItems) return;
    setForm((current) => ({
      ...current,
      items: current.items.map((item, itemIndex) => (itemIndex === index ? { ...item, ...changes } : item)),
    }));
  };

  const updatePartida = (itemIndex, partidaIndex, changes) => {
    setForm((current) => ({
      ...current,
      items: current.items.map((item, index) =>
        index === itemIndex
          ? {
              ...item,
              partidas: (item.partidas || [emptyPartida()]).map((row, rowIndex) =>
                rowIndex === partidaIndex ? { ...row, ...changes } : row
              ),
            }
          : item
      ),
    }));
  };

  const setPartidaScanWarning = (itemIndex, partidaIndex, message) => {
    const key = partidaInputKey(itemIndex, partidaIndex);
    setPartidaScanWarnings((current) => ({ ...current, [key]: message }));
  };

  const clearPartidaScanWarning = (itemIndex, partidaIndex) => {
    const key = partidaInputKey(itemIndex, partidaIndex);
    setPartidaScanWarnings((current) => {
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  };

  const focusPartidaInput = (itemIndex, partidaIndex) => {
    window.setTimeout(() => {
      const input = partidaInputRefs.current[partidaInputKey(itemIndex, partidaIndex)];
      input?.focus();
      input?.select();
    }, 0);
  };

  const changePartidaValue = (itemIndex, partidaIndex, value) => {
    clearPartidaScanWarning(itemIndex, partidaIndex);
    updatePartida(itemIndex, partidaIndex, { partida: value });
  };

  const handlePartidaScanKeyDown = (event, itemIndex, partidaIndex, stockLots) => {
    if (event.key !== "Enter" && event.key !== "Tab") return;
    if (event.key === "Enter") event.preventDefault();

    const scannedPartida = clean(event.currentTarget.value);
    if (!scannedPartida) {
      if (event.key === "Tab") return;
      setPartidaScanWarning(itemIndex, partidaIndex, "Ingrese una partida.");
      return;
    }

    event.preventDefault();
    const item = form.items[itemIndex] || {};
    const rows = item.partidas || [emptyPartida()];
    const stockState = articleStock[itemIndex];
    if (!clean(item.articleCode)) {
      setPartidaScanWarning(itemIndex, partidaIndex, "Seleccione un artículo antes de escanear partidas.");
      focusPartidaInput(itemIndex, partidaIndex);
      return;
    }
    if (stockState?.loading) {
      setPartidaScanWarning(itemIndex, partidaIndex, "Espere a que terminen de cargar las partidas disponibles.");
      focusPartidaInput(itemIndex, partidaIndex);
      return;
    }

    const scannedKey = partidaMatchKey(scannedPartida);
    const matchingLot = (stockLots || []).find((lot) => partidaMatchKey(lot?.partida) === scannedKey);
    if (!matchingLot) {
      setPartidaScanWarning(itemIndex, partidaIndex, "La partida escaneada no figura para este artículo en el depósito seleccionado.");
      focusPartidaInput(itemIndex, partidaIndex);
      return;
    }

    const duplicate = rows.some((row, rowIndex) => rowIndex !== partidaIndex && partidaMatchKey(row.partida) === scannedKey);
    if (duplicate) {
      setPartidaScanWarning(itemIndex, partidaIndex, "La partida ya está cargada en este renglón.");
      focusPartidaInput(itemIndex, partidaIndex);
      return;
    }

    const requested = quantityNumber(item.quantity, 1);
    const assignedOther = rows.reduce(
      (sum, row, rowIndex) => sum + (rowIndex === partidaIndex ? 0 : quantityNumber(row.assignedQuantity, 0)),
      0
    );
    const remaining = Math.max(0, requested - assignedOther);
    if (remaining <= QUANTITY_TOLERANCE) {
      setPartidaScanWarning(itemIndex, partidaIndex, "La cantidad del renglón ya está completa.");
      focusPartidaInput(itemIndex, partidaIndex);
      return;
    }

    const available = lotAvailableQuantity(matchingLot);
    const assignedQuantity = Math.min(1, remaining, available > 0 ? available : 1);
    const nextRows = rows.map((row, rowIndex) =>
      rowIndex === partidaIndex ? { ...row, ...lotDraft(matchingLot, assignedQuantity) } : row
    );
    const remainingAfterScan = remaining - assignedQuantity;
    let nextFocusIndex = partidaIndex;
    if (remainingAfterScan > QUANTITY_TOLERANCE) {
      const nextEmptyIndex = nextRows.findIndex((row, rowIndex) => rowIndex !== partidaIndex && !clean(row.partida));
      if (nextEmptyIndex >= 0) {
        nextFocusIndex = nextEmptyIndex;
      } else {
        nextFocusIndex = nextRows.length;
        nextRows.push(emptyPartida("1", clean(matchingLot?.stockDepositCode || matchingLot?.depositCode) || clean(item.stockDepositCode) || depositCode));
      }
    }

    setForm((current) => ({
      ...current,
      items: current.items.map((currentItem, index) =>
        index === itemIndex ? { ...currentItem, partidas: nextRows } : currentItem
      ),
    }));
    clearPartidaScanWarning(itemIndex, partidaIndex);
    focusPartidaInput(itemIndex, nextFocusIndex);
  };

  const handlePartidaBlur = (itemIndex, partidaIndex, stockLots) => {
    const item = form.items[itemIndex] || {};
    const rows = item.partidas || [emptyPartida()];
    const row = rows[partidaIndex] || {};
    const partida = clean(row.partida);
    if (!partida) {
      clearPartidaScanWarning(itemIndex, partidaIndex);
      return;
    }
    const stockState = articleStock[itemIndex];
    if (!clean(item.articleCode) || stockState?.loading || stockState?.unavailable) return;
    const lots = Array.isArray(stockLots) ? stockLots : [];
    if (!stockState || !lots.length) return;
    const matchingLot = lots.find((lot) => partidaMatchKey(lot?.partida) === partidaMatchKey(partida));
    if (!matchingLot) {
      setPartidaScanWarning(itemIndex, partidaIndex, "La partida no figura para este artículo en el depósito seleccionado.");
      return;
    }
    const assignedQuantity = clean(row.assignedQuantity) || "1";
    updatePartida(itemIndex, partidaIndex, lotDraft(matchingLot, assignedQuantity));
    clearPartidaScanWarning(itemIndex, partidaIndex);
  };

  const addItem = () => {
    if (readOnlyItems) return;
    setForm((current) => ({ ...current, items: [...current.items, emptyItem(current.priceCurrency)] }));
  };

  const addRentalEquipment = (option) => {
    if (readOnlyItems) return;
    const ingresoId = clean(option?.ingresoId);
    if (!ingresoId || selectedRentalIngresoIds.has(ingresoId)) return;
    setForm((current) => ({
      ...current,
      items: [...current.items, { ...equipmentItemFromOption(option), priceCurrency: normalizePriceCurrency(current.priceCurrency) }],
    }));
  };

  const removeItem = (index) => {
    if (readOnlyItems) return;
    setForm((current) => ({
      ...current,
      items: current.items.length > 1 || current.deliveryType === "rental"
        ? current.items.filter((_, itemIndex) => itemIndex !== index)
        : current.items,
    }));
    setArticleOptions((current) => shiftIndexedState(current, index));
    setArticleLoading((current) => shiftIndexedState(current, index));
    setArticleEmpty((current) => shiftIndexedState(current, index));
    setArticleStock((current) => shiftIndexedState(current, index));
    setPartidasOpenByItem((current) => shiftIndexedState(current, index));
    setPartidaScanWarnings({});
    partidaInputRefs.current = {};
  };

  const addPartida = (itemIndex) => {
    openPartidas(itemIndex);
    setForm((current) => ({
      ...current,
      items: current.items.map((item, index) => {
        if (index !== itemIndex) return item;
        const stats = partidaStats(item);
        const assignedQuantity = stats.pendingNumber > 0 ? String(stats.pendingNumber) : "1";
        return { ...item, partidas: [...(item.partidas || []), emptyPartida(assignedQuantity, depositCode)] };
      }),
    }));
  };

  const removePartida = (itemIndex, partidaIndex) => {
    setForm((current) => ({
      ...current,
      items: current.items.map((item, index) => {
        if (index !== itemIndex) return item;
        const next = (item.partidas || []).filter((_, rowIndex) => rowIndex !== partidaIndex);
        return { ...item, partidas: next.length ? next : [emptyPartida()] };
      }),
    }));
    setPartidaScanWarnings({});
  };

  const openPartidas = (itemIndex) => {
    setPartidasOpenByItem((current) => ({ ...current, [itemIndex]: true }));
    const item = form.items[itemIndex] || {};
    const articleCode = clean(item.articleCode);
    if (articleCode) {
      void loadArticleStock(itemIndex, articleCode, clean(item.stockDepositCode) || depositCode);
    }
  };

  const closePartidas = (itemIndex) => {
    if (partidasRequired) return;
    setPartidasOpenByItem((current) => ({ ...current, [itemIndex]: false }));
  };

  const searchArticles = async (index, value) => {
    if (readOnlyItems) return;
    setPartidaScanWarnings({});
    updateItem(index, {
      articleCode: "",
      articleName: value,
      description: value,
      partida: "",
      partidaExpirationDate: "",
      stockDepositCode: depositCode,
      stockAvailableQuantity: "",
      stockCheckedAt: "",
      partidas: [emptyPartida()],
    });
    setArticleStock((current) => ({ ...current, [index]: null }));

    const q = clean(value);
    if (q.length < 2) {
      setArticleOptions((current) => ({ ...current, [index]: [] }));
      setArticleEmpty((current) => ({ ...current, [index]: false }));
      return;
    }

    setArticleLoading((current) => ({ ...current, [index]: true }));
    setArticleEmpty((current) => ({ ...current, [index]: false }));
    try {
      const data = await getDeliveryOrderBejermanArticles({ q, search: q, limit: 8, companyKey: effectiveCompanyKey });
      const items = Array.isArray(data?.items) ? data.items : [];
      setArticleUnavailable(Boolean(data?.unavailable));
      setArticleOptions((current) => ({ ...current, [index]: items }));
      setArticleEmpty((current) => ({ ...current, [index]: items.length === 0 }));
    } catch {
      setArticleUnavailable(true);
      setArticleOptions((current) => ({ ...current, [index]: [] }));
      setArticleEmpty((current) => ({ ...current, [index]: true }));
    } finally {
      setArticleLoading((current) => ({ ...current, [index]: false }));
    }
  };

  const loadArticleStock = async (index, articleCode, requestedDepositCode = depositCode) => {
    const code = clean(articleCode);
    const selectedDeposit = clean(requestedDepositCode).toUpperCase() || defaultDepositForDeliveryType(form.deliveryType);
    if (!code) {
      setArticleStock((current) => ({ ...current, [index]: null }));
      return;
    }
    setArticleStock((current) => ({
      ...current,
      [index]: { items: [], loading: true, unavailable: false, warning: "", depositCode: selectedDeposit },
    }));
    try {
      const data = await getDeliveryOrderBejermanArticleStock({
        articleCode: code,
        limit: 100,
        companyKey: effectiveCompanyKey,
        deliveryType: form.deliveryType,
        depositCode: selectedDeposit,
      });
      setArticleStock((current) => ({
        ...current,
        [index]: {
          items: Array.isArray(data?.items) ? data.items : [],
          loading: false,
          unavailable: Boolean(data?.unavailable),
          warning: clean(data?.warning),
          depositCode: clean(data?.depositCode) || selectedDeposit,
        },
      }));
    } catch (err) {
      setArticleStock((current) => ({
        ...current,
        [index]: {
          items: [],
          loading: false,
          unavailable: true,
          warning: err?.message || "No se pudo verificar stock Bejerman.",
          depositCode: selectedDeposit,
        },
      }));
    }
  };

  useEffect(() => {
    if (isRental || (!partidasRequired && !partidasOpenByDefault)) return;
    form.items.forEach((item, index) => {
      const articleCode = clean(item.articleCode);
      if (articleCode) {
        void loadArticleStock(index, articleCode, clean(item.stockDepositCode) || depositCode);
      }
    });
  }, [initialOrder?.id, isRental, partidasRequired, partidasOpenByDefault, effectiveCompanyKey, depositCode]);

  const changeDeposit = (event) => {
    if (readOnlyItems) return;
    const nextDeposit = clean(event.target.value).toUpperCase() || defaultDepositForDeliveryType(form.deliveryType);
    setDepositCode(nextDeposit);
    setAllItemDeposits(nextDeposit);
    setArticleStock({});
    setPartidaScanWarnings({});
    form.items.forEach((item, index) => {
      if ((partidasRequired || partidasOpenByItem[index]) && clean(item.articleCode)) {
        void loadArticleStock(index, item.articleCode, nextDeposit);
      }
    });
  };

  const chooseArticle = (index, article) => {
    if (readOnlyItems) return;
    setPartidaScanWarnings({});
    const articleCode = clean(article?.code);
    const articleName = clean(article?.name || article?.description || articleCode);
    updateItem(index, {
      articleCode,
      articleName,
      description: clean(article?.description) || articleName || articleCode,
      partida: "",
      partidaExpirationDate: "",
      stockDepositCode: depositCode,
      stockAvailableQuantity: "",
      stockCheckedAt: "",
      partidas: [emptyPartida()],
    });
    setArticleOptions((current) => ({ ...current, [index]: [] }));
    setArticleEmpty((current) => ({ ...current, [index]: false }));
    setArticleStock((current) => ({ ...current, [index]: null }));
    if (partidasRequired || partidasOpenByItem[index]) {
      void loadArticleStock(index, articleCode, depositCode);
    }
  };

  const chooseLot = (itemIndex, partidaIndex, lot) => {
    clearPartidaScanWarning(itemIndex, partidaIndex);
    const item = form.items[itemIndex] || {};
    const requested = quantityNumber(item.quantity, 1);
    const currentRows = item.partidas || [];
    const assignedOther = currentRows.reduce(
      (sum, row, index) => sum + (index === partidaIndex ? 0 : quantityNumber(row.assignedQuantity, 0)),
      0
    );
    const remaining = Math.max(0, requested - assignedOther);
    const available = lotAvailableQuantity(lot);
    const assigned = available > 0 ? Math.min(remaining || requested, available) : remaining || requested;
    updatePartida(itemIndex, partidaIndex, lotDraft(lot, assigned));
  };

  const buildPartidasPayloadForItem = (item) =>
    activePartidas(item).map((row, rowIndex) => ({
      id: clean(row.id) || null,
      partida: clean(row.partida),
      assignedQuantity: quantityOf(row.assignedQuantity),
      partidaExpirationDate: clean(row.partidaExpirationDate) || null,
      stockDepositCode: clean(row.stockDepositCode) || depositCode || null,
      stockAvailableQuantity: decimalTextOrNull(row.stockAvailableQuantity),
      stockCheckedAt: clean(row.stockCheckedAt) || null,
      sortOrder: rowIndex,
    }));

  const validateItems = (items, { requirePartidas = false } = {}) => {
    if (isRental) {
      if (!items.length) return "Seleccione al menos un equipo disponible para alquilar.";
      const seen = new Set();
      for (let index = 0; index < items.length; index += 1) {
        const item = items[index];
        if (!clean(item.ingresoId) || !clean(item.deviceId)) return `El renglón ${index + 1} debe estar vinculado a un equipo.`;
        if (seen.has(clean(item.ingresoId))) return `La OS ${item.ingresoId} está repetida en la orden.`;
        seen.add(clean(item.ingresoId));
        if (quantityNumber(item.quantity, 0) !== 1) return `La cantidad del renglón ${index + 1} debe ser 1.`;
        if (!clean(item.articleCode)) return `La OS ${item.ingresoId} no tiene artículo Bejerman mapeado.`;
        if (!clean(item.partida)) return `La OS ${item.ingresoId} no tiene serie/partida.`;
        if (canEditItemDiscounts) {
          const discount = discountNumber(item.discountPercent);
          if (discount === null || discount < 0 || discount > 100) return `El descuento del renglón ${index + 1} debe estar entre 0 y 100.`;
        }
        if (clean(item.stockDepositCode).toUpperCase() !== "STL") return `La OS ${item.ingresoId} debe salir de STL.`;
        if (quantityNumber(item.stockAvailableQuantity, 0) <= 0) return `La OS ${item.ingresoId} no tiene stock disponible en STL.`;
      }
      return "";
    }
    for (let index = 0; index < items.length; index += 1) {
      const item = items[index];
      const description = clean(item.description) || clean(item.articleName) || clean(item.articleCode);
      if (!description) return `El renglón ${index + 1} necesita un detalle.`;
      const total = quantityNumber(item.quantity, 0);
      if (total <= 0) return `La cantidad del renglón ${index + 1} debe ser mayor a cero.`;
      if (canEditItemDiscounts) {
        const discount = discountNumber(item.discountPercent);
        if (discount === null || discount < 0 || discount > 100) return `El descuento del renglón ${index + 1} debe estar entre 0 y 100.`;
      }
      const rows = activePartidas(item);
      if (!rows.length) {
        if (requirePartidas) return `Complete las partidas del renglón ${index + 1}.`;
        continue;
      }
      let assigned = 0;
      const seenPartidas = new Set();
      for (const row of rows) {
        const rowPartida = clean(row.partida);
        if (!rowPartida || quantityNumber(row.assignedQuantity, 0) <= 0) {
          return `Cada partida del renglón ${index + 1} necesita número y cantidad.`;
        }
        const rowPartidaKey = rowPartida.toLowerCase();
        if (seenPartidas.has(rowPartidaKey)) {
          return `La partida ${rowPartida} ya está cargada en el renglón ${index + 1}.`;
        }
        seenPartidas.add(rowPartidaKey);
        const stockState = articleStock[index];
        const stockLots = Array.isArray(stockState?.items) ? stockState.items : [];
        if (clean(item.articleCode) && stockState && !stockState.loading && !stockState.unavailable && stockLots.length) {
          const matchingLot = stockLots.find((lot) => partidaMatchKey(lot?.partida) === rowPartidaKey);
          if (!matchingLot) {
            return `La partida ${rowPartida} no figura para este artículo en el depósito seleccionado.`;
          }
          if (quantityNumber(row.assignedQuantity, 0) - lotAvailableQuantity(matchingLot) > QUANTITY_TOLERANCE) {
            return `La partida ${rowPartida} no tiene stock suficiente en el depósito seleccionado.`;
          }
        }
        assigned += quantityNumber(row.assignedQuantity, 0);
      }
      if (Math.abs(assigned - total) > QUANTITY_TOLERANCE) {
        return `La suma de las partidas del renglón ${index + 1} debe ser igual a ${formatQuantity(total)}.`;
      }
    }
    return "";
  };

  const buildItemsPayload = () =>
    form.items
      .map((item, index) => {
        const description = clean(item.description) || clean(item.articleName) || clean(item.articleCode);
        const partidas = buildPartidasPayloadForItem(item);
        return {
          id: clean(item.id) || null,
          ingresoId: clean(item.ingresoId) ? Number(item.ingresoId) : null,
          deviceId: clean(item.deviceId) ? Number(item.deviceId) : null,
          articleCode: clean(item.articleCode) || null,
          articleName: clean(item.articleName) || null,
          description,
          quantity: quantityOf(item.quantity),
          unitPrice: decimalTextOrNull(item.unitPrice),
          priceCurrency: normalizePriceCurrency(item.priceCurrency, form.priceCurrency),
          ...(canEditItemDiscounts ? { discountPercent: decimalTextOrNull(item.discountPercent) || "0" } : {}),
          partida: partidas.length ? null : clean(item.partida) || null,
          partidaExpirationDate: partidas.length ? null : clean(item.partidaExpirationDate) || null,
          stockDepositCode: partidas.length ? null : clean(item.stockDepositCode) || depositCode || null,
          stockAvailableQuantity: partidas.length ? null : decimalTextOrNull(item.stockAvailableQuantity),
          stockCheckedAt: partidas.length ? null : clean(item.stockCheckedAt) || null,
          sortOrder: index,
          partidas,
        };
      })
      .filter((item) => item.description);

  const buildPartidasPayload = () =>
    form.items.map((item) => ({
      itemId: clean(item.id),
      partidas: buildPartidasPayloadForItem(item),
    }));

  const saveOrder = async (event) => {
    event.preventDefault();
    const validationError = validateItems(form.items, {
      requirePartidas: partidasRequired || (isPartidasMode && partidasSubmitRequiresAll),
    });

    if (validationError) {
      setError(validationError);
      return;
    }

    if (isPartidasMode) {
      const partidasPayload = buildPartidasPayload();
      if (partidasPayload.some((item) => !item.itemId)) {
        setError("No se pueden guardar partidas porque falta el identificador de un renglón.");
        return;
      }
      setSaving(true);
      setError("");
      try {
        const saved = await onPartidasSubmit(partidasPayload);
        if (onCreated) onCreated(saved);
      } catch (err) {
        setError(err?.message || "No se pudieron guardar las partidas.");
      } finally {
        setSaving(false);
      }
      return;
    }

    const customer = customerById.get(String(form.customerId));
    const items = buildItemsPayload();
    const rawPedido = clean(form.rawPedido) || items.map((item) => item.description).join("\n");
    if (!clean(form.customerName)) {
      setError("Cliente en remito es requerido.");
      return;
    }
    if (!clean(form.companyKey)) {
      setError("Empresa Bejerman es requerida.");
      return;
    }
    if (!rawPedido || !items.length) {
      setError("Cargue al menos un renglón de artículo o detalle.");
      return;
    }

    const effectiveSellerCode = isRental ? RENTAL_SELLER_CODE : normalizeSellerCode(form.sellerCode);
    const payload = {
      customerId: form.customerId ? Number(form.customerId) : null,
      customerName: clean(form.customerName),
      bejermanCustomerCode: customer?.cod_empresa || initialOrder?.bejermanCustomerCode || "",
      deliveryType: form.deliveryType,
      priority: form.priority,
      sellerName: sellerNameForCode(effectiveSellerCode, form.sellerName),
      sellerCode: effectiveSellerCode,
      orderDate: form.orderDate,
      companyKey: effectiveCompanyKey,
      operationCompanyLabel: clean(form.operationCompanyLabel),
      priceCurrency: form.priceCurrency === "USD" ? "USD" : "ARS",
      rawPedido,
      commercialExchangeRate: clean(form.commercialExchangeRate) || null,
      commercialCondition: clean(form.commercialCondition) || null,
      items,
    };

    setSaving(true);
    setError("");
    try {
      const saved = isEdit
        ? await patchDeliveryOrder(initialOrder.id, payload)
        : await postDeliveryOrder(payload);
      if (!isEdit) {
        const resetForm = initialForm(currentUserSellerCode);
        setForm(resetForm);
        setDepositCode(depositFromForm(resetForm));
        setCustomerSearchOpen(false);
        setArticleOptions({});
        setArticleLoading({});
        setArticleEmpty({});
        setArticleStock({});
        setArticleUnavailable(false);
        setRentalSearch("");
        setRentalEquipment({ items: [], loading: false, unavailable: false, warning: "" });
      }
      if (onCreated) onCreated(saved);
    } catch (err) {
      setError(err?.message || (isEdit ? "No se pudo guardar la entrega." : "No se pudo crear la entrega."));
    } finally {
      setSaving(false);
    }
  };

  const hasPartidaQuantityMismatch = !isRental && form.items.some((item) => partidaStats(item).mismatch);

  return (
    <form onSubmit={saveOrder} className={compact ? "space-y-4" : "space-y-5"}>
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Concepto</span>
          <select
            value={form.deliveryType}
            onChange={update("deliveryType")}
            className="h-10 w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
          >
            <option value="sale">Venta</option>
            <option value="rental">Alquiler</option>
            <option value="demo">Demo</option>
          </select>
        </label>

        <label className="relative text-sm md:col-span-2">
          <span className="mb-1 block text-xs uppercase text-gray-500">Cliente</span>
          <input
            value={form.customerSearch}
            onFocus={() => {
              if (readOnlyHeader) return;
              setCustomerSearchOpen(true);
              setCustomerActiveIndex(0);
            }}
            onKeyDown={handleCustomerSearchKeyDown}
            onChange={(event) => {
              if (readOnlyHeader) return;
              setForm((current) => ({
                ...current,
                customerId: "",
                customerSearch: event.target.value,
              }));
              setCustomerActiveIndex(0);
              setCustomerSearchOpen(true);
            }}
            className="h-10 w-full rounded border px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={loadingCustomers || headerDisabled}
          />
          {!readOnlyHeader && customerSearchOpen && (
            <div className="absolute z-30 mt-1 max-h-64 w-full overflow-y-auto rounded border bg-white shadow-lg">
              {filteredCustomers.length ? (
                filteredCustomers.map((customer, customerIndex) => (
                  <button
                    key={customer.id}
                    type="button"
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setCustomerActiveIndex(customerIndex)}
                    onClick={() => selectCustomer(customer)}
                    className={`w-full px-3 py-2 text-left text-sm ${customerIndex === customerActiveIndex ? "bg-slate-100" : "hover:bg-gray-50"}`}
                  >
                    <span className="block truncate font-medium">{customer.razon_social || customer.nombre}</span>
                    <span className="block truncate text-xs text-gray-500">
                      {customerDisplayMeta(customer)}
                    </span>
                  </button>
                ))
              ) : (
                <div className="px-3 py-2 text-xs text-gray-500">Sin coincidencias.</div>
              )}
            </div>
          )}
        </label>

        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Cliente en remito</span>
          <input
            value={form.customerName}
            onChange={update("customerName")}
            onKeyDown={suppressEnterSubmit}
            className="h-10 w-full rounded border px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
            required
          />
        </label>

        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Vendedor</span>
          <select
            value={form.sellerCode}
            onChange={changeSeller}
            className="h-10 w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled || isRental}
          >
            {sellerOptions.map((seller) => (
              <option key={seller.code} value={seller.code}>
                {seller.label}
              </option>
            ))}
          </select>
          <span className="mt-1 block truncate text-xs text-gray-500">
            {isRental ? "Alquiler usa ADM." : sellerNameForCode(form.sellerCode, form.sellerName) || "Sin código."}
          </span>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Fecha</span>
          <input
            type="date"
            value={form.orderDate}
            onChange={update("orderDate")}
            className="h-10 w-full rounded border px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
            required
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Empresa Bejerman</span>
          <select
            value={form.companyKey}
            onChange={update("companyKey")}
            className="h-10 w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
            required
          >
            {BEJERMAN_COMPANIES.map((company) => (
              <option key={company.key} value={company.key}>
                {company.label}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Prioridad</span>
          <select
            value={form.priority}
            onChange={update("priority")}
            className="h-10 w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
          >
            <option value="normal">Normal</option>
            <option value="urgente">Urgente</option>
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">TC</span>
          <input
            value={form.commercialExchangeRate}
            onChange={update("commercialExchangeRate")}
            onKeyDown={suppressEnterSubmit}
            className="h-10 w-full rounded border px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Moneda default</span>
          <select
            value={form.priceCurrency}
            onChange={update("priceCurrency")}
            className="h-10 w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
          >
            <option value="ARS">$</option>
            <option value="USD">U$S</option>
          </select>
        </label>
        <div className="rounded border bg-gray-50 px-3 py-2 text-sm text-gray-700">
          <span className="mb-1 block text-xs uppercase text-gray-500">Perfil</span>
          <span>{REMITO_PROFILE_BY_TYPE[form.deliveryType] || "-"}</span>
        </div>
        <label className="text-sm xl:col-span-2">
          <span className="mb-1 block text-xs uppercase text-gray-500">Condición</span>
          <input
            value={form.commercialCondition}
            onChange={update("commercialCondition")}
            onKeyDown={suppressEnterSubmit}
            className="h-10 w-full rounded border px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
            disabled={headerDisabled}
          />
        </label>
      </div>

      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-900">{isRental ? "Equipos de alquiler" : "Artículos"}</h3>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-gray-600">
              <span className="uppercase">Depósito</span>
              <select
                value={depositCode}
                onChange={changeDeposit}
                className="h-8 min-w-[92px] rounded border bg-white px-2 text-sm text-gray-900 disabled:bg-gray-100 disabled:text-gray-600"
                disabled={itemDisabled || isRental || loadingDeposits || !form.companyKey}
              >
                {depositOptions.map((option) => (
                  <option key={option.code} value={option.code}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            {!readOnlyItems && !isRental && (
              <button type="button" onClick={addItem} className="rounded border px-3 py-1.5 text-sm hover:bg-gray-50">
                Agregar renglón
              </button>
            )}
          </div>
        </div>
        {depositWarning && (
          <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            {depositWarning}
          </div>
        )}
        {!isRental && articleUnavailable && (
          <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            Catálogo Bejerman no disponible. Se puede cargar texto libre.
          </div>
        )}

        {isRental && (
          <div className="rounded border bg-gray-50 p-3">
            <label className="block text-sm">
              <span className="mb-1 block text-xs uppercase text-gray-500">Buscar equipo disponible</span>
              <input
                value={rentalSearch}
                onChange={(event) => setRentalSearch(event.target.value)}
                onKeyDown={suppressEnterSubmit}
                placeholder="OS, serie, MG, modelo o artículo"
                className="h-10 w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
                disabled={itemDisabled}
              />
            </label>
            {rentalEquipment.warning && (
              <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                {rentalEquipment.warning}
              </div>
            )}
            {rentalEquipment.loading ? (
              <div className="mt-3 text-sm text-gray-500">Consultando equipos en Estantería de Alquiler...</div>
            ) : (
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {(rentalEquipment.items || [])
                  .filter((option) => !selectedRentalIngresoIds.has(clean(option.ingresoId)))
                  .slice(0, 12)
                  .map((option) => (
                    <button
                      key={`${option.ingresoId}-${option.deviceId}`}
                      type="button"
                      onClick={() => addRentalEquipment(option)}
                      className="rounded border bg-white px-3 py-2 text-left text-sm hover:bg-gray-50 disabled:opacity-50"
                      disabled={itemDisabled}
                    >
                      <span className="block font-semibold text-gray-900">{option.sourceReference || formatServiceOrderReference(option.ingresoId)}</span>
                      <span className="mt-0.5 block text-gray-700">{option.equipmentDetail || option.equipmentModel}</span>
                      <span className="mt-1 block text-xs text-gray-500">
                        Serie/partida {option.partida || option.equipmentSerial || "-"} · Artículo {option.articleCode || "-"} · STL {formatQuantity(option.stockAvailableQuantity, 0)}
                      </span>
                    </button>
                  ))}
                {!rentalEquipment.loading &&
                  (rentalEquipment.items || []).filter((option) => !selectedRentalIngresoIds.has(clean(option.ingresoId))).length === 0 && (
                    <div className="rounded border bg-white px-3 py-2 text-sm text-gray-500">Sin equipos seleccionables para el filtro actual.</div>
                  )}
              </div>
            )}
          </div>
        )}

        {isRental &&
          form.items.map((item, index) => (
            <div key={item.id || `${item.ingresoId}-${index}`} className="rounded border bg-gray-50 p-3">
              <div className={`grid grid-cols-1 gap-3 ${canEditItemDiscounts ? "lg:grid-cols-[110px_minmax(220px,1fr)_minmax(180px,.8fr)_105px_74px_82px_42px]" : "lg:grid-cols-[110px_minmax(220px,1fr)_minmax(180px,.8fr)_105px_74px_42px]"}`}>
                <div className="text-sm">
                  <span className="block text-xs uppercase text-gray-500">OS</span>
                  <span className="font-semibold text-gray-900">{item.ingresoId ? formatServiceOrderReference(item.ingresoId) : "-"}</span>
                </div>
                <div className="text-sm">
                  <span className="block text-xs uppercase text-gray-500">Equipo</span>
                  <span className="text-gray-900">{item.description || item.articleName || item.articleCode || "-"}</span>
                  <span className="mt-1 block text-xs text-gray-500">Serie/partida {item.partida || "-"}</span>
                </div>
                <div className="text-sm">
                  <span className="block text-xs uppercase text-gray-500">Artículo Bejerman</span>
                  <span className="font-mono text-sky-800">{item.articleCode || "-"}</span>
                  <span className="mt-1 block text-xs text-gray-500">{item.articleName || "-"}</span>
                </div>
                <label className="text-sm">
                  <span className="block text-xs uppercase text-gray-500">Precio</span>
                  <input
                    value={item.unitPrice}
                    onChange={(event) => updateItem(index, { unitPrice: event.target.value })}
                    onKeyDown={suppressEnterSubmit}
                    className="mt-1 h-9 w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
                    disabled={itemDisabled}
                  />
                  <span className="mt-1 block text-xs text-gray-500">STL {formatQuantity(item.stockAvailableQuantity, 0)}</span>
                </label>
                <label className="text-sm">
                  <span className="block text-xs uppercase text-gray-500">Moneda</span>
                  <select
                    value={item.priceCurrency}
                    onChange={(event) => updateItem(index, { priceCurrency: event.target.value })}
                    className="mt-1 h-9 w-full rounded border bg-white px-2 py-2 disabled:bg-gray-100 disabled:text-gray-600"
                    disabled={itemDisabled}
                  >
                    <option value="ARS">$</option>
                    <option value="USD">U$S</option>
                  </select>
                </label>
                {canEditItemDiscounts && (
                  <label className="text-sm">
                    <span className="block text-xs uppercase text-gray-500">Desc. %</span>
                    <input
                      value={item.discountPercent}
                      onChange={(event) => updateItem(index, { discountPercent: event.target.value })}
                      onKeyDown={suppressEnterSubmit}
                      placeholder="0"
                      className="mt-1 h-9 w-full rounded border bg-white px-2 py-2 text-right disabled:bg-gray-100 disabled:text-gray-600"
                      disabled={itemDisabled}
                    />
                  </label>
                )}
                {!readOnlyItems ? (
                  <button
                    type="button"
                    onClick={() => removeItem(index)}
                    className="h-9 rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-40"
                    aria-label="Quitar equipo"
                    disabled={saving}
                  >
                    X
                  </button>
                ) : (
                  <div aria-hidden="true" />
                )}
              </div>
            </div>
          ))}

        {!isRental && form.items.map((item, index) => {
          const stats = partidaStats(item);
          const partidaQuantityMessage = stats.mismatch
            ? `La suma de las cantidades por partida debe ser igual a la cantidad del artículo (${stats.total}).`
            : "";
          const stockState = articleStock[index];
          const stockLots = Array.isArray(stockState?.items) ? stockState.items : [];
          const partidaRows = item.partidas || [emptyPartida()];
          const suggestedPartidaIndex = Math.max(0, partidaRows.length - 1);
          const suggestedPartidaQuery = clean(partidaRows[suggestedPartidaIndex]?.partida).toLowerCase();
          const usedPartidas = new Set(
            partidaRows
              .map((row, rowIndex) => (rowIndex === suggestedPartidaIndex ? "" : clean(row.partida).toLowerCase()))
              .filter(Boolean)
          );
          const visibleStockLots = stockLots.filter((lot) => {
            const lotPartida = clean(lot?.partida).toLowerCase();
            if (lotPartida && usedPartidas.has(lotPartida)) return false;
            if (!suggestedPartidaQuery) return true;
            return lotLabel(lot).toLowerCase().includes(suggestedPartidaQuery);
          });
          const partidasOpen = partidasRequired || Boolean(partidasOpenByItem[index]) || stats.hasRows;
          return (
            <div key={item.id || index} className="rounded border bg-gray-50 p-2">
              <div className={`grid grid-cols-1 gap-2 ${canEditItemDiscounts ? "lg:grid-cols-[82px_minmax(180px,1fr)_minmax(220px,1.25fr)_105px_74px_82px_42px]" : "lg:grid-cols-[82px_minmax(180px,1fr)_minmax(220px,1.25fr)_105px_74px_42px]"}`}>
                <input
                  value={item.quantity}
                  onChange={(event) => updateItem(index, { quantity: event.target.value })}
                  onKeyDown={suppressEnterSubmit}
                  placeholder="Cant."
                  className={`rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600 ${stats.mismatch ? "border-red-400 text-red-900 focus:outline-red-500" : ""}`}
                  disabled={itemDisabled}
                />
                <div className="relative">
                  <input
                    value={item.articleCode || item.articleName}
                    onChange={(event) => searchArticles(index, event.target.value)}
                    onKeyDown={suppressEnterSubmit}
                    placeholder="Buscar por código o detalle"
                    className="w-full rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
                    disabled={itemDisabled}
                  />
                  {item.articleCode && (
                    <p className="mt-1 truncate text-[11px] text-gray-500">{item.articleName || item.articleCode}</p>
                  )}
                  {articleLoading[index] && (
                    <div className="absolute z-30 mt-1 w-full rounded border bg-white px-3 py-2 text-xs text-gray-500 shadow-lg">
                      Buscando artículos...
                    </div>
                  )}
                  {!readOnlyItems && !articleLoading[index] && (articleOptions[index] || []).length > 0 && (
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
                  {!articleLoading[index] && articleEmpty[index] && (
                    <div className="absolute z-30 mt-1 w-full rounded border bg-white px-3 py-2 text-xs text-gray-500 shadow-lg">
                      Sin coincidencias en Bejerman.
                    </div>
                  )}
                </div>
                <textarea
                  value={item.description}
                  onChange={(event) => updateItem(index, { description: event.target.value })}
                  placeholder="Detalle del renglón"
                  rows={2}
                  className="rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
                  disabled={itemDisabled}
                />
                <input
                  value={item.unitPrice}
                  onChange={(event) => updateItem(index, { unitPrice: event.target.value })}
                  onKeyDown={suppressEnterSubmit}
                  placeholder="Precio"
                  className="rounded border bg-white px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
                  disabled={itemDisabled}
                />
                <select
                  value={item.priceCurrency}
                  onChange={(event) => updateItem(index, { priceCurrency: event.target.value })}
                  className="rounded border bg-white px-2 py-2 disabled:bg-gray-100 disabled:text-gray-600"
                  disabled={itemDisabled}
                  aria-label="Moneda del precio"
                >
                  <option value="ARS">$</option>
                  <option value="USD">U$S</option>
                </select>
                {canEditItemDiscounts && (
                  <input
                    value={item.discountPercent}
                    onChange={(event) => updateItem(index, { discountPercent: event.target.value })}
                    onKeyDown={suppressEnterSubmit}
                    placeholder="Desc. %"
                    className="rounded border bg-white px-2 py-2 text-right disabled:bg-gray-100 disabled:text-gray-600"
                    disabled={itemDisabled}
                  />
                )}
                {!readOnlyItems ? (
                  <button
                    type="button"
                    onClick={() => removeItem(index)}
                    disabled={form.items.length <= 1}
                    className="rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-40"
                    aria-label="Quitar renglón"
                  >
                    X
                  </button>
                ) : (
                  <div aria-hidden="true" />
                )}
              </div>

              <div className="mt-2 rounded border border-gray-200 bg-white p-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-xs font-medium uppercase text-gray-500">Partidas</div>
                    {!partidasOpen && (
                      <div className="mt-0.5 text-xs text-gray-500">Opcional al crear la orden. Recepción deberá completarlas antes de preparar.</div>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <div className={`text-xs ${stats.valid ? "text-gray-600" : "text-red-700"}`}>
                      Asignado {stats.assigned} / {stats.total}
                      {stats.mismatch && stats.overNumber > 0 ? ` · Excede ${stats.over}` : ` · Pendiente ${stats.pending}`}
                    </div>
                    {!partidasOpen ? (
                      <button type="button" onClick={() => openPartidas(index)} className="rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-50" disabled={saving}>
                        Indicar partidas
                      </button>
                    ) : (
                      !partidasRequired && !stats.hasRows && (
                        <button type="button" onClick={() => closePartidas(index)} className="rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-50" disabled={saving}>
                          Ocultar partidas
                        </button>
                      )
                    )}
                  </div>
                </div>
                {partidasRequired && !stats.hasRows && (
                  <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                    Complete las partidas faltantes para poder marcar la orden como preparada.
                  </div>
                )}
                {partidasOpen && (
                  <>
                    {partidaQuantityMessage && (
                      <div className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                        {partidaQuantityMessage}
                      </div>
                    )}
                    <div className="mt-2 space-y-2">
                      {partidaRows.map((row, partidaIndex) => {
                        const rowHasData = hasPartidaData(row);
                        const scanWarning = partidaScanWarnings[partidaInputKey(index, partidaIndex)];
                        return (
                          <div key={partidaIndex} className="space-y-1">
                            <div className="grid grid-cols-1 gap-2 lg:grid-cols-[minmax(170px,1fr)_100px_140px_95px_42px]">
                              <input
                                ref={(node) => {
                                  const key = partidaInputKey(index, partidaIndex);
                                  if (node) partidaInputRefs.current[key] = node;
                                  else delete partidaInputRefs.current[key];
                                }}
                                value={row.partida}
                                onChange={(event) => changePartidaValue(index, partidaIndex, event.target.value)}
                                onKeyDown={(event) => handlePartidaScanKeyDown(event, index, partidaIndex, stockLots)}
                                onBlur={() => handlePartidaBlur(index, partidaIndex, stockLots)}
                                placeholder="Partida / serie"
                                className={`h-10 w-full rounded border bg-white px-3 py-2 ${
                                  scanWarning ? "border-amber-400 focus:outline-amber-500" : ""
                                }`}
                                disabled={saving}
                              />
                              <input
                                value={row.assignedQuantity}
                                onChange={(event) => updatePartida(index, partidaIndex, { assignedQuantity: event.target.value })}
                                onKeyDown={suppressEnterSubmit}
                                placeholder="Cantidad"
                                className={`h-10 rounded border bg-white px-3 py-2 ${
                                  stats.mismatch && rowHasData ? "border-red-400 text-red-900 focus:outline-red-500" : ""
                                }`}
                                disabled={saving}
                              />
                              <input
                                type="date"
                                value={row.partidaExpirationDate}
                                onChange={(event) => updatePartida(index, partidaIndex, { partidaExpirationDate: event.target.value })}
                                className="h-10 rounded border bg-white px-3 py-2"
                                disabled={saving}
                              />
                              <input
                                value={row.stockAvailableQuantity}
                                onChange={(event) => updatePartida(index, partidaIndex, { stockAvailableQuantity: event.target.value })}
                                onKeyDown={suppressEnterSubmit}
                                placeholder="Stock"
                                className="h-10 rounded border bg-white px-3 py-2"
                                disabled={saving}
                              />
                              <button
                                type="button"
                                onClick={() => removePartida(index, partidaIndex)}
                                disabled={saving || (item.partidas || []).length <= 1}
                                className="rounded border bg-white px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-40"
                                aria-label="Quitar partida"
                              >
                                X
                              </button>
                            </div>
                            {scanWarning && <div className="text-xs text-amber-800">{scanWarning}</div>}
                          </div>
                        );
                      })}
                    </div>
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                      <button type="button" onClick={() => addPartida(index)} className="rounded border bg-white px-3 py-1.5 text-sm hover:bg-gray-50" disabled={saving}>
                        Agregar partida
                      </button>
                      {item.articleCode && (
                        <button
                          type="button"
                          onClick={() => loadArticleStock(index, item.articleCode)}
                          className="rounded border border-sky-200 bg-white px-2 py-1 text-xs text-sky-800 hover:bg-sky-50"
                          disabled={saving || stockState?.loading}
                        >
                          {stockState?.loading ? "Consultando..." : "Actualizar partidas sugeridas"}
                        </button>
                      )}
                    </div>
                    {item.articleCode && (
                      <div className="mt-2 rounded border border-sky-100 bg-sky-50 px-3 py-2">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="text-xs font-medium uppercase text-sky-900">Partidas disponibles</div>
                            <div className="text-xs text-sky-700">Depósito {stockState?.depositCode || depositCode}</div>
                          </div>
                        </div>
                        {stockState?.loading && (
                          <div className="mt-2 text-xs text-sky-800">Buscando partidas con stock positivo en {stockState?.depositCode || depositCode}...</div>
                        )}
                        {!stockState?.loading && stockState?.warning && (
                          <div className="mt-2 text-xs text-amber-800">{stockState.warning}</div>
                        )}
                        {!stockState?.loading && visibleStockLots.length > 0 && (
                          <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                            {visibleStockLots.slice(0, 12).map((lot) => (
                              <button
                                key={`${lot.partida}-${lotExpiration(lot)}-${lotAvailableQuantity(lot)}`}
                                type="button"
                                onClick={() => chooseLot(index, suggestedPartidaIndex, lot)}
                                className="rounded border border-sky-200 bg-sky-50 px-3 py-2 text-left text-xs text-gray-700 hover:bg-white"
                                disabled={saving}
                              >
                                <span className="block font-mono text-sm font-semibold">{lot.partida || "-"}</span>
                                <span className="mt-0.5 block">{lotLabel(lot)}</span>
                              </button>
                            ))}
                          </div>
                        )}
                        {!stockState?.loading && stockLots.length > 0 && visibleStockLots.length === 0 && (
                          <div className="mt-2 text-xs text-sky-800">Sin coincidencias para el filtro ingresado.</div>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
              {item.articleName && (
                <div className="mt-2 text-xs text-gray-600">Artículo seleccionado: {item.articleName}</div>
              )}
            </div>
          );
        })}
      </div>

      <label className="block text-sm">
        <span className="mb-1 block text-xs uppercase text-gray-500">Detalle completo de la entrega</span>
        <textarea
          value={form.rawPedido}
          onChange={update("rawPedido")}
          rows={4}
          className="w-full rounded border px-3 py-2 disabled:bg-gray-100 disabled:text-gray-600"
          disabled={headerDisabled}
        />
      </label>

      <div className="flex justify-end gap-2">
        {onCancel && (
          <button type="button" className="rounded border px-4 py-2 text-sm hover:bg-gray-50" onClick={onCancel} disabled={saving}>
            Cancelar
          </button>
        )}
        <button type="submit" className="btn" disabled={saving || (!isPartidasMode && loadingCustomers) || hasPartidaQuantityMismatch}>
          {saving ? (isPartidasMode || isEdit ? "Guardando..." : "Creando...") : submitLabel || (isEdit ? "Guardar cambios" : "Crear entrega")}
        </button>
      </div>
    </form>
  );
}
