import test from "node:test";
import assert from "node:assert/strict";
import { hoursTextFromMinutes, minutesFromHoursText } from "./labor-hours.js";

test("admite horas con coma o punto y convierte a minutos enteros", () => {
  for (const [text, minutes] of [["0", 0], ["0,5", 30], ["1,25", 75], ["1,5", 90],
    ["2.75", 165], [",5", 30], ["1,", 60], [" 1,5 ", 90], ["1,3333", 80], ["8760", 525600]]) {
    assert.equal(minutesFromHoursText(text), minutes, text);
  }
});

test("rechaza valores incompletos o inválidos sin reutilizar el tiempo anterior", () => {
  for (const text of ["", " ", ",", "-1", "1,2,3", "1,2.3", "abc", "1e3", "Infinity", "NaN", "8760,01", "0,001", null]) {
    assert.equal(minutesFromHoursText(text), null, String(text));
  }
});

test("abrir y guardar las horas de registros anteriores conserva sus minutos", () => {
  for (let minutes = 0; minutes <= 1440; minutes += 1) {
    assert.equal(minutesFromHoursText(hoursTextFromMinutes(minutes)), minutes);
  }
  assert.equal(hoursTextFromMinutes(90), "1,5");
  assert.equal(minutesFromHoursText(hoursTextFromMinutes(525600)), 525600);
});
