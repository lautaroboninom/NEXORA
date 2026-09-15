// web/src/pages/DerivarIngreso.jsx
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { localDateInputValue } from "../lib/ui-helpers";
import {
  getProveedoresExternos,
  postDerivarIngreso,
  fetchBlobAuth,
  getDerivacionesPorIngreso,
} from "../lib/api";

export default function DerivarIngreso() {
  const { id } = useParams(); // :id del ingreso
  const nav = useNavigate();
  const [loading, setLoading] = useState(false);
  const [proveedores, setProveedores] = useState([]);
  const [form, setForm] = useState({
    proveedor_id: "",
    fecha_deriv: localDateInputValue(),
    comentarios: "",
  });
  const [error, setError] = useState("");
  const busyRef = useRef(false); // barrera sincrónica contra doble submit

  useEffect(() => {
    (async () => {
      try {
        const rows = await getProveedoresExternos();
        setProveedores(rows || []);
      } catch (e) {
        setError(String(e));
      }
    })();
  }, []);

  const onChange = (e) => {
    const { name, value } = e.target;
    setForm((f) => ({ ...f, [name]: value }));
  };

  const onSubmit = async (e) => {
    e.preventDefault();
    if (loading || busyRef.current) return;
    busyRef.current = true;
    setError("");
    if (!form.proveedor_id) {
      setError("Seleccioná un proveedor.");
      busyRef.current = false;
      return;
    }

    // Abrir ventana anticipada para evitar bloqueos de pop-ups tras await
    let win = null;
    try { win = window.open('', '_blank'); } catch (_) {}
    setLoading(true);
    try {
      const res = await postDerivarIngreso(id, {
        proveedor_id: Number(form.proveedor_id),
        fecha_deriv: form.fecha_deriv || null,
        comentarios: form.comentarios || null,
      });
      // Intentar abrir el remito de derivación inmediatamente.
      try {
        const derivId = res?.deriv_id;
        if (derivId) {
          const { blob } = await fetchBlobAuth(`/api/ingresos/${id}/derivaciones/${derivId}/remito/`);
          const url = URL.createObjectURL(blob);
          if (win) { try { win.location = url; } catch { window.open(url, "_blank"); } } else { window.open(url, "_blank"); }
          setTimeout(() => URL.revokeObjectURL(url), 20000);
        }
      } catch (_) {
        // Fallback: abrir la derivación activa si existe.
        try {
          const list = await getDerivacionesPorIngreso(id);
          const abierta = Array.isArray(list)
            ? list.find((d) => !d.fecha_entrega && ["derivado", "en_servicio"].includes(String(d?.estado || "").toLowerCase()))
            : null;
          if (abierta) {
            const { blob } = await fetchBlobAuth(`/api/ingresos/${id}/derivaciones/${abierta.id}/remito/`);
            const url = URL.createObjectURL(blob);
            if (win) { try { win.location = url; } catch { window.open(url, "_blank"); } } else { window.open(url, "_blank"); }
            setTimeout(() => URL.revokeObjectURL(url), 20000);
          } else if (win) {
            try { win.close(); } catch {}
          }
        } catch { /* noop */ }
      }
      // Volver a la hoja de servicio y resaltar Derivaciones
      nav(`/ingresos/${id}`, { state: { tab: "derivaciones" } });
    } catch (e) {
      setError(e?.data?.detail || e?.message || "No se pudo registrar la derivación.");
      // Si el POST falló por una derivación activa, intentar abrir el PDF correspondiente.
      try {
        const list = await getDerivacionesPorIngreso(id);
        const abierta = Array.isArray(list)
          ? list.find((d) => !d.fecha_entrega && ["derivado", "en_servicio"].includes(String(d?.estado || "").toLowerCase()))
          : null;
        if (abierta) {
          const { blob } = await fetchBlobAuth(`/api/ingresos/${id}/derivaciones/${abierta.id}/remito/`);
          const url = URL.createObjectURL(blob);
          if (win) { try { win.location = url; } catch { window.open(url, "_blank"); } } else { window.open(url, "_blank"); }
          setTimeout(() => URL.revokeObjectURL(url), 20000);
        } else if (win) { try { win.close(); } catch {} }
      } catch (_) { try { if (win) win.close(); } catch {} }
    } finally {
      setLoading(false);
      busyRef.current = false;
    }
  };

  return (
    <div className="max-w-xl">
      <button
        type="button"
        onClick={() => nav(`/ingresos/${id}`, { state: { tab: "derivaciones" } })}
        className="mb-3 inline-flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800"
      >
        Volver
      </button>
      <h1 className="text-xl font-semibold mb-4">Derivar ingreso #{id}</h1>

      {error && <div className="p-3 mb-3 bg-red-100 text-red-700 rounded">{error}</div>}

      <form onSubmit={onSubmit} className="space-y-4">
        <div>
          <label className="block text-sm mb-1">Proveedor</label>
          <select
            name="proveedor_id"
            value={form.proveedor_id}
            onChange={onChange}
            className="input w-full"
          >
            <option value="">-- seleccionar --</option>
            {proveedores.map((p) => (
              <option key={p.id} value={p.id}>{p.nombre}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm mb-1">Fecha derivación</label>
          <input
            type="date"
            name="fecha_deriv"
            value={form.fecha_deriv}
            onChange={onChange}
            className="input"
          />
        </div>

        <div>
          <label className="block text-sm mb-1">Comentarios</label>
          <textarea
            name="comentarios"
            value={form.comentarios}
            onChange={onChange}
            className="input w-full"
            rows={3}
          />
        </div>

        <div className="flex gap-2">
          <button className="btn" disabled={loading}>
            {loading ? "Derivando..." : "Derivar"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => nav(`/ingresos/${id}`, { state: { tab: "derivaciones" } })}
          >
            Cancelar
          </button>
        </div>
      </form>
    </div>
  );
}

