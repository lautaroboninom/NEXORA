import test from "node:test";
import assert from "node:assert/strict";
import {
  equipmentIdentityKey,
  findDuplicateEquipmentIndex,
  findStagedEquipmentIndex,
} from "./ingreso-equipos.js";

const equipo = ({ serial = "", interno = "", tipo = "", id = "x" } = {}) => ({
  id,
  payload: { equipo: { numero_serie: serial, numero_interno: interno } },
  editor: { tipoSel: tipo },
});

test("el CPAP y su humidificador comparten serie pero no son el mismo equipo", () => {
  const cpap = equipo({ id: "1", serial: "y662125150133", tipo: "CPAP" });
  const humidificador = equipo({ id: "2", serial: "y662125150133", tipo: "Calentador humidificador" });

  assert.notEqual(equipmentIdentityKey(cpap), equipmentIdentityKey(humidificador));
  assert.equal(findDuplicateEquipmentIndex([cpap], humidificador), -1);
});

test("el mismo equipo cargado dos veces sí se detecta, aunque cambie el formato de la serie", () => {
  const primero = equipo({ id: "1", serial: "y662125150133", tipo: "CPAP" });
  const repetido = equipo({ id: "2", serial: " Y6621-2515 0133 ", tipo: "cpap" });

  assert.equal(findDuplicateEquipmentIndex([primero], repetido), 0);
});

test("editar un equipo ya cargado no choca contra sí mismo", () => {
  const cargado = equipo({ id: "1", serial: "SN-1", tipo: "CPAP" });
  const editado = equipo({ id: "1", serial: "SN-1", tipo: "CPAP" });

  assert.equal(findDuplicateEquipmentIndex([cargado], editado), -1);
});

test("sin serie, la identidad usa el número interno", () => {
  const mg = equipo({ id: "1", interno: "MG 0123", tipo: "Concentrador" });
  const mismoMg = equipo({ id: "2", interno: "mg0123", tipo: "Concentrador" });
  const otroTipo = equipo({ id: "3", interno: "MG 0123", tipo: "Humidificador" });

  assert.equal(findDuplicateEquipmentIndex([mg], mismoMg), 0);
  assert.equal(findDuplicateEquipmentIndex([mg], otroTipo), -1);
});

test("un equipo sin identificador no bloquea la carga de otros", () => {
  const sinDatos = equipo({ id: "1", tipo: "CPAP" });
  const otroSinDatos = equipo({ id: "2", tipo: "CPAP" });

  assert.equal(equipmentIdentityKey(sinDatos), "");
  assert.equal(findDuplicateEquipmentIndex([sinDatos], otroSinDatos), -1);
});

test("el índice devuelto sirve para nombrar el equipo que choca", () => {
  const lista = [
    equipo({ id: "1", serial: "SN-A", tipo: "CPAP" }),
    equipo({ id: "2", serial: "SN-B", tipo: "CPAP" }),
    equipo({ id: "3", serial: "SN-C", tipo: "CPAP" }),
  ];

  assert.equal(findDuplicateEquipmentIndex(lista, equipo({ id: "4", serial: "SN-C", tipo: "CPAP" })), 2);
});

test("el tipo del payload manda sobre el del selector", () => {
  // El backend estampa en el equipo el tipo del modelo elegido, no el del selector: si el
  // operador dejó el selector en CPAP pero eligió un modelo de humidificador, la identidad
  // que vale es la del modelo.
  const item = {
    id: "1",
    payload: { equipo: { numero_serie: "SN-1", tipo_equipo: "Calentador humidificador" } },
    editor: { tipoSel: "CPAP" },
  };
  const cpap = equipo({ id: "2", serial: "SN-1", tipo: "CPAP" });

  assert.equal(findDuplicateEquipmentIndex([item], cpap), -1);
});

test("el humidificador ve al CPAP que ya está en la lista, pero no al revés que a sí mismo", () => {
  const cpap = equipo({ id: "1", serial: "SN-9", tipo: "CPAP" });
  const lista = [cpap];

  assert.equal(findStagedEquipmentIndex(lista, { numero_serie: "sn9", tipo_equipo: "cpap" }), 0);
  assert.equal(findStagedEquipmentIndex(lista, { numero_serie: "SN-9", tipo_equipo: "Calentador humidificador" }), -1);
  assert.equal(findStagedEquipmentIndex(lista, { numero_serie: "SN-9", tipo_equipo: "CPAP" }, { skipItemId: "1" }), -1);
});
