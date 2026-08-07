import test from "node:test";
import assert from "node:assert/strict";
import {
  compareTableValues,
  nextSort,
  parseSort,
  sortRows,
} from "./table-sort.js";

test("el ciclo de orden vuelve al orden predeterminado", () => {
  assert.equal(nextSort("", "os"), "os");
  assert.equal(nextSort("os", "os"), "-os");
  assert.equal(nextSort("-os", "os"), "");
  assert.deepEqual(parseSort("-fecha"), { field: "fecha", direction: "desc" });
});

test("ordena texto con números naturales y conserva los vacíos al final", () => {
  const rows = [
    { os: "OS 10" },
    { os: "" },
    { os: "OS 2" },
  ];
  const columns = { os: { accessor: (row) => row.os } };
  assert.deepEqual(sortRows(rows, "os", columns).map((row) => row.os), ["OS 2", "OS 10", ""]);
  assert.deepEqual(sortRows(rows, "-os", columns).map((row) => row.os), ["OS 10", "OS 2", ""]);
});

test("ordena fechas e importes por su valor real", () => {
  const rows = [
    { id: 1, fecha: "2026-07-02", monto: "1.200,50" },
    { id: 2, fecha: "2026-06-30", monto: "90,00" },
  ];
  assert.deepEqual(
    sortRows(rows, "fecha", { fecha: { accessor: (row) => row.fecha, type: "date" } }).map((row) => row.id),
    [2, 1]
  );
  assert.deepEqual(
    sortRows(rows, "-monto", { monto: { accessor: (row) => row.monto, type: "number" } }).map((row) => row.id),
    [1, 2]
  );
});

test("los nulos quedan al final en ambas direcciones", () => {
  assert.equal(compareTableValues(null, 1, { type: "number" }, "asc"), 1);
  assert.equal(compareTableValues(null, 1, { type: "number" }, "desc"), 1);
});

test("acepta getValue como alias de accessor", () => {
  const rows = [{ nombre: "Zulu" }, { nombre: "Alfa" }];
  assert.deepEqual(
    sortRows(rows, "cliente", { cliente: { getValue: (row) => row.nombre } }).map((row) => row.nombre),
    ["Alfa", "Zulu"],
  );
});
