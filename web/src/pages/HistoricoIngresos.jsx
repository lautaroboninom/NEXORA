// web/src/pages/HistoricoIngresos.jsx
import { Fragment, useEffect, useRef, useState } from "react";
import { getHistoricoIngresos, downloadAuth } from "../lib/api";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ingresoIdOf, formatOS, formatDateTime, tipoEquipoOf, resolveFechaIngreso } from "../lib/ui-helpers";
import useQueryState from "../hooks/useQueryState";
import useDebouncedValue from "../hooks/useDebouncedValue";
import StatusChip from "../components/StatusChip.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import { DesktopTableWrap, MobileDataCard, MobileDataField, MobileDataList } from "../components/Responsive.jsx";
import { MobileSortControl, SortableTh } from "../components/TableSortControls.jsx";
import { ingresoSortOptions } from "../lib/ingreso-list-sort";

export default function HistoricoIngresos() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [page, setPage] = useState(1);
  const [hasNext, setHasNext] = useState(false);
  const [err, setErr] = useState("");
  const [exporting, setExporting] = useState(false);
  const [q, setQ] = useQueryState("q", "");
  const [sort, setSort] = useQueryState("sort", "");
  const [qDebounced, setQDebounced] = useState(q);
  const [osFilter, setOsFilter] = useQueryState("os", "");
  const [clienteFilter, setClienteFilter] = useQueryState("cliente", "");
  const [tipoFilter, setTipoFilter] = useQueryState("tipo_equipo", "");
  const [marcaFilter, setMarcaFilter] = useQueryState("marca", "");
  const [modeloFilter, setModeloFilter] = useQueryState("modelo", "");
  const [varianteFilter, setVarianteFilter] = useQueryState("variante", "");
  const [estadoFilter, setEstadoFilter] = useQueryState("estado_q", "");
  const [serieFilter, setSerieFilter] = useQueryState("numero_serie", "");
  const [mgFilter, setMgFilter] = useQueryState("numero_interno", "");
  const [fechaIngresoFrom, setFechaIngresoFrom] = useQueryState("fecha_ingreso_from", "");
  const [fechaIngresoTo, setFechaIngresoTo] = useQueryState("fecha_ingreso_to", "");
  const [fechaLiberacionFrom, setFechaLiberacionFrom] = useQueryState("fecha_liberacion_from", "");
  const [fechaLiberacionTo, setFechaLiberacionTo] = useQueryState("fecha_liberacion_to", "");
  const [fechaEntregaFrom, setFechaEntregaFrom] = useQueryState("fecha_entrega_from", "");
  const [fechaEntregaTo, setFechaEntregaTo] = useQueryState("fecha_entrega_to", "");
  const [fechaIngresoFromDraft, setFechaIngresoFromDraft] = useState(fechaIngresoFrom);
  const [fechaIngresoToDraft, setFechaIngresoToDraft] = useState(fechaIngresoTo);
  const [fechaLiberacionFromDraft, setFechaLiberacionFromDraft] = useState(fechaLiberacionFrom);
  const [fechaLiberacionToDraft, setFechaLiberacionToDraft] = useState(fechaLiberacionTo);
  const [fechaEntregaFromDraft, setFechaEntregaFromDraft] = useState(fechaEntregaFrom);
  const [fechaEntregaToDraft, setFechaEntregaToDraft] = useState(fechaEntregaTo);
  const osFilterDebounced = useDebouncedValue(osFilter, 300);
  const clienteFilterDebounced = useDebouncedValue(clienteFilter, 300);
  const tipoFilterDebounced = useDebouncedValue(tipoFilter, 300);
  const marcaFilterDebounced = useDebouncedValue(marcaFilter, 300);
  const modeloFilterDebounced = useDebouncedValue(modeloFilter, 300);
  const varianteFilterDebounced = useDebouncedValue(varianteFilter, 300);
  const estadoFilterDebounced = useDebouncedValue(estadoFilter, 300);
  const serieFilterDebounced = useDebouncedValue(serieFilter, 300);
  const mgFilterDebounced = useDebouncedValue(mgFilter, 300);
  const navigate = useNavigate();
  const hasLoadedOnceRef = useRef(false);
  const [search, setSearch] = useSearchParams();
  const pageSize = 200;
  const delivered = search.get("delivered") || "";
  const compatFrom = search.get("from") || "";
  const compatTo = search.get("to") || "";
  const fechaEntregaFromEffective = fechaEntregaFrom || (delivered === "1" ? compatFrom : "");
  const fechaEntregaToEffective = fechaEntregaTo || (delivered === "1" ? compatTo : "");

  useEffect(() => {
    if (!q) {
      setQDebounced("");
      return;
    }
    const h = setTimeout(() => setQDebounced(q), 300);
    return () => clearTimeout(h);
  }, [q]);

  useEffect(() => {
    if (delivered !== "1") return;
    if (!compatFrom && !compatTo) return;
    if (fechaEntregaFrom || fechaEntregaTo) return;
    if (compatFrom) setFechaEntregaFrom(compatFrom);
    if (compatTo) setFechaEntregaTo(compatTo);
    const next = new URLSearchParams(search.toString());
    next.delete("from");
    next.delete("to");
    setSearch(next, { replace: true });
  }, [
    delivered,
    compatFrom,
    compatTo,
    fechaEntregaFrom,
    fechaEntregaTo,
    search,
    setSearch,
    setFechaEntregaFrom,
    setFechaEntregaTo,
  ]);

  useEffect(() => {
    setFechaIngresoFromDraft(fechaIngresoFrom || "");
    setFechaIngresoToDraft(fechaIngresoTo || "");
    setFechaLiberacionFromDraft(fechaLiberacionFrom || "");
    setFechaLiberacionToDraft(fechaLiberacionTo || "");
    setFechaEntregaFromDraft(fechaEntregaFrom || "");
    setFechaEntregaToDraft(fechaEntregaTo || "");
  }, [
    fechaIngresoFrom,
    fechaIngresoTo,
    fechaLiberacionFrom,
    fechaLiberacionTo,
    fechaEntregaFrom,
    fechaEntregaTo,
  ]);

  const commitDate = (draftValue, currentValue, setter) => {
    const next = draftValue || "";
    const current = currentValue || "";
    if (next !== current) setter(next);
  };

  const onDateKeyDown = (e, commitFn) => {
    if (e.key !== "Enter") return;
    commitFn();
    e.currentTarget.blur();
  };

  const qEffective = (qDebounced || "").trim();
  const buildFilterParams = ({ includePaging = false, pageValue = 1 } = {}) => {
    const params = new URLSearchParams();
    if (delivered === "1") params.set("delivered", "1");
    if (osFilterDebounced) params.set("os", osFilterDebounced);
    if (clienteFilterDebounced) params.set("cliente", clienteFilterDebounced);
    if (tipoFilterDebounced) params.set("tipo_equipo", tipoFilterDebounced);
    if (marcaFilterDebounced) params.set("marca", marcaFilterDebounced);
    if (modeloFilterDebounced) params.set("modelo", modeloFilterDebounced);
    if (varianteFilterDebounced) params.set("variante", varianteFilterDebounced);
    if (estadoFilterDebounced) params.set("estado_q", estadoFilterDebounced);
    if (serieFilterDebounced) params.set("numero_serie", serieFilterDebounced);
    if (mgFilterDebounced) params.set("numero_interno", mgFilterDebounced);
    if (fechaIngresoFrom) params.set("fecha_ingreso_from", fechaIngresoFrom);
    if (fechaIngresoTo) params.set("fecha_ingreso_to", fechaIngresoTo);
    if (fechaLiberacionFrom) params.set("fecha_liberacion_from", fechaLiberacionFrom);
    if (fechaLiberacionTo) params.set("fecha_liberacion_to", fechaLiberacionTo);
    if (fechaEntregaFromEffective) params.set("fecha_entrega_from", fechaEntregaFromEffective);
    if (fechaEntregaToEffective) params.set("fecha_entrega_to", fechaEntregaToEffective);
    if (qEffective) params.set("q", qEffective);
    if (sort) params.set("sort", sort);
    if (includePaging) {
      params.set("page", String(pageValue));
      params.set("page_size", String(pageSize));
    }
    return params;
  };

  async function loadPage(p = 1, { reset = false } = {}) {
    try {
      if (reset) {
        setRows([]);
        setHasNext(false);
        setPage(1);
      }
      const isFirst = reset || p === 1;
      isFirst ? setLoading(true) : setLoadingMore(true);
      setErr("");

      const params = buildFilterParams({ includePaging: true, pageValue: p });

      const res = await getHistoricoIngresos(Object.fromEntries(params.entries()));
      const pageItems = Array.isArray(res) ? (res || []) : (res.items || []);
      const next = Array.isArray(res) ? false : !!res.has_next;

      setRows((prev) => (isFirst ? pageItems : [...prev, ...pageItems]));
      setHasNext(next);
      setPage(p);
    } catch (e) {
      setErr(e?.message || "No se pudo cargar el histórico de equipos");
      if (reset) setRows([]);
    } finally {
      setLoading(false);
      setLoadingMore(false);
      hasLoadedOnceRef.current = true;
    }
  }

  async function exportFiltered() {
    try {
      setExporting(true);
      setErr("");
      const params = buildFilterParams({ includePaging: false });
      const qs = params.toString();
      await downloadAuth(
        `/api/ingresos/historico/export/${qs ? `?${qs}` : ""}`,
        "historico_ingresos.xlsx"
      );
    } catch (e) {
      setErr(e?.message || "No se pudo exportar el Excel del historial");
    } finally {
      setExporting(false);
    }
  }

  useEffect(() => {
    loadPage(1, { reset: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    delivered,
    qEffective,
    osFilterDebounced,
    clienteFilterDebounced,
    tipoFilterDebounced,
    marcaFilterDebounced,
    modeloFilterDebounced,
    varianteFilterDebounced,
    estadoFilterDebounced,
    serieFilterDebounced,
    mgFilterDebounced,
    fechaIngresoFrom,
    fechaIngresoTo,
    fechaLiberacionFrom,
    fechaLiberacionTo,
    fechaEntregaFromEffective,
    fechaEntregaToEffective,
    sort,
  ]);

  const filtered = rows;
  const isDeletedRow = (row) => Boolean(row?.eliminado) || String(row?.estado || "").toLowerCase() === "eliminado";
  const rowKey = (row) => `${isDeletedRow(row) ? "deleted" : "ingreso"}-${ingresoIdOf(row)}`;

  const go = (row) => {
    if (isDeletedRow(row)) return;
    const id = ingresoIdOf(row);
    if (!id) return;
    navigate(`/ingresos/${id}`);
  };

  const onRowKeyDown = (e, row) => {
    if (isDeletedRow(row)) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      go(row);
    }
  };

  const sentinelRef = useRef(null);
  useEffect(() => {
    if (!hasNext) return;
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && !loadingMore) {
          loadPage(page + 1);
        }
      }
    });
    io.observe(el);
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasNext, page, loadingMore]);

  return (
    <div className="card">
      <div className="h1 mb-3">Histórico ingresos</div>

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
          placeholder="Filtrar por OS, cliente, tipo de equipo, marca, modelo, variante, estado, serie..."
          className="border rounded p-2 w-full max-w-md"
          aria-label="Filtrar histórico"
        />
        <button
          className="btn"
          onClick={() => loadPage(1, { reset: true })}
          title="Recargar lista"
          disabled={loading}
          aria-busy={loading ? "true" : "false"}
        >
          Recargar
        </button>
        <button
          className="btn"
          onClick={exportFiltered}
          title="Exportar resultados filtrados"
          disabled={loading || exporting}
          aria-busy={exporting ? "true" : "false"}
        >
          {exporting ? "Exportando..." : "Exportar filtrados"}
        </button>
      </div>

      {loading && !hasLoadedOnceRef.current ? (
        "Cargando..."
      ) : (
        <div>
          {loading && (
            <div className="text-xs text-gray-500 mb-2">Actualizando…</div>
          )}
          <MobileSortControl
            sort={sort}
            onSortChange={setSort}
            options={ingresoSortOptions([
              "os",
              "cliente",
              "tipo",
              "marca",
              "modelo",
              "variante",
              "estado",
              "numero_serie",
              "numero_interno",
              "fecha_ingreso",
              "fecha_liberacion",
              "fecha_entrega",
            ])}
            className="mb-3"
          />
          <MobileDataList>
            {filtered.length === 0 ? (
              <MobileDataCard className="text-gray-500">
                {loading ? "Cargando…" : "No hay resultados que coincidan con el filtro."}
              </MobileDataCard>
            ) : (
              filtered.map((row) => {
                const deleted = isDeletedRow(row);
                return (
                <MobileDataCard
                  key={rowKey(row)}
                  as={deleted ? "div" : "button"}
                  type={deleted ? undefined : "button"}
                  onClick={deleted ? undefined : () => go(row)}
                  className={deleted ? "border-gray-300 bg-gray-50 text-gray-700" : "hover:bg-gray-50"}
                  aria-label={deleted ? `${formatOS(row)} eliminado` : `Abrir hoja de servicio de ${formatOS(row)}`}
                  data-testid={`row-mobile-${rowKey(row)}`}
                >
                  <div className={`font-semibold text-gray-900 ${deleted ? "" : "underline"}`}>{formatOS(row)}</div>
                  <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                    <MobileDataField label="Cliente" value={row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"} />
                    <MobileDataField label="Equipo" value={[tipoEquipoOf(row), row?.marca ?? row?.equipo?.marca, row?.modelo ?? row?.equipo?.modelo].filter(Boolean).join(" · ") || "-"} />
                    <MobileDataField label="Variante" value={row?.equipo_variante ?? row?.variante ?? row?.modelo_variante ?? "-"} />
                    <MobileDataField label="Estado">
                      <StatusChip value={row?.estado} />
                    </MobileDataField>
                    <MobileDataField label="Identificación">
                      <DeviceIdentifier row={row} />
                    </MobileDataField>
                    <MobileDataField label="Fecha ingreso" value={formatDateTime(resolveFechaIngreso(row))} />
                    <MobileDataField label="Fecha liberación" value={formatDateTime(row?.fecha_liberacion)} />
                    <MobileDataField label="Fecha entrega" value={formatDateTime(row?.fecha_entrega)} />
                    {deleted && (
                      <MobileDataField
                        label="Trazabilidad"
                        value={row?.eliminacion_motivo || "Ingreso eliminado por corrección operativa."}
                        className="min-[420px]:col-span-2"
                      />
                    )}
                  </div>
                </MobileDataCard>
                );
              })
            )}
          </MobileDataList>
          <DesktopTableWrap>
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <SortableTh field="os" sort={sort} onSortChange={setSort} className="p-2">OS</SortableTh>
                <SortableTh field="cliente" sort={sort} onSortChange={setSort} className="p-2">Cliente</SortableTh>
                <SortableTh field="tipo" sort={sort} onSortChange={setSort} className="p-2">Tipo de equipo</SortableTh>
                <SortableTh field="marca" sort={sort} onSortChange={setSort} className="p-2">Marca</SortableTh>
                <SortableTh field="modelo" sort={sort} onSortChange={setSort} className="p-2">Modelo</SortableTh>
                <SortableTh field="variante" sort={sort} onSortChange={setSort} className="p-2">Variante</SortableTh>
                <SortableTh field="estado" sort={sort} onSortChange={setSort} className="p-2">Estado</SortableTh>
                <SortableTh field="numero_serie" sort={sort} onSortChange={setSort} className="p-2">N/S</SortableTh>
                <SortableTh field="numero_interno" sort={sort} onSortChange={setSort} className="p-2">MG</SortableTh>
                <SortableTh field="fecha_ingreso" sort={sort} onSortChange={setSort} className="p-2">Fecha ingreso</SortableTh>
                <SortableTh field="fecha_liberacion" sort={sort} onSortChange={setSort} className="p-2">Fecha liberación</SortableTh>
                <SortableTh field="fecha_entrega" sort={sort} onSortChange={setSort} className="p-2">Fecha entrega</SortableTh>
              </tr>
              <tr className="text-left">
                <th className="p-2">
                  <input
                    type="text"
                    value={osFilter}
                    onChange={(e) => setOsFilter(e.target.value)}
                    placeholder="OS"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar OS"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={clienteFilter}
                    onChange={(e) => setClienteFilter(e.target.value)}
                    placeholder="Cliente"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar cliente"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={tipoFilter}
                    onChange={(e) => setTipoFilter(e.target.value)}
                    placeholder="Tipo"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar tipo de equipo"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={marcaFilter}
                    onChange={(e) => setMarcaFilter(e.target.value)}
                    placeholder="Marca"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar marca"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={modeloFilter}
                    onChange={(e) => setModeloFilter(e.target.value)}
                    placeholder="Modelo"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar modelo"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={varianteFilter}
                    onChange={(e) => setVarianteFilter(e.target.value)}
                    placeholder="Variante"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar variante"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={estadoFilter}
                    onChange={(e) => setEstadoFilter(e.target.value)}
                    placeholder="Estado"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar estado"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={serieFilter}
                    onChange={(e) => setSerieFilter(e.target.value)}
                    placeholder="N/S"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar número de serie"
                  />
                </th>
                <th className="p-2">
                  <input
                    type="text"
                    value={mgFilter}
                    onChange={(e) => setMgFilter(e.target.value)}
                    placeholder="MG"
                    className="border rounded p-1 w-full text-xs"
                    aria-label="Filtrar MG"
                  />
                </th>
                <th className="p-2 align-top">
                  <div className="flex flex-col gap-1">
                    <label className="text-[10px] leading-none text-gray-500">
                      Desde
                      <input
                        type="date"
                        value={fechaIngresoFromDraft}
                        onChange={(e) => setFechaIngresoFromDraft(e.target.value)}
                        onBlur={() => commitDate(fechaIngresoFromDraft, fechaIngresoFrom, setFechaIngresoFrom)}
                        onKeyDown={(e) => onDateKeyDown(e, () => commitDate(fechaIngresoFromDraft, fechaIngresoFrom, setFechaIngresoFrom))}
                        className="border rounded p-1 w-full text-[11px] mt-0.5"
                        aria-label="Fecha ingreso desde"
                      />
                    </label>
                    <label className="text-[10px] leading-none text-gray-500">
                      Hasta
                      <input
                        type="date"
                        value={fechaIngresoToDraft}
                        onChange={(e) => setFechaIngresoToDraft(e.target.value)}
                        onBlur={() => commitDate(fechaIngresoToDraft, fechaIngresoTo, setFechaIngresoTo)}
                        onKeyDown={(e) => onDateKeyDown(e, () => commitDate(fechaIngresoToDraft, fechaIngresoTo, setFechaIngresoTo))}
                        className="border rounded p-1 w-full text-[11px] mt-0.5"
                        aria-label="Fecha ingreso hasta"
                      />
                    </label>
                  </div>
                </th>
                <th className="p-2 align-top">
                  <div className="flex flex-col gap-1">
                    <label className="text-[10px] leading-none text-gray-500">
                      Desde
                      <input
                        type="date"
                        value={fechaLiberacionFromDraft}
                        onChange={(e) => setFechaLiberacionFromDraft(e.target.value)}
                        onBlur={() => commitDate(fechaLiberacionFromDraft, fechaLiberacionFrom, setFechaLiberacionFrom)}
                        onKeyDown={(e) => onDateKeyDown(e, () => commitDate(fechaLiberacionFromDraft, fechaLiberacionFrom, setFechaLiberacionFrom))}
                        className="border rounded p-1 w-full text-[11px] mt-0.5"
                        aria-label="Fecha liberacion desde"
                      />
                    </label>
                    <label className="text-[10px] leading-none text-gray-500">
                      Hasta
                      <input
                        type="date"
                        value={fechaLiberacionToDraft}
                        onChange={(e) => setFechaLiberacionToDraft(e.target.value)}
                        onBlur={() => commitDate(fechaLiberacionToDraft, fechaLiberacionTo, setFechaLiberacionTo)}
                        onKeyDown={(e) => onDateKeyDown(e, () => commitDate(fechaLiberacionToDraft, fechaLiberacionTo, setFechaLiberacionTo))}
                        className="border rounded p-1 w-full text-[11px] mt-0.5"
                        aria-label="Fecha liberacion hasta"
                      />
                    </label>
                  </div>
                </th>
                <th className="p-2 align-top">
                  <div className="flex flex-col gap-1">
                    <label className="text-[10px] leading-none text-gray-500">
                      Desde
                      <input
                        type="date"
                        value={fechaEntregaFromDraft}
                        onChange={(e) => setFechaEntregaFromDraft(e.target.value)}
                        onBlur={() => commitDate(fechaEntregaFromDraft, fechaEntregaFrom, setFechaEntregaFrom)}
                        onKeyDown={(e) => onDateKeyDown(e, () => commitDate(fechaEntregaFromDraft, fechaEntregaFrom, setFechaEntregaFrom))}
                        className="border rounded p-1 w-full text-[11px] mt-0.5"
                        aria-label="Fecha entrega desde"
                      />
                    </label>
                    <label className="text-[10px] leading-none text-gray-500">
                      Hasta
                      <input
                        type="date"
                        value={fechaEntregaToDraft}
                        onChange={(e) => setFechaEntregaToDraft(e.target.value)}
                        onBlur={() => commitDate(fechaEntregaToDraft, fechaEntregaTo, setFechaEntregaTo)}
                        onKeyDown={(e) => onDateKeyDown(e, () => commitDate(fechaEntregaToDraft, fechaEntregaTo, setFechaEntregaTo))}
                        className="border rounded p-1 w-full text-[11px] mt-0.5"
                        aria-label="Fecha entrega hasta"
                      />
                    </label>
                  </div>
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={12} className="p-2 text-sm text-gray-500">
                    {loading ? "Cargando…" : "No hay resultados que coincidan con el filtro."}
                  </td>
                </tr>
              ) : (
                filtered.map((row) => {
                  const deleted = isDeletedRow(row);
                  return (
                  <Fragment key={rowKey(row)}>
                  <tr
                    onClick={deleted ? undefined : () => go(row)}
                    onKeyDown={(e) => onRowKeyDown(e, row)}
                    className={deleted ? "bg-gray-50 text-gray-700" : "hover:bg-gray-50 cursor-pointer"}
                    role={deleted ? undefined : "link"}
                    tabIndex={deleted ? undefined : 0}
                    aria-label={deleted ? `${formatOS(row)} eliminado` : `Abrir hoja de servicio de ${formatOS(row)}`}
                    data-testid={`row-${rowKey(row)}`}
                  >
                    <td className={`p-2 ${deleted ? "font-medium" : "underline"}`}>{formatOS(row)}</td>
                    <td className="p-2">
                      {row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}
                    </td>
                    <td className="p-2">{tipoEquipoOf(row)}</td>
                    <td className="p-2">{row?.marca ?? row?.equipo?.marca ?? "-"}</td>
                    <td className="p-2">{row?.modelo ?? row?.equipo?.modelo ?? "-"}</td>
                    <td className="p-2">{row?.equipo_variante ?? row?.variante ?? row?.modelo_variante ?? "-"}</td>
                    <td className="p-2"><StatusChip value={row?.estado} /></td>
                    <td className="p-2" colSpan={2}><DeviceIdentifier row={row} /></td>
                    <td className="p-2 whitespace-nowrap">{formatDateTime(resolveFechaIngreso(row))}</td>
                    <td className="p-2 whitespace-nowrap">{formatDateTime(row?.fecha_liberacion)}</td>
                    <td className="p-2 whitespace-nowrap">{formatDateTime(row?.fecha_entrega)}</td>
                  </tr>
                  {deleted && (
                    <tr className="bg-gray-50 text-gray-600">
                      <td className="px-2 pb-2 text-xs" colSpan={12}>
                        {row?.eliminacion_motivo || "Ingreso eliminado por corrección operativa."}
                      </td>
                    </tr>
                  )}
                  </Fragment>
                  );
                })
              )}
            </tbody>
          </table>
          </DesktopTableWrap>
          <div className="text-xs text-gray-500 mt-2">
            Mostrando {filtered.length} de {rows.length}. {hasNext ? "Desplazá para cargar más…" : ""}
          </div>
          <div ref={sentinelRef} style={{ height: 1 }} />
          {loadingMore && (
            <div className="text-xs text-gray-500 mt-2">Cargando más…</div>
          )}
        </div>
      )}
    </div>
  );
}
