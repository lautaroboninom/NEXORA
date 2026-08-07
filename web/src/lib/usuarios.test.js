import assert from "node:assert/strict";
import test from "node:test";

import {
  bejermanStatus,
  describeUserAgent,
  filterUsers,
} from "./usuarios.js";

const users = [
  {
    id: 1,
    nombre: "Gracia",
    email: "gracia@sepid.com.ar",
    rol: "admin",
    activo: true,
    bejerman_credentials_valid: true,
    bejerman_credentials_username: "S***2",
    bejerman_workstation: "ADMV",
  },
  {
    id: 2,
    nombre: "Técnica",
    email: "tecnica@sepid.com.ar",
    rol: "tecnico",
    activo: false,
    bejerman_credentials_configured: false,
  },
];

test("filterUsers combina búsqueda, rol, estado y Bejerman", () => {
  assert.deepEqual(
    filterUsers(users, { query: "tecnica", role: "tecnico", active: "inactive" }).map(
      (user) => user.id,
    ),
    [2],
  );
  assert.deepEqual(
    filterUsers(users, { bejerman: "valid" }).map((user) => user.id),
    [1],
  );
});

test("bejermanStatus distingue validado, revisar y pendiente", () => {
  assert.equal(bejermanStatus({ bejerman_credentials_valid: true }), "valid");
  assert.equal(
    bejermanStatus({ bejerman_credentials_configured: true }),
    "review",
  );
  assert.equal(bejermanStatus({}), "pending");
});

test("describeUserAgent reconoce navegador y sistema sin exponer la cadena técnica", () => {
  assert.equal(
    describeUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    ),
    "Chrome en Windows",
  );
  assert.equal(
    describeUserAgent(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15",
    ),
    "Safari en macOS",
  );
});
