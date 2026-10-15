-- Pedido del usuario (14/10/2026): en Pendientes de presupuesto, "todos los concentradores de
-- oxígeno Longfian que sean propiedad de MG BIO poneles presupuesto no aplica".
--
-- Propiedad de MG BIO = el mismo criterio que usa NEXORA (`resolve_mg_flags`): código MG/NM/NV/CE
-- en número interno, serie o N° de control, y sin venta registrada (`mg_estado <> inactivo_venta`).
-- Ninguno de la cola tiene como cliente a MGBIO: el cliente es a quien se le alquilaba.
--
-- Medido sobre el backup del 13/10 22:30 (servicio_tecnico-20261014-013019.dump): 64 Longfian en la
-- cola, 30 con código MG. Quedan 24, todos "baja alquiler" que volvieron con RDA/RDN según Bejerman.
--
-- Fuera a propósito:
--   * Vendidos según el HISTÓRICO DE BAJAS (Z:\MG BIO\MADRES), aunque NEXORA los tenga activos:
--     29056 (MG 4715, venta 12/08/2021), 29161 (MG 4785, 28/05/2024), 29194 (MG 4497, 27/06/2024),
--     29568 (MG 6789, 15/05/2024). Son del cliente: el presupuesto se le cobra.
--   * Ya marcados como vendidos en NEXORA: 29782 (MG 6515) y 29808 (MG 4700).
--   * Sin MG en NEXORA y con dudas, para revisar a mano: 29189 (E828, BRAHIM, volvió con RDA 4062:
--     es de la flota de alquiler, pero no se sabe su MG), 29709 (MZJ5S117719, ORTOPEDIA INTEGRAR:
--     el RDA 5220 que cita trae otra partida) y 29795 (serie cargada como "MG MZJ5S121876", SIED,
--     entró con RIS).
--
-- La lista es explícita y además se revalida al correr: la OS tiene que seguir en la cola con
-- presupuesto pendiente, ser Longfian concentrador, tener código MG y no estar vendida. Las que ya
-- no cumplan se saltean y se informan.
--
-- Se escribe `quotes` (el trigger sync_quote_with_ingreso copia la versión vigente a ingresos).
--
-- Uso:     psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_longfian_mg_aplicar_20261014.sql
-- Ensayo:  agregar -v ensayo=1 (termina con ROLLBACK).

BEGIN;

CREATE TEMP TABLE lista(id integer PRIMARY KEY) ON COMMIT DROP;
INSERT INTO lista(id) VALUES
  (29238),(29239),(29240),(29262),(29263),(29265),(29273),(29374),(29376),(29440),(29441),(29487),
  (29538),(29626),(29728),(29736),(29738),(29769),(29797),(29799),(29802),(29812),(29813),(29814);

CREATE TEMP TABLE candidatos ON COMMIT DROP AS
SELECT t.id, d.numero_interno, d.numero_serie, t.motivo::text AS motivo, t.estado::text AS estado
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  JOIN marcas b ON b.id = d.marca_id
  LEFT JOIN models m ON m.id = d.model_id
 WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND t.estado NOT IN ('ingresado','entregado','liberado','no_se_repara',
                        'vendido_pendiente_entrega','vendido_entregado')
   -- La misma excepción de "alquilado" que PendientesPresupuestoView.
   AND (   t.estado <> 'alquilado'
        OR BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
        OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler'))
   AND LOWER(BTRIM(b.nombre)) = 'longfian'
   AND COALESCE(m.tipo_equipo, d.tipo_equipo, '') ILIKE '%concentrador%'
   AND (   BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
        OR BTRIM(COALESCE(d.numero_serie,''))   ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
        OR BTRIM(COALESCE(d.n_de_control,''))   ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$')
   AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta';

CREATE TEMP TABLE objetivo ON COMMIT DROP AS
SELECT c.* FROM candidatos c JOIN lista l ON l.id = c.id;

\echo == De la lista, ya no cumplen (se saltean) ==
SELECT l.id, t.estado::text AS estado, t.presupuesto_estado::text AS presupuesto, d.numero_interno, d.mg_estado
  FROM lista l
  LEFT JOIN ingresos t ON t.id = l.id
  LEFT JOIN devices d ON d.id = t.device_id
 WHERE l.id NOT IN (SELECT id FROM objetivo)
 ORDER BY l.id;

\echo == Longfian con MG en la cola que no están en la lista (no se tocan) ==
SELECT c.id, c.numero_interno, c.motivo, c.estado
  FROM candidatos c
 WHERE c.id NOT IN (SELECT id FROM lista)
 ORDER BY c.id;

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM objetivo;
  RAISE NOTICE 'OS objetivo: % de 24', v;
  IF v = 0 THEN RAISE EXCEPTION 'Ninguna OS de la lista cumple: no se aplica nada'; END IF;
END $$;

\echo == Versión vigente pendiente sin importe -> no_aplica ==
UPDATE quotes q
   SET estado = 'no_aplica'
  FROM (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado, subtotal
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
  ) cur
 WHERE q.id = cur.quote_id
   AND COALESCE(cur.quote_estado, '') IN ('', 'pendiente')
   AND COALESCE(cur.subtotal, 0) = 0
   AND cur.ingreso_id IN (SELECT id FROM objetivo);

\echo == Sin fila en quotes -> versión 1 en no_aplica ==
INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT o.id, 1, 'no_aplica'
  FROM objetivo o
 WHERE NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = o.id);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id IN (SELECT id FROM objetivo)
     AND COALESCE(presupuesto_estado::text, 'pendiente') <> 'no_aplica';
  IF v > 0 THEN RAISE EXCEPTION 'Quedan % OS sin no_aplica (¿borrador con importe?): no se aplica nada', v; END IF;
END $$;

\echo == Resultado ==
SELECT t.id, d.numero_interno, d.numero_serie, t.motivo::text AS motivo, t.presupuesto_estado::text AS presupuesto
  FROM ingresos t JOIN devices d ON d.id = t.device_id
 WHERE t.id IN (SELECT id FROM objetivo)
 ORDER BY t.id;

\if :{?ensayo}
  \echo == ENSAYO: ROLLBACK ==
  ROLLBACK;
\else
  COMMIT;
\endif
