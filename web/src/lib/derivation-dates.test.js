import test from "node:test";
import assert from "node:assert/strict";
import { formatDateOnly, localDateInputValue } from "./ui-helpers.js";

test("la fecha civil de derivación conserva el día en Argentina", () => {
  const original = process.env.TZ;
  process.env.TZ = "America/Argentina/Buenos_Aires";
  try {
    assert.equal(formatDateOnly("2026-09-12"), "12/9/26");
    assert.equal(localDateInputValue(new Date("2026-09-12T02:30:00Z")), "2026-09-11");
    assert.equal(localDateInputValue(new Date("2026-09-12T03:30:00Z")), "2026-09-12");
  } finally {
    if (original === undefined) delete process.env.TZ;
    else process.env.TZ = original;
  }
});
