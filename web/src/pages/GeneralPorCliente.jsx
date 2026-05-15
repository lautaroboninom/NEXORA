// web/src/pages/GeneralPorCliente.jsx
import { useEffect, useMemo, useState } from "react";
import api, { getClientesBasico, downloadAuth } from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateOnly, norm, tipoEquipoOf, resolveFechaIngreso, resolveFechaCreacion, catalogEquipmentLabel } from "../lib/ui-helpers";
import useQueryState from "../hooks/useQueryState";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";



export default function GeneralPorCliente() {
  const [clientes, setClientes] = useState([]);
  const [loadingClientes, setLoadingClientes] = useState(true);
  const [errClientes, setErrClientes] = useState("");

  const [sel, setSel] = useQueryState("cliente_id", "");
  const [rows, setRows] = useState([]);
  const [loadingRows, setLoadingRows] = useState(false);
  const [errRows, setErrRows] = useState("");
  const [q, setQ] = useQueryState("q", "");
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [exporting, setExporting] = useState(false);

  const navigate = useNavigate();

  // Cargar listado de clientes
  useEffect(() => {
    (async () => {
      try {
        setErrClientes("");
        setLoadingClientes(true);
        const data = await getClientesBasico(); // /api/clientes/
        setClientes(Array.isArray(data) ? data : []);
      } catch (e) {
        setErrClientes(e?.message || "No se pudieron cargar los clientes");
        setClientes([]);
      } finally {
        setLoadingClientes(false);
      }
    })();
  }, []);

  // Buscar ingresos del cliente seleccionado
  async function buscar() {
    if (!sel) return;
    try {
      setErrRows("");
      setLoadingRows(true);
      const data = await api.get(`/api/clientes/${sel}/general/`);
      const list = Array.isArray(data) ? data : [];
      // Si necesits ordenar por fecha de ingreso (recientes primero):
      list.sort((a, b) => {
        const da = new Date(resolveFechaCreacion(a) ?? 0).getTime();
        const db = new Date(resolveFechaCreacion(b) ?? 0).getTime();
        return db - da;
      });
      setRows(list);
    } catch (e) {
      setErrRows(e?.message || "No se pudo cargar el general del cliente");
      setRows([]);
    } finally {
      setLoadingRows(false);
    }
  }

  const filtered = useMemo(() => {
    const needle = norm(q);
    if (!needle) return rows;
    return rows.filter((row) => {
      const campos = [
        formatOS(row),
        row?.marca ?? row?.equipo?.marca,
        catalogEquipmentLabel(row),
        tipoEquipoOf(row),
        row?.estado,
        row?.presupuesto_estado,
        row?.numero_serie,
        row?.numero_interno,
        row?.ubicacion_nombre ?? String(row?.ubicacion_id ?? ""),
      ];
      return campos.some((c) => norm(c).includes(needle));
    });
  }, [rows, q]);

  // Mantener el comportamiento actual: buscar sólo al presionar el botón

  const visibleIds = useMemo(() => new Set(filtered.map((r) => ingresoIdOf(r))), [filtered]);
  const allVisibleSelected = useMemo(() => {
    if (visibleIds.size === 0) return false;
    for (const id of visibleIds) if (!selectedIds.has(id)) return false;
    return true;
  }, [visibleIds, selectedIds]);

  const toggleSelectAllVisible = () => {
    const next = new Set(selectedIds);
    if (allVisibleSelected) {
      for (const id of visibleIds) next.delete(id);
    } else {
      for (const id of visibleIds) next.add(id);
    }
    setSelectedIds(next);
  };

  const toggleRow = (e, row) => {
    e.stopPropagation();
    const id = ingresoIdOf(row);
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelectedIds(next);
  };

  async function exportGeneral(ids) {
    if (!sel) return;
    try {
      setExporting(true);
      const base = `/api/clientes/${sel}/general/export/`;
      if (ids && ids.length) {
        const qs = new URLSearchParams({ ids: ids.join(",") }).toString();
        await downloadAuth(`${base}?${qs}`, `general_cliente_${sel}_${ids.length}.xlsx`);
      } else {
        await downloadAuth(base, `general_cliente_${sel}.xlsx`);
      }
    } catch (e) {
      setErrRows(e?.message || "No se pudo exportar el Excel");
    } finally {
      setExporting(false);
    }
  }

  // Cargar automáticamente cuando cambia el cliente (o si viene por URL)
  useEffect(() => {
    buscar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel]);

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
      <div className="h1 mb-3">General por cliente</div>

      {/* Errores de clientes */}
      {errClientes && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">
          {errClientes}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <select
          className="input"
          value={sel}
          onChange={(e) => setSel(e.target.value)}
          disabled={loadingClientes}
          aria-label="Elegir cliente"
        >
          <option value="">{loadingClientes ? "Cargando clientes" : "-- Eleg cliente --"}</option>
          {clientes.map((c) => (
            <option key={c.id} value={c.id}>
              {c.razon_social ?? c.nombre ?? `Cliente ${c.id}`}
            </option>
          ))}
        </select>

        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filtrar resultados por OS, equipo, serie, estado"
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar resultados"
        />
        <div className="flex items-center gap-2 ml-auto">
          <button
            className="btn"
            onClick={() => exportGeneral()}
            disabled={!sel || exporting || rows.length === 0}
            aria-busy={exporting ? "true" : "false"}
            title="Exportar todos los equipos del cliente"
          >
            Exportar todos
          </button>
          <button
            className="btn"
            onClick={() => exportGeneral(filtered.map(ingresoIdOf))}
            disabled={!sel || exporting || filtered.length === 0}
            aria-busy={exporting ? "true" : "false"}
            title="Exportar resultados filtrados a Excel"
          >
            Exportar filtrados
          </button>
          <button
            className="btn"
            onClick={() => exportGeneral(Array.from(selectedIds))}
            disabled={!sel || exporting || selectedIds.size === 0}
            aria-busy={exporting ? "true" : "false"}
            title="Exportar seleccin a Excel"
          >
            Exportar seleccin
          </button>
        </div>
      </div>

      {/* Errores de la bsqueda */}
      {errRows && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3">
          {errRows}
        </div>
      )}

      {loadingRows ? (
        "Cargando"
      ) : rows.length === 0 && sel ? (
        <div className="text-sm text-gray-500">No hay resultados para este cliente.</div>
      ) : rows.length === 0 ? (
        <div className="text-sm text-gray-500">
          Eleg un cliente y presion <span className="font-medium">Buscar</span>.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th scope="col" className="p-2">
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleSelectAllVisible}
                    aria-label="Seleccionar todos los visibles"
                  />
                </th>
                <th scope="col" className="p-2">OS</th>
                <th scope="col" className="p-2">Equipo</th>
                <th scope="col" className="p-2">Serie</th>
                <th scope="col" className="p-2">Estado</th>
                <th scope="col" className="p-2">Presupuesto</th>
                <th scope="col" className="p-2">Ubicacin</th>
                <th scope="col" className="p-2">Fecha ingreso</th>
                <th scope="col" className="p-2">Fecha presupuestado</th>
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
                  <td className="p-2" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectedIds.has(ingresoIdOf(row))}
                      onChange={(e) => toggleRow(e, row)}
                      aria-label={`Seleccionar ${formatOS(row)}`}
                    />
                  </td>
                  <td className="p-2 underline">{formatOS(row)}</td>
                  <td className="p-2">{catalogEquipmentLabel(row)}</td>
                  <td className="p-2"><DeviceIdentifier row={row} /></td>
                  <td className="p-2">{row?.estado ?? "-"}</td>
                  <td className="p-2">{(() => {
                    const v = row?.presupuesto_estado;
                    if (!v) return "-";
                    if (v === "presupuestado") return "Presupuestado";
                    if (v === "no_aplica") return "No aplica";
                    try { const s = String(v); return s.charAt(0).toUpperCase() + s.slice(1); } catch { return String(v); }
                  })()}</td>
                  <td className="p-2">{row?.ubicacion_nombre ?? row?.ubicacion_id ?? "-"}</td>
                  <td className="p-2 whitespace-nowrap">{formatDateOnly(resolveFechaIngreso(row))}</td>
                  <td className="p-2 whitespace-nowrap">
                {formatDateOnly(row?.presupuesto_fecha_emision || row?.presupuesto_fecha_envio)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="text-xs text-gray-500 mt-2">
            Mostrando {filtered.length} de {rows.length}.
          </div>
        </div>
      )}
    </div>
  );
}
