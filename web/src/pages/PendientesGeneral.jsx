import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../lib/api";
import WorkQueueTable from "../components/WorkQueueTable.jsx";
import useQueryState from "../hooks/useQueryState";
import {
  catalogEquipmentLabel,
  formatOS,
  ingresoIdOf,
  norm,
  resolveFechaCreacion,
  tipoEquipoOf,
} from "../lib/ui-helpers";

const ENDPOINT = "/api/ingresos/pendientes/";

export default function PendientesGeneral() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const navigate = useNavigate();

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get(ENDPOINT);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los pendientes");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filteredAndSorted = useMemo(() => {
    const needle = norm(q);
    const base = needle
      ? rows.filter((row) => {
          const campos = [
            formatOS(row),
            row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
            row?.marca ?? row?.equipo?.marca,
            catalogEquipmentLabel(row),
            tipoEquipoOf(row),
            row?.estado,
            row?.presupuesto_estado,
            row?.numero_serie,
            row?.numero_interno,
          ];
          return campos.some((c) => norm(c).includes(needle));
        })
      : rows;

    return [...base].sort((a, b) => {
      const ad = a?.derivado_devuelto ? 1 : 0;
      const bd = b?.derivado_devuelto ? 1 : 0;
      if (ad !== bd) return bd - ad;

      const au = (a?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      const bu = (b?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      if (au !== bu) return bu - au;

      const rawA = resolveFechaCreacion(a);
      const rawB = resolveFechaCreacion(b);
      const dtA = rawA ? new Date(rawA).getTime() : Number.POSITIVE_INFINITY;
      const dtB = rawB ? new Date(rawB).getTime() : Number.POSITIVE_INFINITY;
      return dtA - dtB;
    });
  }, [rows, q]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Pendientes general</div>

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">
          {err}
        </div>
      )}

      <div className="flex items-center gap-2 mb-3">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filtrar por OS, cliente, equipo, estado, serie..."
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar pendientes"
        />
        <button className="btn" onClick={load} title="Recargar lista" type="button">
          Recargar
        </button>
      </div>

      <WorkQueueTable
        rows={filteredAndSorted}
        loading={loading}
        emptyText="No hay pendientes que coincidan con el filtro."
        onOpen={go}
      />
    </div>
  );
}
