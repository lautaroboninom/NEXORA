// web/src/pages/AdminListos.jsx
import { useEffect, useMemo, useState } from "react";
import api, { getBlob } from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, catalogEquipmentLabel } from "../lib/ui-helpers";
import StatusChip from "../components/StatusChip.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { resolutionLabel } from "../lib/constants";
import useQueryState from "../hooks/useQueryState";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { DesktopTableWrap, MobileDataCard, MobileDataField, MobileDataList } from "../components/Responsive.jsx";


// Ajustar si el backend usa otra ruta.
const ENDPOINT = "/api/listos-para-retiro/";


export default function AdminListos() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [busyId, setBusyId] = useState(null);
  const [remitoBusyId, setRemitoBusyId] = useState(null);
  const navigate = useNavigate();
  const { user } = useAuth();
  const release = can(user, PERMISSION_CODES.ACTION_INGRESO_PRINT_EXIT_ORDER);
  const canDeliver = can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY);

  async function load() {
    try {
      setErr("");
      setLoading(true);
      const data = await api.get(ENDPOINT);
      const list = Array.isArray(data) ? data : [];
      const clientNameOf = (row) => (row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "").trim();
      const readyTime = (row) =>
        new Date(
          row?.fecha_entrega ?? row?.fecha_listo ?? row?.fecha_reparado ?? row?.fecha_estado ?? row?.estado_fecha ?? 0,
        ).getTime();
      list.sort((a, b) => {
        const nameComparison = clientNameOf(a).localeCompare(clientNameOf(b), undefined, { sensitivity: "base" });
        if (nameComparison !== 0) return nameComparison;
        return readyTime(b) - readyTime(a);
      });
      setRows(list);
    } catch (e) {
      setErr(e?.message || "No se pudieron cargar los equipos listos para retiro");
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
        row?.resolucion,
        resolutionLabel(row?.resolucion ?? ""),
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

  async function entregar(row) {
    const id = ingresoIdOf(row);
    if (!id) return;
    if (String(row?.estado || "").toLowerCase() === "vendido_pendiente_entrega") {
      navigate(`/ingresos/${id}`);
      return;
    }
    try {
      setBusyId(id);
      await api.post(`/api/ingresos/${id}/entregar/`);
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo marcar como entregado");
    } finally {
      setBusyId(null);
    }
  }

  async function verOrdenSalida(row) {
    const id = ingresoIdOf(row);
    if (!id) return;
    try {
      setRemitoBusyId(id);
      const blob = await getBlob(`/api/ingresos/${id}/remito/`);
      if (!(blob instanceof Blob)) {
        throw new Error("La respuesta no fue un PDF");
      }
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      await load();
    } catch (e) {
      setErr(e?.message || "No se pudo obtener la orden de salida");
    } finally {
      setRemitoBusyId(null);
    }
  }

  return (
    <div className="card">
      <div className="h1 mb-3">Listos para retiro</div>

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
          placeholder="Filtrar por OS, cliente, equipo, serie, resolución"
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar listos para retiro"
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
        <div className="text-sm text-gray-500">No hay equipos listos para retiro.</div>
      ) : (
        <div>
          <MobileDataList>
            {filtered.map((row) => {
              const rowId = ingresoIdOf(row);
              const isSalePending = String(row?.estado || "").toLowerCase() === "vendido_pendiente_entrega";
              return (
                <MobileDataCard
                  key={rowId}
                  onClick={() => go(row)}
                  onKeyDown={(e) => onRowKeyDown(e, row)}
                  className="cursor-pointer hover:bg-gray-50"
                  role="link"
                  tabIndex={0}
                  aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                  data-testid={`row-mobile-${rowId}`}
                >
                  <div className="font-semibold text-gray-900 underline">{formatOS(row)}</div>
                  <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                    <MobileDataField label="Cliente" value={row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"} />
                    <MobileDataField label="Equipo" value={catalogEquipmentLabel(row) ?? "-"} />
                    <MobileDataField label="Resolución">
                      <StatusChip value={resolutionLabel(row?.resolucion)} title="Resolución" />
                    </MobileDataField>
                    <MobileDataField label="Serie">
                      <DeviceIdentifier row={row} />
                    </MobileDataField>
                    <MobileDataField label="Fecha listo" value={formatDateOnly(row?.fecha_listo ?? "-")} />
                  </div>
                  <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                    {release && (
                      <button
                        className="btn w-full justify-center"
                        onClick={(e) => {
                          e.stopPropagation();
                          verOrdenSalida(row);
                        }}
                        disabled={remitoBusyId === rowId}
                        aria-busy={remitoBusyId === rowId ? "true" : "false"}
                        title="Ver o reimprimir orden de salida"
                      >
                        Ver OS
                      </button>
                    )}
                    {canDeliver && (
                      <button
                        className="btn-secondary w-full justify-center"
                        onClick={(e) => {
                          e.stopPropagation();
                          entregar(row);
                        }}
                        disabled={busyId === rowId}
                        aria-busy={busyId === rowId ? "true" : "false"}
                        title={isSalePending ? "Completar entrega de venta" : "Marcar como entregado"}
                      >
                        {isSalePending ? "Completar entrega" : "Entregado"}
                      </button>
                    )}
                  </div>
                </MobileDataCard>
              );
            })}
          </MobileDataList>
          <DesktopTableWrap>
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th scope="col" className="p-2">OS</th>
                <th scope="col" className="p-2">Cliente</th>
                <th scope="col" className="p-2">Equipo</th>
                <th scope="col" className="p-2">Resolución</th>
                <th scope="col" className="p-2">Serie</th>
                <th scope="col" className="p-2">Fecha listo</th>
                <th scope="col" className="p-2 text-right">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr
                  key={ingresoIdOf(row)}
                  onClick={() => go(row)}
                  onKeyDown={(e) => onRowKeyDown(e, row)}
                  className="hover:bg-gray-50 cursor-pointer border-t"
                  role="link"
                  tabIndex={0}
                  aria-label={`Abrir hoja de servicio de ${formatOS(row)}`}
                  data-testid={`row-${ingresoIdOf(row)}`}
                >
                  <td className="p-2 underline">{formatOS(row)}</td>
                  <td className="p-2">{row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}</td>
                  <td className="p-2">{catalogEquipmentLabel(row) ?? "-"}</td>
                  <td className="p-2">
                    <StatusChip value={resolutionLabel(row?.resolucion)} title="Resolución" />
                  </td>
                  <td className="p-2"><DeviceIdentifier row={row} /></td>
                  <td className="p-2 whitespace-nowrap">
                    {formatDateOnly(row?.fecha_listo ?? "-")}
                  </td>
                  <td className="p-2">
                    <div className="flex gap-2 justify-end">
                      {release && (
                        <button
                          className="btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            verOrdenSalida(row);
                          }}
                          disabled={remitoBusyId === ingresoIdOf(row)}
                          aria-busy={remitoBusyId === ingresoIdOf(row) ? "true" : "false"}
                          title="Ver o reimprimir orden de salida"
                        >
                          Ver OS
                        </button>
                      )}
                      {canDeliver && (
                        <button
                          className="btn-secondary"
                          onClick={(e) => {
                            e.stopPropagation();
                            entregar(row);
                          }}
                          disabled={busyId === ingresoIdOf(row)}
                          aria-busy={busyId === ingresoIdOf(row) ? "true" : "false"}
                          title={String(row?.estado || "").toLowerCase() === "vendido_pendiente_entrega" ? "Completar entrega de venta" : "Marcar como entregado"}
                        >
                          {String(row?.estado || "").toLowerCase() === "vendido_pendiente_entrega" ? "Completar entrega" : "Entregado"}
                        </button>
                      )}
                    </div>
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







