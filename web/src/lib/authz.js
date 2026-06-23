// web/src/lib/authz.js
// Helpers de autorizacion (RBAC) centralizados.

export const ROLES = {
  JEFE: "jefe",
  JEFE_VEEDOR: "jefe_veedor",
  ADMIN: "admin",
  VENTAS: "ventas",
  RECEPCION: "recepcion",
  COBRANZAS: "cobranzas",
  TECNICO: "tecnico",
};

export const normalizeRole = (r) => (r ?? "").toString().trim().toLowerCase();
export const hasAnyRole = (user, roles) => roles.includes(normalizeRole(user?.rol));

// Jefe/Jefe_veedor pueden actuar como tecnico.
export const canActAsTech = (user) =>
  [ROLES.TECNICO, ROLES.JEFE, ROLES.JEFE_VEEDOR].includes(normalizeRole(user?.rol));

// Quienes pueden liberar (imprimir orden de salida).
export const canRelease = (user) =>
  [ROLES.JEFE, ROLES.JEFE_VEEDOR, ROLES.ADMIN, ROLES.VENTAS].includes(normalizeRole(user?.rol));

// Helpers por rol para uso en otras pantallas.
export const isJefe = (u) => normalizeRole(u?.rol) === ROLES.JEFE;
export const isJefeVeedor = (u) => normalizeRole(u?.rol) === ROLES.JEFE_VEEDOR;
export const isAdmin = (u) => normalizeRole(u?.rol) === ROLES.ADMIN;
export const isVentas = (u) => normalizeRole(u?.rol) === ROLES.VENTAS;
export const isRecepcion = (u) => normalizeRole(u?.rol) === ROLES.RECEPCION;
export const isCobranzas = (u) => normalizeRole(u?.rol) === ROLES.COBRANZAS;
export const isTecnico = (u) => normalizeRole(u?.rol) === ROLES.TECNICO;
