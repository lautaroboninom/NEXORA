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

export default function Tecnico() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const navigate = useNavigate();

  const sortPendientes = (arr) => {
    return [...arr].sort((a, b) => {
      const aDev = a?.derivado_devuelto ? 1 : 0;
      const bDev = b?.derivado_devuelto ? 1 : 0;
      if (aDev !== bDev) return bDev - aDev;

      const au = (a?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      const bu = (b?.motivo || "").toLowerCase() === "urgente control" ? 1 : 0;
      if (au !== bu) return bu - au;

      const rawA = resolveFechaCreacion(a);
      const rawB = resolveFechaCreacion(b);
      const dtA = rawA ? new Date(rawA).getTime() : Number.POSITIVE_INFINITY;
      const dtB = rawB ? new Date(rawB).getTime() : Number.POSITIVE_INFINITY;
      return dtA - dtB;
    });
  };

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get("/api/tecnico/mis-pendientes/");
      setRows(sortPendientes(Array.isArray(data) ? data : []));
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

  const displayRows = useMemo(() => {
    const needle = norm(q);
    if (!needle) return rows;
    return rows.filter((row) => {
      const campos = [
        formatOS(row),
        row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
        catalogEquipmentLabel(row),
        tipoEquipoOf(row),
        row?.numero_serie,
        row?.numero_interno,
        row?.estado,
        row?.presupuesto_estado,
      ];
      return campos.some((campo) => norm(campo).includes(needle));
    });
  }, [rows, q]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Mis pendientes</div>

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">{err}</div>
      )}

      <div className="flex justify-end mb-3">
        <input
          className="border rounded p-2 w-full max-w-md"
          placeholder="Filtrar por OS, cliente, equipo, serie, estado..."
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Filtrar mis pendientes"
        />
      </div>

      <WorkQueueTable
        rows={displayRows}
        loading={loading}
        emptyText={q ? "No se encontraron pendientes para el filtro aplicado." : "No tiene pendientes por ahora."}
        onOpen={go}
        showTechnician={false}
      />
    </div>
  );
}
