import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { getGeneralEquipos } from "../lib/api";
import { formatDateOnly as formatDateOnlyHelper, formatOS as formatOSHelper, tipoEquipoOf, resolveFechaIngreso, resolveFechaCreacion } from "../lib/ui-helpers";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DesktopTableWrap, MobileDataCard, MobileDataField, MobileDataList } from "../components/Responsive.jsx";
import { MobileSortControl, SortableTh } from "../components/TableSortControls.jsx";
import useQueryState from "../hooks/useQueryState";
import { sortRows } from "../lib/table-sort";
import { INGRESO_LIST_SORT_COLUMNS, ingresoSortOptions } from "../lib/ingreso-list-sort";

export default function BuscarNS() {
  const [sp] = useSearchParams();
  const ns = (sp.get("serie") || "").trim();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [sort, setSort] = useQueryState("sort", "");
  const nav = useNavigate();

  useEffect(() => {
    (async () => {
      setLoading(true); setErr("");
      try {
        const normalizeToken = (value) =>
          String(value || "")
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]/g, "");

        const data = await getGeneralEquipos(ns ? { q: ns } : {});
        const safe = Array.isArray(data) ? data : [];

        // Filtrar por coincidencia exacta normalizada de N/S o MG y ordenar por fecha de creacion (desc)
        const needle = String(ns || "").trim();
        const compact = normalizeToken(needle);
        const mgMatch = /^mg\d{1,4}$/.test(compact);
        const onlyMatches = safe
          .filter(r => {
            const serieCompact = normalizeToken(r?.numero_serie);
            const internoCompact = normalizeToken(r?.numero_interno);
            if (mgMatch) {
              // Aceptar solo MG exacto (MG ####)
              const mgDigits = compact.replace(/^mg/, "");
              const mgRaw = `mg${mgDigits}`;
              const mgPadded = `mg${mgDigits.padStart(4, "0")}`;
              return (
                serieCompact === mgRaw ||
                internoCompact === mgRaw ||
                serieCompact === mgPadded ||
                internoCompact === mgPadded
              );
            }
            return serieCompact === compact || internoCompact === compact;
          })
          .sort((a, b) => {
            const tb = resolveFechaCreacion(b);
            const ta = resolveFechaCreacion(a);
            return (tb ? new Date(tb).getTime() : 0) - (ta ? new Date(ta).getTime() : 0);
          });
        setRows(onlyMatches);
      } catch (e) {
        setErr(e?.message || "Error cargando resultados");
      } finally { setLoading(false); }
    })();
  }, [ns]);

  const titulo = ns ? `Resultados para N/S o MG: ${ns}` : "Búsqueda por N/S o MG";
  const displayRows = useMemo(
    () => sortRows(rows, sort, INGRESO_LIST_SORT_COLUMNS),
    [rows, sort]
  );
  const sortOptions = ingresoSortOptions([
    "os",
    "marca",
    "modelo",
    "identificacion",
    "tipo",
    "fecha_ingreso",
  ]);

  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      <h1 className="text-2xl font-bold">{titulo}</h1>
      {err && <div className="bg-red-100 text-red-700 border border-red-300 p-2 rounded">{err}</div>}
      {loading ? "Cargando..." :
        displayRows.length === 0 ? <div className="text-sm text-gray-500">No se encontraron ingresos con ese N° de serie o MG.</div> :
        <div>
          <MobileSortControl sort={sort} onSortChange={setSort} options={sortOptions} className="mb-3" />
          <MobileDataList>
            {displayRows.map((r) => {
              const ingresoId = r?.id;
              return (
                <MobileDataCard
                  key={ingresoId}
                  as="button"
                  type="button"
                  className="hover:bg-gray-50"
                  onClick={() => nav(`/ingresos/${ingresoId}`)}
                >
                  <div className="font-semibold text-gray-900 underline">{formatOSHelper(r, "")}</div>
                  <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                    <MobileDataField label="Marca" value={r?.marca || "-"} />
                    <MobileDataField label="Modelo" value={r?.modelo || "-"} />
                    <MobileDataField label="Identificación">
                      <DeviceIdentifier row={r} />
                    </MobileDataField>
                    <MobileDataField label="Tipo" value={tipoEquipoOf(r)} />
                    <MobileDataField label="Fecha de ingreso" value={formatDateOnlyHelper(resolveFechaIngreso(r))} />
                  </div>
                </MobileDataCard>
              );
            })}
          </MobileDataList>
          <DesktopTableWrap>
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <SortableTh field="os" sort={sort} onSortChange={setSort} className="p-2">OS</SortableTh>
                <SortableTh field="marca" sort={sort} onSortChange={setSort} className="p-2">Marca</SortableTh>
                <SortableTh field="modelo" sort={sort} onSortChange={setSort} className="p-2">Modelo</SortableTh>
                <SortableTh field="identificacion" sort={sort} onSortChange={setSort} className="p-2">Identificación</SortableTh>
                <SortableTh field="tipo" sort={sort} onSortChange={setSort} className="p-2">Tipo</SortableTh>
                <SortableTh field="fecha_ingreso" sort={sort} onSortChange={setSort} className="p-2">Fecha de ingreso</SortableTh>
              </tr>
            </thead>
            <tbody>
              {displayRows.map((r) => {
                const ingresoId = r?.id;
                return (
                  <tr
                    key={ingresoId}
                    className="border-t hover:bg-gray-50 cursor-pointer"
                    onClick={() => nav(`/ingresos/${ingresoId}`)}
                    title="Ir a la hoja de servicio"
                  >
                    <td className="p-2 underline">{formatOSHelper(r, "")}</td>
                    <td className="p-2">{r?.marca || "-"}</td>
                    <td className="p-2">{r?.modelo || "-"}</td>
                    <td className="p-2"><DeviceIdentifier row={r} /></td>
                    <td className="p-2">{tipoEquipoOf(r)}</td>
                    <td className="p-2 whitespace-nowrap">{formatDateOnlyHelper(resolveFechaIngreso(r))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </DesktopTableWrap>
          <div className="text-xs text-gray-500 mt-2">Mostrando {displayRows.length} ingreso(s).</div>
        </div>}
    </div>
  );
}


