import test from "node:test";
import assert from "node:assert/strict";
import {
  EMPTY_FILTER,
  activeFilterCount,
  columnFilterOptions,
  columnFilterValue,
  matchesColumnFilters,
  parseColumnFilters,
  serializeColumnFilters,
} from "./ingreso-list-filters.js";

const COLUMNS = [
  { field: "cliente", label: "Cliente", filter: "text" },
  { field: "estado", label: "Estado", filter: "select" },
  { field: "equipo", label: "Equipo" },
];

const ROWS = [
  { id: 1, razon_social: "ACUDIR S.A.", estado: "reparado" },
  { id: 2, razon_social: "Sanatorio Güemes", estado: "no_se_repara" },
  { id: 3, razon_social: "ACUDIR S.A.", estado: "" },
];

test("el filtro de texto ignora acentos y mayúsculas", () => {
  const filtros = { cliente: "guemes" };
  assert.equal(matchesColumnFilters(ROWS[1], COLUMNS, filtros), true);
  assert.equal(matchesColumnFilters(ROWS[0], COLUMNS, filtros), false);
});

test("el filtro de select pide coincidencia exacta", () => {
  assert.equal(matchesColumnFilters(ROWS[0], COLUMNS, { estado: "reparado" }), true);
  assert.equal(matchesColumnFilters(ROWS[1], COLUMNS, { estado: "reparado" }), false);
});

test("se puede filtrar por columna vacía", () => {
  assert.equal(matchesColumnFilters(ROWS[2], COLUMNS, { estado: EMPTY_FILTER }), true);
  assert.equal(matchesColumnFilters(ROWS[0], COLUMNS, { estado: EMPTY_FILTER }), false);
});

test("los filtros se acumulan y las columnas sin filtro se ignoran", () => {
  const filtros = { cliente: "acudir", estado: "reparado", equipo: "loquesea" };
  assert.equal(matchesColumnFilters(ROWS[0], COLUMNS, filtros), true);
  assert.equal(matchesColumnFilters(ROWS[2], COLUMNS, filtros), false);
});

test("un filtro vacío no descarta nada", () => {
  assert.equal(matchesColumnFilters(ROWS[1], COLUMNS, { cliente: "", estado: "" }), true);
});

test("toma el valor por el accessor que la columna ya usa para ordenar", () => {
  assert.equal(columnFilterValue(ROWS[0], COLUMNS[0]), "ACUDIR S.A.");
  const propia = { field: "custom", filterValue: (row) => `OS-${row.id}` };
  assert.equal(columnFilterValue(ROWS[0], propia), "OS-1");
});

test("las opciones del select salen de los datos, ordenadas y sin repetir", () => {
  const columnas = [
    { field: "estado", filter: "select", filterLabel: (value) => value.replace(/_/g, " ") },
  ];
  const options = columnFilterOptions(ROWS, columnas);
  assert.deepEqual(options.estado, [
    { value: "no_se_repara", label: "no se repara" },
    { value: "reparado", label: "reparado" },
    { value: EMPTY_FILTER, label: "(sin dato)" },
  ]);
});

test("no ofrece (sin dato) si ninguna fila tiene la columna vacía", () => {
  const options = columnFilterOptions(ROWS.slice(0, 2), [{ field: "estado", filter: "select" }]);
  assert.deepEqual(
    options.estado.map((option) => option.value),
    ["no_se_repara", "reparado"]
  );
});

test("cuenta sólo los filtros con algo cargado", () => {
  assert.equal(activeFilterCount({ cliente: "acudir", estado: "", equipo: "  " }), 1);
  assert.equal(activeFilterCount({}), 0);
});

test("los filtros viajan en la URL y vuelven iguales", () => {
  const filtros = { cliente: "ACUDIR S.A.", estado: "no_se_repara" };
  const raw = serializeColumnFilters(filtros);
  assert.equal(raw, "cliente:ACUDIR%20S.A.;estado:no_se_repara");
  assert.deepEqual(parseColumnFilters(raw), filtros);
});

test("sobrevive a valores con los separadores adentro", () => {
  const filtros = { cliente: "a;b:c" };
  assert.deepEqual(parseColumnFilters(serializeColumnFilters(filtros)), filtros);
});

test("una URL rota no rompe la página", () => {
  assert.deepEqual(parseColumnFilters(""), {});
  assert.deepEqual(parseColumnFilters("basura"), {});
  assert.deepEqual(parseColumnFilters(":sinCampo"), {});
  assert.deepEqual(parseColumnFilters("cliente:%E0%A4%A"), {});
});
