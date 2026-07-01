// web/src/pages/Reparados.jsx
import { useEffect, useMemo, useState } from "react";
import api from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, resolveFechaIngreso, catalogEquipmentLabel } from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import StatusChip from "../components/StatusChip.jsx";
import useQueryState from "../hooks/useQueryState";
import { DesktopTableWrap, MobileDataCard, MobileDataField, MobileDataList } from "../components/Responsive.jsx";

// Ajustar si el backend usa otra ruta.


const ENDPOINT = "/api/ingresos/aprobados-reparados/";


export default function Reparados() {
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
      setErr(e?.message || "No se pudieron cargar los reparados");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    const needle = norm(q);
    if (!needle) return rows;
    return rows.filter((row) => {
      const campos = [
        formatOS(row),
        row?.razon_social ?? row?.cliente ?? row?.cliente_nombre,
        row?.marca ?? row?.equipo?.marca,
        catalogEquipmentLabel(row),
        tipoEquipoOf(row),
        row?.estado,
        row?.numero_serie,
        row?.numero_interno,
      ];
      return campos.some((c) => norm(c).includes(needle));
    });
  }, [rows, q]);

  const go = (row) => {
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  const onRowKeyDown = (e, row) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      go(row);
    }
  };

  return (
    <div className="card">
      <div className="h1 mb-3">Reparados</div>

      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">
          {err}
        </div>
      )}

      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filtrar por OS, cliente, marca, equipo, serie"
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar reparados"
        />
        <button
          className="btn"
          onClick={load}
          title="Recargar lista"
          disabled={loading}
          aria-busy={loading ? "true" : "false"}
        >
          Recargar
        </button>
      </div>

      {loading ? (
        "Cargando..."
      ) : filtered.length === 0 ? (
        <div className="text-sm text-gray-500">No hay reparados que coincidan con el filtro.</div>
      ) : (
        <div>
          <MobileDataList>
            {filtered.map((row) => (
              <MobileDataCard
                key={ingresoIdOf(row)}
                as="button"
                type="button"
                onClick={() => go(row)}
                className="hover:bg-gray-50"
                aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                data-testid={`row-mobile-${ingresoIdOf(row)}`}
              >
                <div className="font-semibold text-gray-900 underline">{formatOS(row)}</div>
                <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                  <MobileDataField label="Cliente" value={row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"} />
                  <MobileDataField label="Estado">
                    <StatusChip value={row?.estado} />
                  </MobileDataField>
                  <MobileDataField label="Equipo" value={catalogEquipmentLabel(row) ?? "-"} />
                  <MobileDataField label="Serie">
                    <DeviceIdentifier row={row} />
                  </MobileDataField>
                  <MobileDataField label="Fecha ingreso" value={formatDateOnly(resolveFechaIngreso(row))} />
                  <MobileDataField label="Fecha reparación">
                    {formatDateOnly(
                      row?.fecha_reparado ??
                        row?.fecha_reparacion ??
                        row?.reparado_fecha ??
                        row?.estado_fecha
                    )}
                  </MobileDataField>
                </div>
              </MobileDataCard>
            ))}
          </MobileDataList>
          <DesktopTableWrap>
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th scope="col" className="p-2">OS</th>
                <th scope="col" className="p-2">Estado</th>
                <th scope="col" className="p-2">Cliente</th>
                <th scope="col" className="p-2">Equipo</th>
                <th scope="col" className="p-2">Serie</th>
                <th scope="col" className="p-2">Fecha ingreso</th>
                <th scope="col" className="p-2">Fecha reparación</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr
                  key={ingresoIdOf(row)}
                  onClick={() => go(row)}
                  onKeyDown={(e) => onRowKeyDown(e, row)}
                  className="hover:bg-gray-50 cursor-pointer"
                  role="link"
                  tabIndex={0}
                  aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                  data-testid={`row-${ingresoIdOf(row)}`}
                >
                  <td className="p-2 underline">{formatOS(row)}</td>
                  <td className="p-2"><StatusChip value={row?.estado} /></td>
                  <td className="p-2">{row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}</td>
                  <td className="p-2">{catalogEquipmentLabel(row) ?? "-"}</td>
                  
                  <td className="p-2"><DeviceIdentifier row={row} /></td>
                  <td className="p-2 whitespace-nowrap">{formatDateOnly(resolveFechaIngreso(row))}</td>
                  <td className="p-2 whitespace-nowrap">
                    {formatDateOnly(
                      row?.fecha_reparado ??
                        row?.fecha_reparacion ??
                        row?.reparado_fecha ??
                        row?.estado_fecha
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </DesktopTableWrap>
          <div className="text-xs text-gray-500 mt-2">
            Mostrando {filtered.length} de {rows.length}.
          </div>
        </div>
      )}
    </div>
  );
}

