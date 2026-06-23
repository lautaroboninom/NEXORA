import { useEffect, useState } from "react";
import { postBejermanSellerCode } from "../lib/api";

function normalizeSellerCode(value) {
  return String(value ?? "").trim().toUpperCase().slice(0, 4);
}

function errorMessage(error) {
  const detail = error?.data?.detail || error?.message;
  return detail || "No se pudo guardar el código de vendedor.";
}

export default function BejermanSellerCodeModal({ open, initialCode = "", onSaved }) {
  const [code, setCode] = useState(() => normalizeSellerCode(initialCode));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setCode(normalizeSellerCode(initialCode));
    setError("");
    setSaving(false);
  }, [open, initialCode]);

  if (!open) return null;

  const save = async (sellerCode) => {
    setSaving(true);
    setError("");
    try {
      await postBejermanSellerCode({ sellerCode: normalizeSellerCode(sellerCode) });
      if (onSaved) await onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const submit = (event) => {
    event.preventDefault();
    void save(code);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <form onSubmit={submit} className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold text-gray-900">Código de vendedor Bejerman</h2>
          <p className="text-sm text-gray-600">
            Si tiene un código de vendedor propio en Bejerman, ingréselo para precargarlo en las nuevas órdenes de entrega.
          </p>
        </div>

        {error && (
          <div className="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </div>
        )}

        <label className="mt-4 block text-sm">
          <span className="mb-1 block text-xs uppercase text-gray-500">Código</span>
          <input
            value={code}
            onChange={(event) => setCode(normalizeSellerCode(event.target.value))}
            className="h-10 w-full rounded border px-3 py-2 uppercase disabled:bg-gray-100 disabled:text-gray-600"
            maxLength={4}
            autoFocus
            disabled={saving}
          />
        </label>

        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={() => void save("")}
            className="rounded border px-4 py-2 text-sm hover:bg-gray-50 disabled:opacity-60"
            disabled={saving}
          >
            No tengo código
          </button>
          <button
            type="submit"
            className="rounded bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-60"
            disabled={saving}
          >
            Guardar
          </button>
        </div>
      </form>
    </div>
  );
}
