import test from "node:test";
import assert from "node:assert/strict";
import {
  applyPartidasMasivas,
  looksLikeScientificNotation,
  parsePartidasMasivasText,
  partidasMasivasCargadas,
  partidasMasivasPayloadRows,
  partidasMasivasResultado,
  partidasMasivasTextFromCells,
  removePartidasMasivasLines,
  splitPartidasMasivasLine,
} from "./partidas-masivas.js";

const okRow = (fila, partida, extra = {}) => ({
  fila,
  partida,
  estado: "ok",
  accion: "cargar",
  errores: [],
  avisos: [],
  lote: {
    partida,
    stockDepositCode: "SAR",
    stockAvailableQuantity: 1,
    partidaExpirationDate: null,
    stockCheckedAt: "2026-09-11T10:00:00",
    ...extra,
  },
});

test("una partida por renglón, descartando el encabezado y las líneas en blanco", () => {
  assert.deepEqual(parsePartidasMasivasText("Partida\nE238D501001\r\n\n  \nE238D501002\n"), [
    { linea: 2, partida: "E238D501001" },
    { linea: 5, partida: "E238D501002" },
  ]);
});

test("reconoce los encabezados de serie habituales", () => {
  for (const header of ["N/S", "NS", "S/N", "Serie", "Nro. de serie", "N° de serie", "Número de serie", "Partidas"]) {
    assert.deepEqual(parsePartidasMasivasText(`${header}\nA1`), [{ linea: 2, partida: "A1" }], header);
  }
});

test("sólo el primer renglón con datos puede ser encabezado", () => {
  assert.deepEqual(
    parsePartidasMasivasText("A1\nSerie").map((row) => row.partida),
    ["A1", "Serie"]
  );
});

test("de varias columnas pegadas toma la primera; el espacio no separa", () => {
  assert.deepEqual(parsePartidasMasivasText("E21BE708153\tMG 9525\nMG 1234\n22151 772959;x"), [
    { linea: 1, partida: "E21BE708153" },
    { linea: 2, partida: "MG 1234" },
    { linea: 3, partida: "22151 772959" },
  ]);
});

test("la coma no separa: la notación científica llega entera para avisarla", () => {
  assert.deepEqual(splitPartidasMasivasLine("2,1E+11"), ["2,1E+11"]);
  assert.equal(looksLikeScientificNotation("2,1E+11"), true);
  assert.equal(looksLikeScientificNotation("2.10000708E+11"), true);
  assert.equal(looksLikeScientificNotation("E21BE708153"), false);
  assert.equal(looksLikeScientificNotation("21E3"), false);
});

test("una planilla con la partida en otra columna la toma por el encabezado", () => {
  const cells = [
    ["Artículo", "Descripción", "Partida"],
    ["1105001", "Auto CPAP RESmart", "E238D501001"],
    ["", "", ""],
    ["1105001", "Auto CPAP RESmart", "E238D501002"],
    ["Total", "2", ""],
  ];
  assert.equal(partidasMasivasTextFromCells(cells), "E238D501001\nE238D501002");
});

test("el texto limpia espacios duros de Excel", () => {
  assert.deepEqual(parsePartidasMasivasText(" E1 "), [{ linea: 1, partida: "E1" }]);
});

test("quita los renglones indicados sin tocar los demás", () => {
  assert.equal(removePartidasMasivasLines("A1\nA2\nA3", [2]), "A1\nA3");
});

test("el payload manda el renglón como número de fila", () => {
  assert.deepEqual(partidasMasivasPayloadRows([{ linea: 4, partida: "A" }]), [{ fila: 4, partida: "A" }]);
});

test("cargadas: mismo artículo y depósito, marcando las del propio renglón", () => {
  const items = [
    { articleCode: "1105001", partidas: [{ partida: "A1", stockDepositCode: "SAR" }, { partida: "" }] },
    { articleCode: "1111001", partidas: [{ partida: "H1", stockDepositCode: "SAR" }] },
    { articleCode: "1105001", partidas: [{ partida: "A2", stockDepositCode: "SAR" }, { partida: "A3", stockDepositCode: "VAL" }] },
  ];
  assert.deepEqual(partidasMasivasCargadas(items, 2, "SAR"), [
    { partida: "A1", renglon: 1, actual: false },
    { partida: "A2", renglon: 3, actual: true },
  ]);
  assert.deepEqual(partidasMasivasCargadas([{ articleCode: "" }], 0, "SAR"), []);
});

test("cargar agrega una unidad por partida, descarta la fila vacía y no repite", () => {
  const item = {
    quantity: "1",
    articleCode: "1105001",
    partidas: [
      { partida: "A1", assignedQuantity: "1", stockDepositCode: "SAR" },
      { partida: "", assignedQuantity: "1", stockDepositCode: "SAR" },
    ],
  };
  const filas = [
    okRow(1, "A2"),
    { ...okRow(2, "A1"), accion: "ya_cargada" },
    { fila: 3, partida: "X", estado: "error", accion: "", errores: ["No tiene stock en SAR."], avisos: [], lote: null },
    okRow(4, "a2"),
  ];
  const next = applyPartidasMasivas(item, filas);
  assert.equal(next.quantity, "1");
  assert.deepEqual(
    next.partidas.map((row) => [row.partida, row.assignedQuantity, row.stockDepositCode, row.stockAvailableQuantity]),
    [
      ["A1", "1", "SAR", undefined],
      ["A2", "1", "SAR", "1"],
    ]
  );
  assert.equal(next.partidas[1].stockCheckedAt, "2026-09-11T10:00:00");
});

test("con ajustar cantidad el renglón pasa a pedir la suma de sus partidas", () => {
  const item = { quantity: "1", partidas: [{ partida: "" }] };
  const next = applyPartidasMasivas(item, [okRow(1, "A1"), okRow(2, "A2"), okRow(3, "A3")], { adjustQuantity: true });
  assert.equal(next.quantity, "3");
  assert.equal(next.partidas.length, 3);
});

test("sin nada para cargar el renglón queda igual", () => {
  const item = { quantity: "2", partidas: [{ partida: "" }] };
  assert.equal(applyPartidasMasivas(item, [], { adjustQuantity: true }), item);
});

test("el resultado adelanta cuántas se agregan y cuánto suma el renglón", () => {
  const item = { quantity: "40", partidas: [{ partida: "A1", assignedQuantity: "1" }] };
  assert.deepEqual(partidasMasivasResultado(item, [okRow(1, "A1"), okRow(2, "A2"), okRow(3, "A3")]), {
    added: 2,
    assigned: 3,
  });
});
