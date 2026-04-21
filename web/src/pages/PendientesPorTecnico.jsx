import { useEffect, useMemo, useState } from "react";
import { getTecnicos } from "../lib/api";
import api from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, resolveFechaIngreso, catalogEquipmentLabel, nsPreferInternoOf, isMotivoCotizacionEquipo } from "../lib/ui-helpers";
import StatusChip from "../components/StatusChip.jsx";
import useQueryState from "../hooks/useQueryState";

const UNASSIGNED_TECHNICIAN_FILTER = "sin_asignar";


export default function PendientesPorTecnico() {
    
  const [tecnicos, setTecnicos] = useState([]);
  const [tecnicoId, setTecnicoId] = useQueryState("tecnico_id", "");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const nav = useNavigate();

  useEffect(() => { getTecnicos().then(setTecnicos).catch(e=>setErr(e.message)); }, []);

  async function load() {
    if (!tecnicoId) {
      setRows([]);
      return;
    }
    setLoading(true); setErr("");
    try {
      const data = await api.get(`/api/ingresos/pendientes/?tecnico_id=${encodeURIComponent(tecnicoId)}`);
      console.log("Pendientes recibidos:", data?.length, data?.[0]);
      setRows(Array.isArray(data) ? data : []);
    } catch(e) {
      setErr(e?.message || "Error cargando pendientes");
    } finally { setLoading(false); }
  }

  useEffect(()=>{ load(); }, [tecnicoId]);

  const filtered = useMemo(()=> {
    const needle = norm(q);
    if (!needle) return rows;
    return rows.filter(r=>{
      const campos = [formatOS(r), r?.razon_social, r?.marca, r?.modelo, tipoEquipoOf(r), r?.numero_serie, r?.numero_interno, r?.estado];
      return campos.some(c => norm(c).includes(needle));
    });
  }, [rows, q]);

  return (
    <div className="card">
      <div className="h1 mb-3">Pendientes por técnico</div>
      {err && <div className="bg-red-100 text-red-700 p-2 rounded mb-3">{err}</div>}
      <div className="flex gap-2 mb-3 items-center">
        <select className="border rounded p-2" value={tecnicoId} onChange={e=>setTecnicoId(e.target.value)}>
          <option value="">-- Seleccionar técnico --</option>
          <option value={UNASSIGNED_TECHNICIAN_FILTER}>-- Sin técnico asignado --</option>
          {tecnicos.map(t=> <option key={t.id} value={t.id}>{t.nombre}</option>)}
        </select>
        <input className="border rounded p-2 w-full max-w-md" placeholder="Filtrar por OS, cliente, marca, equipo, serie" value={q} onChange={e=>setQ(e.target.value)} />
      </div>

      {!tecnicoId ? <div className="text-sm text-gray-500">Elegí un técnico o la opción sin asignar para ver los pendientes.</div> :
      loading ? "Cargando..." :
      filtered.length === 0 ? <div className="text-sm text-gray-500">Sin pendientes.</div> : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead><tr className="text-left">
              <th className="p-2">OS</th><th className="p-2">Cliente</th>
              <th className="p-2">Equipo</th><th className="p-2">Estado</th><th className="p-2">Serie</th><th className="p-2">Fecha</th>
            </tr></thead>
            <tbody>
              {filtered.map(row=>(
                <tr key={ingresoIdOf(row)} className="hover:bg-gray-50 cursor-pointer" onClick={()=>nav(`/ingresos/${ingresoIdOf(row)}`)}>
                  <td className="p-2 underline">
                    {formatOS(row)}
                    {isMotivoCotizacionEquipo(row?.motivo) && (
                      <span className="ml-2 inline-block px-2 py-0.5 text-[10px] rounded bg-amber-100 text-amber-800 align-middle">
                        COTIZACIÓN EQUIPO
                      </span>
                    )}
                  </td>
                  <td className="p-2">{row.razon_social}</td>
                  <td className="p-2">{catalogEquipmentLabel(row)}</td>
                  <td className="p-2"><StatusChip value={row.estado} /></td>
                  <td className="p-2">{nsPreferInternoOf(row)}</td>
                  <td className="p-2 whitespace-nowrap">{formatDateOnly(resolveFechaIngreso(row))}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="text-xs text-gray-500 mt-2">Mostrando {filtered.length} de {rows.length}.</div>
        </div>
      )}
    </div>
  );
}



