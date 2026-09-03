import test from "node:test";
import assert from "node:assert/strict";
import { techSidebarSections, usesTechMenu } from "./sidebar-menu.js";

const tecnico = { rol: "tecnico" };
const pathsOf = (user) =>
  techSidebarSections(user).flatMap((section) => section.items.map((item) => item.to));

test("el menú reducido es sólo para el rol técnico", () => {
  assert.equal(usesTechMenu(tecnico), true);
  assert.equal(usesTechMenu({ rol: "TECNICO" }), true);
  // Jefe y jefe_veedor pueden actuar como técnicos, pero supervisan: menú completo.
  assert.equal(usesTechMenu({ rol: "jefe" }), false);
  assert.equal(usesTechMenu({ rol: "jefe_veedor" }), false);
  assert.equal(usesTechMenu({ rol: "recepcion" }), false);
  assert.equal(usesTechMenu(null), false);
});

test("el técnico ve sus tres colas y las dos herramientas de taller", () => {
  assert.deepEqual(pathsOf(tecnico), [
    "/tecnico",
    "/pendientes",
    "/pendientes-reparacion",
    "/catalogo/repuestos",
    "/buscar-ns",
  ]);
});

test("las pantallas de supervisión, administración y logística quedan fuera del menú", () => {
  const paths = new Set(pathsOf(tecnico));
  for (const oculta of [
    "/pendientes-por-tecnico",
    "/listos-para-liberar",
    "/listos",
    "/derivados",
    "/depositos",
    "/clientes",
    "/ingresos/historico",
    "/equipos",
    "/buscar-accesorio",
  ]) {
    assert.equal(paths.has(oculta), false, `${oculta} no debería estar en el menú del técnico`);
  }
});

test("un ítem sin permiso no aparece, y la sección vacía tampoco", () => {
  // Técnico al que le sacaron repuestos y los buscadores: la sección Taller entera se cae.
  const sinTaller = { rol: "tecnico", permissions: { "page.spare_parts": false, "page.home_search": false } };
  const secciones = techSidebarSections(sinTaller);
  assert.deepEqual(secciones.map((s) => s.title), ["Servicio técnico"]);
});

test("un técnico con permisos personalizados no usa el menú reducido", () => {
  const conPresupuesto = {
    rol: "tecnico",
    permissions: { "page.budget_queues": true },
  };
  assert.equal(usesTechMenu(conPresupuesto), false);

  const conOverrides = {
    rol: "tecnico",
    permisos_personalizados: 2,
  };
  assert.equal(usesTechMenu(conOverrides), false);

  const conHasCustom = {
    rol: "tecnico",
    has_custom_permissions: true,
  };
  assert.equal(usesTechMenu(conHasCustom), false);
});

test("quitarle un permiso a un técnico no le devuelve el menú completo", () => {
  // Un `deny` le saca una pantalla: si eso contara como "personalizado", sacarle
  // permisos le agrandaba el menú de 5 a 14 ítems.
  const conDeny = {
    rol: "tecnico",
    custom_permissions: { "page.spare_parts": "deny" },
    has_custom_permissions: true,
    permisos_personalizados: 1,
    permissions: { "page.spare_parts": false },
  };
  assert.equal(usesTechMenu(conDeny), true);
  assert.equal(
    pathsOf(conDeny).includes("/catalogo/repuestos"),
    false,
    "el permiso denegado sigue sin aparecer",
  );

  // Un `allow` sobre algo que el rol no trae sí amplía: ese es el caso que el
  // menú reducido no puede representar.
  const conAllow = {
    rol: "tecnico",
    custom_permissions: { "page.budget_queues": "allow" },
    has_custom_permissions: true,
    permisos_personalizados: 1,
    permissions: { "page.budget_queues": true },
  };
  assert.equal(usesTechMenu(conAllow), false);

  // Un `allow` redundante sobre un permiso que el rol ya tiene no cambia nada.
  const conAllowRedundante = {
    rol: "tecnico",
    custom_permissions: { "page.spare_parts": "allow" },
    has_custom_permissions: true,
    permisos_personalizados: 1,
  };
  assert.equal(usesTechMenu(conAllowRedundante), true);
});

test("techSidebarSections incluye presupuestos si el usuario tiene el permiso", () => {
  const conPresupuesto = {
    rol: "tecnico",
    permissions: { "page.budget_queues": true },
  };
  const paths = pathsOf(conPresupuesto);
  assert.equal(paths.includes("/pendientes-presupuesto"), true);
  assert.equal(paths.includes("/presupuestados"), true);
});
