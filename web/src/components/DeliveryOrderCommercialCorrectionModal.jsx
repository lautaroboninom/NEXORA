import { useEffect, useState } from "react";
import { patchDeliveryOrderCommercialCorrection } from "../lib/api";
import {
  deliveryOrderItemPriceCurrency,
  deliveryOrderItemsTotals,
  deliveryOrderPriceCurrency,
  formatOrderTotalsAmount,
} from "../lib/delivery-orders";
import { ResponsiveModalOverlay, ResponsiveModalPanel } from "./Responsive.jsx";

function DetailField({ label, value, children, className = "" }) {
  return (
    <div className={className}>
      <div className="text-[11px] font-semibold uppercase text-gray-500">{label}</div>
      <div className="mt-0.5 break-words text-sm text-gray-900">{children ?? value ?? "-"}</div>
    </div>
  );
}

function formatQuantity(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("es-AR", { maximumFractionDigits: 2 });
}

function formatMoney(value, currency = "ARS") {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const normalizedCurrency = String(currency || "").toUpperCase() === "USD" ? "USD" : "ARS";
  return number.toLocaleString("es-AR", {
    style: "currency",
    currency: normalizedCurrency,
    maximumFractionDigits: 2,
  });
}

function blockedReasonForOrder(order, items) {
  if (order?.invoiceNumber) return "La orden ya fue facturada.";
  if (!order?.remitoNumber) return "La orden no tiene remito.";
  if (!order?.status || ["entregado_no_facturable", "facturado", "cancelado"].includes(order.status)) {
    return "La orden no está pendiente de facturación.";
  }
  if (!items.length) return "La orden no tiene renglones para corregir.";
  return "";
}

