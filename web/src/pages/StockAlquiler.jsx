//web\src\pages\StockAlquiler.jsx

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getGeneralEquipos } from "../lib/api";
import { ingresoIdOf, formatOS, norm, tipoEquipoOf, catalogEquipmentLabel } from "../lib/ui-helpers";
import { useAuth } from "../context/AuthContext";
import useQueryState from "../hooks/useQueryState";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";

// Catlogo (DB):
const TARGET_ID = 5;
const TARGET_NAME = "Estantería de Alquiler";
const EXCLUIR_ESTADOS_QUERY = "entregado,alquilado,baja,vendido_pendiente_entrega,vendido_entregado";
const ESTADOS_EXCLUIR = new Set(["entregado", "alquilado", "baja", "vendido_pendiente_entrega", "vendido_entregado"]);
const isStockAlquiler = (r) => {
  const id = Number(r?.ubicacion_id ?? NaN);
  const name = r?.ubicacion_nombre;
  return id === TARGET_ID || norm(name) === norm(TARGET_NAME);
};

const estadoValido = (r) => {
  const estado = (r?.estado || '').toString().trim().toLowerCase();
  return !ESTADOS_EXCLUIR.has(estado);
};

export default function StockAlquiler() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const nav = useNavigate();

  const { user, loading: authLoading } = useAuth();

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      setLoading(false);
      return;
    }

    let active = true;
    (async () => {
      setErr("");
      setLoading(true);
      try {
        let data = await getGeneralEquipos({ ubicacion_id: TARGET_ID, solo_taller: false, excluir_estados: EXCLUIR_ESTADOS_QUERY });
        if (!Array.isArray(data) || data.length === 0) {
          data = await getGeneralEquipos({ solo_taller: false, excluir_estados: EXCLUIR_ESTADOS_QUERY });
        }
        if (!active) return;
        const safe = Array.isArray(data) ? data : [];
        setRows(safe.filter((row) => isStockAlquiler(row) && estadoValido(row)));
      } catch (e) {
        if (!active) return;
        setErr(e?.message || "Error cargando stock");
      } finally {
        if (active) setLoading(false);
      }
    })();

    return () => {
      active = false;
    };
  }, [authLoading, user]);

  const filtered = useMemo(() => {
    const needle = norm(q);
    if (!needle) return rows;
    return rows.filter(r => {
      if (!estadoValido(r)) return false;
      const campos = [formatOS(r), r?.marca, catalogEquipmentLabel(r), tipoEquipoOf(r), r?.numero_serie, r?.numero_interno, r?.razon_social];
      return campos.some(c => norm(c).includes(needle));
    });
  }, [rows, q]);

  const marcaOf = (row) => (row?.marca ?? row?.equipo?.marca ?? "-");
  const modeloOf = (row) => {
    const candidates = [row?.modelo, row?.equipo?.modelo, row?.modelo_nombre, row?.equipo?.modelo_nombre, row?.modelo_serie, row?.serie_nombre];
    for (const raw of candidates) {
      if (typeof raw === "string") {
        const v = raw.trim();
        if (v) return v;
      }
    }
    return "-";
  };
  const varianteOf = (row) => {
    const candidates = [row?.equipo_variante, row?.modelo_variante, row?.variante, row?.variante_nombre];
    for (const raw of candidates) {
      if (typeof raw === "string") {
        const v = raw.trim();
        if (v) return v;
      }
    }
    return "-";
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Stock de Alquiler</div>
      {err && <div className="bg-red-100 text-red-700 p-2 rounded mb-3">{err}</div>}

      <div className="flex items-center gap-2 mb-3">
        <input
          className="border rounded p-2 w-full max-w-md"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filtrar por OS, marca, equipo, serie"
        />
      </div>

      {loading ? "Cargando..." :
        filtered.length === 0 ? (
          <div className="text-sm text-gray-500">No hay equipos en Estanteria de Alquiler.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="text-left">
                  <th className="p-2">OS</th>
                  <th className="p-2">Tipo de equipo</th>
                  <th className="p-2">Marca</th>
                  <th className="p-2">Modelo</th>
                  <th className="p-2">Variante</th>
                  <th className="p-2">Serie</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr
                    key={ingresoIdOf(row)}
                    className="border-t hover:bg-gray-50 cursor-pointer"
                    onClick={() => nav(`/ingresos/${ingresoIdOf(row)}`)}
                  >
                    <td className="p-2 underline">{formatOS(row)}</td>
                    <td className="p-2">{tipoEquipoOf(row)}</td>
                    <td className="p-2">{marcaOf(row)}</td>
                    <td className="p-2">{modeloOf(row)}</td>
                    <td className="p-2">{varianteOf(row)}</td>
                    <td className="p-2"><DeviceIdentifier row={row} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="text-xs text-gray-500 mt-2">Mostrando {filtered.length} equipos.</div>
          </div>
        )}
    </div>
  );
}


