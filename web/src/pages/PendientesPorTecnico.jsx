import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getTecnicos } from "../lib/api";
import api from "../lib/api";
import WorkQueueTable from "../components/WorkQueueTable.jsx";
import useQueryState from "../hooks/useQueryState";
import {
  catalogEquipmentLabel,
  formatOS,
  ingresoIdOf,
  norm,
  tipoEquipoOf,
} from "../lib/ui-helpers";

const UNASSIGNED_TECHNICIAN_FILTER = "sin_asignar";

export default function PendientesPorTecnico() {
  const [tecnicos, setTecnicos] = useState([]);
  const [tecnicoId, setTecnicoId] = useQueryState("tecnico_id", "");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const navigate = useNavigate();

  useEffect(() => {
    getTecnicos().then(setTecnicos).catch((e) => setErr(e.message));
  }, []);

  async function load() {
    if (!tecnicoId) {
      setRows([]);
      return;
    }
    setLoading(true);
    setErr("");
    try {
      const data = await api.get(`/api/ingresos/pendientes/?tecnico_id=${encodeURIComponent(tecnicoId)}`);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e?.message || "Error cargando pendientes");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tecnicoId]);

  const filtered = useMemo(() => {
    const needle = norm(q);
    if (!needle) return rows;
    return rows.filter((row) => {
      const campos = [
        formatOS(row),
        row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
        row?.marca,
        row?.modelo,
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
      <div className="h1 mb-3">Pendientes por técnico</div>
      {err && <div className="bg-red-100 text-red-700 p-2 rounded mb-3">{err}</div>}

      <div className="flex flex-col md:flex-row gap-2 mb-3 md:items-center">
        <select
          className="border rounded p-2"
          value={tecnicoId}
          onChange={(e) => setTecnicoId(e.target.value)}
          aria-label="Seleccionar técnico"
        >
          <option value="">-- Seleccionar técnico --</option>
          <option value={UNASSIGNED_TECHNICIAN_FILTER}>-- Sin técnico asignado --</option>
          {tecnicos.map((t) => (
            <option key={t.id} value={t.id}>{t.nombre}</option>
          ))}
        </select>
        <input
          className="border rounded p-2 w-full max-w-md"
          placeholder="Filtrar por OS, cliente, marca, equipo, serie"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Filtrar pendientes por técnico"
        />
      </div>

      {!tecnicoId ? (
        <div className="text-sm text-gray-500">Elegí un técnico o la opción sin asignar para ver los pendientes.</div>
      ) : (
        <WorkQueueTable
          rows={filtered}
          loading={loading}
          emptyText="Sin pendientes."
          onOpen={go}
          showTechnician={tecnicoId === UNASSIGNED_TECHNICIAN_FILTER}
        />
      )}
    </div>
  );
}
