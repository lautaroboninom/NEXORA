-- ============================================================================
-- Equipos en poder del cliente por alquiler / comodato  (Bejerman, SQL Server)
-- ----------------------------------------------------------------------------
-- Para que le sirva a cobranzas: cantidad, numero de serie y tipo de equipo
-- que cada cliente tiene en su poder.
--
-- POR QUE EXISTE ESTA CONSULTA
-- Hasta febrero de 2026 el alquiler se registraba SIEMPRE con RTN (Remito No
-- facturable) y se cerraba con RDN. Desde marzo de 2026 se empezo a usar el par
-- semanticamente correcto RTA (Remito Alta de Alquiler) / RDA (Remito Baja de
-- Alquiler). Los dos pares conviven: hay entregas RTN que se cerraron con RDA y
-- devoluciones RDA de equipos que despues volvieron a salir con RTN.
-- Cualquier listado que mire un solo par de tipos da mal en las dos direcciones.
-- Esta consulta mira LOS CUATRO tipos como un unico circuito de alquiler.
--
-- COMO LEERLA
--   * Se recorren movimientos de stock (MovStock + CabMovS), NO comprobantes:
--     es lo mismo que hacen los listados del ERP.
--   * mst_CantUM1 es direccion de stock: negativo = sale del deposito (va al
--     cliente), positivo = entra (vuelve). Por eso el saldo del cliente se
--     calcula con el signo invertido.
--   * saldo > 0  => el cliente lo tiene.
--
-- Correr en las DOS empresas: SBDASEP (SEPID) y SBDAMGBI (MG BIO).
--   python scripts/samtronic_sql.py --db SBDASEP --file sql/ops/bejerman_alquiler_por_cliente.sql
-- ============================================================================

WITH mov AS (
    SELECT LTRIM(RTRIM(m.mststp_Partida))            AS partida,
           LTRIM(RTRIM(m.mstart_CodGen))             AS articulo,
           LTRIM(RTRIM(ISNULL(ms.cmscli_Cod, '')))   AS cliente,
           ms.cmstco_Cod                             AS tipo,
           ms.cms_FComp                              AS fecha,
           ms.cms_CodPvt + '-' + ms.cms_Nro          AS comprobante,
           -m.mst_CantUM1                            AS saldo_cliente,
           m.mst_ID                                  AS mov_id
      FROM dbo.MovStock m
      JOIN dbo.CabMovS ms
        ON ms.cmsemp_Codigo = m.mstemp_Codigo
       AND ms.cmssuc_Cod    = m.mstsuc_Cod
       AND ms.cms_ID        = m.mstcms_ID
     -- Los cuatro tipos del circuito de alquiler, viejos y nuevos.
     WHERE ms.cmstco_Cod IN ('RTN', 'RDN', 'RTA', 'RDA')
       AND LTRIM(RTRIM(ISNULL(m.mststp_Partida, ''))) <> ''
       AND LTRIM(RTRIM(ISNULL(ms.cmscli_Cod, '')))    <> ''
),
saldo AS (
    SELECT partida, articulo, cliente, SUM(saldo_cliente) AS saldo
      FROM mov
     GROUP BY partida, articulo, cliente
)
SELECT s.cliente                                        AS cliente_cod,
       LTRIM(RTRIM(ISNULL(c.cli_RazSoc, '')))           AS cliente,
       s.partida                                        AS numero_serie,
       s.articulo                                       AS articulo,
       LTRIM(RTRIM(ISNULL(a.art_DescGen, '')))          AS equipo,
       s.saldo                                          AS cantidad,
       u.fecha                                          AS ultima_fecha,
       u.tipo                                           AS ultimo_tipo,
       u.comprobante                                    AS ultimo_comprobante
  FROM saldo s
  LEFT JOIN dbo.Clientes  c ON LTRIM(RTRIM(c.cli_Cod))    = s.cliente
  LEFT JOIN dbo.Articulos a ON LTRIM(RTRIM(a.art_CodGen)) = s.articulo
  OUTER APPLY (
        SELECT TOP 1 m.fecha, m.tipo, m.comprobante
          FROM mov m
         WHERE m.partida = s.partida
           AND m.cliente = s.cliente
         ORDER BY m.fecha DESC, m.mov_id DESC
  ) u
 WHERE s.saldo > 0
 ORDER BY cliente, equipo, numero_serie;
