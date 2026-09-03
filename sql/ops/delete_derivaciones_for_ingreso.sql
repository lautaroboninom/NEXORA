-- Uso (Docker Compose dev):
--  docker compose exec -T nexora-postgres \
--    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ingreso_id=28571 -f /docker-entrypoint-initdb.d/../ops/delete_derivaciones_for_ingreso.sql

\set ON_ERROR_STOP on
BEGIN;

-- Mostrar derivaciones actuales para verificación
TABLE (
  SELECT id, proveedor_id, remit_deriv, fecha_deriv, fecha_entrega, estado, comentarios
  FROM equipos_derivados
  WHERE ingreso_id = :ingreso_id
  ORDER BY id
);

-- Borrar derivaciones del ingreso
DELETE FROM equipos_derivados WHERE ingreso_id = :ingreso_id;

-- La derivación no modifica el estado operativo de `ingresos`. Si esta OS aún
-- conserva el estado histórico `derivado`, resolverla mediante la vista previa
-- y el comando `reconcile_legacy_derived_states`; este script no debe adivinarlo.

COMMIT;
