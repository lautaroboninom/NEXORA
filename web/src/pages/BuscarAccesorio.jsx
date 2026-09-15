import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { buscarAccesorioPorRef } from "../lib/api";
import { formatDateOnly as formatDateOnlyHelper, formatOS as formatOSHelper, resolveFechaIngreso } from "../lib/ui-helpers";
import CustomerCell from "../components/CustomerCell.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DerivadoMark } from "../components/DerivadoBadge.jsx";
import { DesktopTableWrap, MobileDataCard, MobileDataField, MobileDataList } from "../components/Responsive.jsx";
import { MobileSortControl, SortableTh } from "../components/TableSortControls.jsx";
import useQueryState from "../hooks/useQueryState";
import { sortRows } from "../lib/table-sort";
import { INGRESO_LIST_SORT_COLUMNS, ingresoSortOptions } from "../lib/ingreso-list-sort";

export default function BuscarAccesorio() {
  const [sp] = useSearchParams();
  const ref = (sp.get("ref") || "").trim();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [sort, setSort] = useQueryState("sort", "");
  const nav = useNavigate();

  useEffect(() => {
    (async () => {
      setLoading(true); setErr("");
      try {
        const data = await buscarAccesorioPorRef(ref);
        setRows(Array.isArray(data) ? data : []);
      } catch (e) {
        setErr(e?.message || "Error cargando resultados");
      } finally { setLoading(false); }
    })();
  }, [ref]);

  const titulo = ref ? `Servicios con referencia: ${ref}` : "Búsqueda por referencia de accesorio";
  const displayRows = useMemo(
    () => sortRows(rows, sort, INGRESO_LIST_SORT_COLUMNS),
    [rows, sort]
  );
  const sortOptions = ingresoSortOptions([
    "os",
    "accesorio",
    "referencia",
    "cliente",
    "equipo",
    "identificacion",
    "fecha_ingreso",
  ]);

  return (
    <div className="max-w-5xl mx-auto p-4 space-y-4">
      <h1 className="text-2xl font-bold">{titulo}</h1>
      {err && <div className="bg-red-100 text-red-700 border border-red-300 p-2 rounded">{err}</div>}
      {loading ? "Cargando..." :
        displayRows.length === 0 ? <div className="text-sm text-gray-500">No se encontraron servicios con esa referencia.</div> :
        <div>
          <MobileSortControl sort={sort} onSortChange={setSort} options={sortOptions} className="mb-3" />
          <MobileDataList>
            {displayRows.map((r) => {
              const ingresoId = r?.id ?? r?.ingreso_id;
              const equipo = [r?.marca, r?.modelo].filter(Boolean).join(" ");
              return (
                <MobileDataCard
                  key={`${ingresoId}-${r?.accesorio_nombre}-${r?.referencia}`}
                  as="button"
                  type="button"
                  className="hover:bg-gray-50"
                  onClick={() => nav(`/ingresos/${ingresoId}`)}
                >
                  <div className="font-semibold text-gray-900">
                    <span className="underline">{formatOSHelper(r, ingresoId)}</span>
                    <DerivadoMark row={r} className="ml-1.5 align-middle" />
                  </div>
                  <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                    <MobileDataField label="Accesorio" value={r?.accesorio_nombre || "-"} />
                    <MobileDataField label="Referencia" value={r?.referencia || "-"} />
                    <MobileDataField label="Cliente"><CustomerCell row={r} /></MobileDataField>
                    <MobileDataField label="Equipo" value={equipo || "-"} />
                    <MobileDataField label="Serie">
                      <DeviceIdentifier row={r} />
                    </MobileDataField>
                    <MobileDataField label="Fecha ingreso" value={formatDateOnlyHelper(resolveFechaIngreso(r))} />
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
                <SortableTh field="accesorio" sort={sort} onSortChange={setSort} className="p-2">Accesorio</SortableTh>
                <SortableTh field="referencia" sort={sort} onSortChange={setSort} className="p-2">Referencia</SortableTh>
                <SortableTh field="cliente" sort={sort} onSortChange={setSort} className="p-2">Cliente</SortableTh>
                <SortableTh field="equipo" sort={sort} onSortChange={setSort} className="p-2">Equipo</SortableTh>
                <SortableTh field="identificacion" sort={sort} onSortChange={setSort} className="p-2">Serie</SortableTh>
                <SortableTh field="fecha_ingreso" sort={sort} onSortChange={setSort} className="p-2">Fecha ingreso</SortableTh>
              </tr>
            </thead>
            <tbody>
              {displayRows.map((r) => {
                const ingresoId = r?.id ?? r?.ingreso_id;
                const equipo = [r?.marca, r?.modelo].filter(Boolean).join(" ");
                return (
                  <tr
                    key={`${ingresoId}-${r?.accesorio_nombre}-${r?.referencia}`}
                    className="border-t hover:bg-gray-50 cursor-pointer"
                    onClick={() => nav(`/ingresos/${ingresoId}`)}
                    title="Ir a la hoja de servicio"
                  >
                    <td className="p-2">
                      <span className="underline">{formatOSHelper(r, ingresoId)}</span>
                      <DerivadoMark row={r} className="ml-1.5 align-middle" />
                    </td>
                    <td className="p-2">{r?.accesorio_nombre || "-"}</td>
                    <td className="p-2">{r?.referencia || "-"}</td>
                    <td className="p-2"><CustomerCell row={r} /></td>
                    <td className="p-2">{equipo || "-"}</td>
                    <td className="p-2"><DeviceIdentifier row={r} /></td>
                    <td className="p-2 whitespace-nowrap">{formatDateOnlyHelper(resolveFechaIngreso(r))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </DesktopTableWrap>
          <div className="text-xs text-gray-500 mt-2">Mostrando {displayRows.length} resultado(s).</div>
        </div>}
    </div>
  );
}


