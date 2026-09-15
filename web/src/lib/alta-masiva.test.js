import test from "node:test";
import assert from "node:assert/strict";
import {
  altaMasivaPayloadRows,
  altaMasivaRowsFromCells,
  altaMasivaTextFromCells,
  looksLikeScientificNotation,
  parseAltaMasivaText,
  removeAltaMasivaLines,
  splitAltaMasivaLine,
} from "./alta-masiva.js";

test("lee dos columnas pegadas desde Excel y descarta el encabezado", () => {
  const rows = parseAltaMasivaText("N/S\tMG\nE21BE708153\tMG 9525\r\nE21BE708154\t9526\n");
  assert.deepEqual(rows, [
    { linea: 2, numero_serie: "E21BE708153", numero_interno: "MG 9525" },
    { linea: 3, numero_serie: "E21BE708154", numero_interno: "9526" },
  ]);
});

test("conserva el número de renglón aunque haya líneas en blanco", () => {
  const rows = parseAltaMasivaText("A1\tMG 1\n\n  \nA2\tMG 2");
  assert.deepEqual(
    rows.map((row) => row.linea),
    [1, 4]
  );
});

test("sólo descarta el encabezado si es el primer renglón con datos", () => {
  const rows = parseAltaMasivaText("A1\tMG 1\nN/S\tMG");
  assert.equal(rows.length, 2);
});

test("acepta punto y coma, coma o espacios como separador", () => {
  assert.deepEqual(splitAltaMasivaLine("E21BE708153;MG 9525"), ["E21BE708153", "MG 9525"]);
  assert.deepEqual(splitAltaMasivaLine("E21BE708153,9525"), ["E21BE708153", "9525"]);
  assert.deepEqual(splitAltaMasivaLine("E21BE708153 MG 9525"), ["E21BE708153", "MG 9525"]);
});

test("el tab manda sobre la coma de una serie en notación científica", () => {
  assert.deepEqual(splitAltaMasivaLine("2,1E+11\tMG 9525"), ["2,1E+11", "MG 9525"]);
});

test("detecta las series que Excel pasó a notación científica", () => {
  assert.equal(looksLikeScientificNotation("2,1E+11"), true);
  assert.equal(looksLikeScientificNotation("2.10000708E+11"), true);
  assert.equal(looksLikeScientificNotation("210000708153"), false);
  assert.equal(looksLikeScientificNotation("E21BE708153"), false);
  assert.equal(looksLikeScientificNotation("21E3"), false);
});

test("quita los renglones indicados sin tocar los demás", () => {
  const text = "A1\tMG 1\nA2\tMG 2\nA3\tMG 3";
  assert.equal(removeAltaMasivaLines(text, [2]), "A1\tMG 1\nA3\tMG 3");
});

test("una planilla con encabezado se convierte al mismo texto que se pega", () => {
  const cells = [
    ["N/S", "MG"],
    ["E21BE708153", "9525"],
    ["", ""],
    ["E21BE708154", "MG 9526", "sobra"],
  ];
  assert.equal(altaMasivaTextFromCells(cells), "E21BE708153\t9525\nE21BE708154\tMG 9526");
});

test("una fila sin serie no corre el MG a la columna de la serie", () => {
  const rows = altaMasivaRowsFromCells([
    ["A1", "MG 1"],
    ["", "MG 2"],
  ]);
  assert.deepEqual(rows[1], { linea: 2, numero_serie: "", numero_interno: "MG 2" });
});

test("el payload manda el renglón como número de fila", () => {
  assert.deepEqual(altaMasivaPayloadRows([{ linea: 4, numero_serie: "A", numero_interno: "MG 1" }]), [
    { fila: 4, numero_serie: "A", numero_interno: "MG 1" },
  ]);
});