export default function DeliveryOrderCommercialCorrectionModal({ open, order, onClose, onSaved }) {
  const [deadline, setDeadline] = useState("");
  const [rows, setRows] = useState([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || !order) return;
    const defaultCurrency = deliveryOrderPriceCurrency(order);
    setDeadline(String(order.commercialDeadline || ""));
    setRows(
      (Array.isArray(order.items) ? order.items : []).map((item) => ({
        id: item.id,
        articleCode: item.articleCode || "",
        articleName: item.articleName || item.description || "",
        quantity: item.quantity,
        unitPrice: item.unitPrice ?? "",
        priceCurrency: deliveryOrderItemPriceCurrency(item, defaultCurrency),
        discountPercent: item.discountPercent ?? "0",
      }))
    );
    setError("");
    setSaving(false);
  }, [open, order]);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose, saving]);

  if (!open || !order) return null;

  const orderItems = Array.isArray(order.items) ? order.items : [];
  const itemsById = new Map(orderItems.map((item) => [item.id, item]));
  const nextOrder = {
    ...order,
    commercialDeadline: deadline,
    items: rows.map((row) => ({
      ...(itemsById.get(row.id) || {}),
      unitPrice: row.unitPrice,
      priceCurrency: row.priceCurrency,
      discountPercent: row.discountPercent,
    })),
  };
  const beforeTotals = deliveryOrderItemsTotals(order);
  const afterTotals = deliveryOrderItemsTotals(nextOrder);
  const blockedReason = blockedReasonForOrder(order, orderItems);

  const updateRow = (rowId, field) => (event) => {
    const value = event.target.value;
    setRows((current) => current.map((row) => (row.id === rowId ? { ...row, [field]: value } : row)));
  };

  const rowSubtotal = (row) => {
    const quantity = Number(row.quantity || 0);
    const unitPrice = Number(String(row.unitPrice || "").replace(",", "."));
    const discount = Number(String(row.discountPercent || "0").replace(",", "."));
    if (!Number.isFinite(quantity) || !Number.isFinite(unitPrice)) return "-";
    const boundedDiscount = Number.isFinite(discount) ? Math.min(100, Math.max(0, discount)) : 0;
    return formatMoney(quantity * unitPrice * (1 - boundedDiscount / 100), row.priceCurrency);
  };

  const submit = async (event) => {
    event.preventDefault();
    if (blockedReason || saving) return;
    setSaving(true);
    setError("");
    try {
      const saved = await patchDeliveryOrderCommercialCorrection(order.id, {
        commercialDeadline: deadline,
        items: rows.map((row) => ({
          id: row.id,
          unitPrice: row.unitPrice,
          priceCurrency: row.priceCurrency,
          discountPercent: row.discountPercent,
        })),
      });
      onSaved(saved);
    } catch (err) {
      setError(err?.message || "No se pudo guardar la corrección comercial.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <ResponsiveModalOverlay role="dialog" aria-modal="true" onClick={() => !saving && onClose()}>
      <ResponsiveModalPanel
        className="max-w-5xl overflow-hidden"
        onClick={(event) => event.stopPropagation()}
      >
        <form onSubmit={submit}>
          <div className="flex items-center justify-between gap-3 border-b px-4 py-3 sm:px-5 sm:py-4">
            <div>
              <h2 className="text-lg font-semibold text-gray-900">Corregir precio/plazo</h2>
              <p className="text-sm text-gray-600">
                {order.orderNumber || "Orden de entrega"} - {order.customerName || "-"}
              </p>
            </div>
            <button
              type="button"
              className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-50"
              onClick={onClose}
              disabled={saving}
            >
              Cerrar
            </button>
          </div>

          <div className="max-h-[calc(100dvh-8rem)] overflow-auto p-3 sm:p-5">
            {blockedReason && (
              <div className="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                {blockedReason}
              </div>
            )}
            {error && (
              <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {error}
              </div>
            )}
            <div className="mb-3 rounded border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-900">
              Esta corrección actualiza únicamente NEXORA y notifica a Cobranzas. El RT emitido en Bejerman no se
              modifica.
            </div>

            <div className="grid grid-cols-1 gap-3 rounded border bg-gray-50 p-3 sm:grid-cols-3">
              <DetailField label="Remito" value={order.remitoNumber || "-"} />
              <DetailField label="Total anterior" value={formatOrderTotalsAmount(beforeTotals)} />
              <DetailField label="Total nuevo" value={formatOrderTotalsAmount(afterTotals)} />
              <label className="text-sm sm:col-span-3">
                <span className="mb-1 block text-[11px] font-semibold uppercase text-gray-500">Plazo comercial</span>
                <input
                  value={deadline}
                  onChange={(event) => setDeadline(event.target.value)}
                  className="h-9 w-full rounded border px-2"
                  maxLength={120}
                  disabled={saving || Boolean(blockedReason)}
                />
              </label>
            </div>

            <div className="mt-4 overflow-hidden rounded border">
              <table className="min-w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-3 py-2 font-medium">Artículo</th>
                    <th className="px-3 py-2 font-medium">Cantidad</th>
                    <th className="px-3 py-2 font-medium">Precio</th>
                    <th className="px-3 py-2 font-medium">Moneda</th>
                    <th className="px-3 py-2 font-medium">Descuento</th>
                    <th className="px-3 py-2 text-right font-medium">Subtotal nuevo</th>
                  </tr>
                </thead>
                <tbody className="divide-y bg-white">
                  {rows.map((row) => (
                    <tr key={row.id} className="align-top">
                      <td className="px-3 py-2">
                        <div className="font-medium text-gray-900">{row.articleName || "-"}</div>
                        {row.articleCode && <div className="mt-0.5 font-mono text-xs text-sky-700">{row.articleCode}</div>}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">{formatQuantity(row.quantity)}</td>
                      <td className="px-3 py-2">
                        <input
                          value={row.unitPrice}
                          onChange={updateRow(row.id, "unitPrice")}
                          inputMode="decimal"
                          className="h-9 w-32 rounded border px-2"
                          disabled={saving || Boolean(blockedReason)}
                        />
                      </td>
                      <td className="px-3 py-2">
                        <select
                          value={row.priceCurrency}
                          onChange={updateRow(row.id, "priceCurrency")}
                          className="h-9 w-24 rounded border px-2"
                          disabled
                        >
                          <option value="ARS">ARS</option>
                          <option value="USD">USD</option>
                        </select>
                      </td>
                      <td className="px-3 py-2">
                        <input
                          value={row.discountPercent}
                          onChange={updateRow(row.id, "discountPercent")}
                          inputMode="decimal"
                          className="h-9 w-24 rounded border px-2"
                          disabled={saving || Boolean(blockedReason)}
                        />
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-right font-medium text-gray-900">
                        {rowSubtotal(row)}
                      </td>
                    </tr>
                  ))}
                  {!rows.length && (
                    <tr>
                      <td colSpan={6} className="px-3 py-8 text-center text-gray-500">
                        Sin renglones.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="flex justify-end gap-2 border-t px-4 py-3 sm:px-5">
            <button type="button" onClick={onClose} disabled={saving} className="rounded border px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-50">
              Cancelar
            </button>
            <button
              type="submit"
              disabled={saving || Boolean(blockedReason)}
              className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
            >
              {saving ? "Guardando..." : "Guardar corrección"}
            </button>
          </div>
        </form>
      </ResponsiveModalPanel>
    </ResponsiveModalOverlay>
  );
}
