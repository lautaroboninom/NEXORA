-- ============================================================================
-- Bajas de alquiler que nunca se registraron  (Bejerman, SQL Server)
-- ----------------------------------------------------------------------------
-- QUE DETECTA: equipos que la consulta de cobranzas muestra en poder del
-- cliente Y QUE AL MISMO TIEMPO estan en stock en un deposito nuestro. Es una
-- contradiccion fisica: el equipo volvio, pero la baja de alquiler no se
-- registro nunca, asi que se lo sigue facturando al cliente.
--
-- Caso testigo (03/09/2026): N/S MZJ5S121748, Longfian JAY-5, SIMPLE SALUD.
-- Stock 1 en STL desde que volvio, y la consulta lo mostraba en 1. Se corrigio
-- cargando el RDA 00004-00005049 a mano en Bejerman.
--
-- LA CONSULTA DE COBRANZAS (reproducida y validada contra las corridas del ERP
-- guardadas en dbo.consEspec7633 / consEspec1981):
--
--     origen : SegCabV + SegDetV        <- renglones de comprobante, NO MovStock
--     filtro : SegCabV.scvtop_Cod = 'ALQ'      (TipoOper: ALQ = Alquiler)
--     cantidad = SUM(SegDetV.sdv_CantUM1)
--     agrupado por cliente + articulo + descripcion del renglon + partida
--
--   Signo del renglon: entrega al cliente = +1, devolucion = -1. Es el signo
--   OPUESTO al de MovStock, donde una salida va en -1.
--
--   El tipo de comprobante NO importa: la consulta no lo mira. Lo que decide si
--   un equipo aparece es el TIPO DE OPERACION.
--
-- POR QUE PASA: cuando el equipo vuelve, alguien repone el stock con un ENT o
-- una TRA -- movimientos del circuito S, que NO llevan cliente-- en lugar de
-- emitir el RDA. El stock queda bien y la cuenta del cliente queda mal, en
-- silencio. Medicion del 03/09/2026: 124 equipos (119 SEPID + 5 MGBIO,
-- 33 clientes); 99 volvieron con ENT y 15 con TRA.
--
-- Correr en las dos empresas:
--   python scripts/samtronic_sql.py --db SBDASEP  --file sql/ops/bejerman_alquiler_por_cliente.sql
--   python scripts/samtronic_sql.py --db SBDAMGBI --file sql/ops/bejerman_alquiler_por_cliente.sql
-- ============================================================================

WITH det AS (
  SELECT LTRIM(RTRIM(c.scvcli_Cod)) AS cliente, LTRIM(RTRIM(d.sdvstp_Partida)) AS partida,
         LTRIM(RTRIM(d.sdvart_CodGen)) AS articulo, d.sdv_CantUM1 AS cant,
         t.spvtco_Cod AS tipo, t.spv_CodPvt+'-'+t.spv_Nro AS comp, c.scv_FEmision AS fecha, c.scv_ID,
         LTRIM(RTRIM(ISNULL(c.scvtop_Cod,'(vacio)'))) AS oper
    FROM dbo.SegCabV c
    JOIN dbo.SegTiposV t ON t.spvemp_Codigo=c.scvemp_Codigo AND t.spvsuc_Cod=c.scvsuc_Cod AND t.spvscv_ID=c.scv_ID
    JOIN dbo.SegDetV  d ON d.sdvemp_Codigo=c.scvemp_Codigo AND d.sdvsuc_Cod=c.scvsuc_Cod AND d.sdvscv_ID=c.scv_ID
   WHERE d.sdv_TipoIt='A' AND LTRIM(RTRIM(ISNULL(d.sdvstp_Partida,''))) <> ''
     AND LTRIM(RTRIM(ISNULL(c.scvcli_Cod,''))) <> ''
), saldo AS (
  SELECT cliente, articulo, partida,
         SUM(CASE WHEN oper='ALQ' THEN cant ELSE 0 END) AS saldo_alq
    FROM det GROUP BY cliente, articulo, partida
), stk AS (
  SELECT LTRIM(RTRIM(stp_Partida)) AS partida, SUM(stp_CantUM1) AS en_casa
    FROM dbo.StockPar WHERE stpdep_Cod IN ('STL','STR','STC','VAL','SAR','MUE','EJE')
   GROUP BY LTRIM(RTRIM(stp_Partida))
), ult_alq AS (
  SELECT partida, cliente, tipo, comp, fecha,
         ROW_NUMBER() OVER (PARTITION BY partida, cliente ORDER BY fecha DESC, scv_ID DESC) rn
    FROM det WHERE oper='ALQ'
), vuelta AS (   -- ultimo movimiento de stock que lo devolvio a un deposito nuestro
  SELECT LTRIM(RTRIM(m.mststp_Partida)) AS partida, ms.cms_FComp AS fecha,
         ms.cmstco_Cod AS tipo, ms.cms_CodPvt+'-'+ms.cms_Nro AS comp, m.mstdep_Cod AS dep,
         ROW_NUMBER() OVER (PARTITION BY LTRIM(RTRIM(m.mststp_Partida)) ORDER BY ms.cms_FComp DESC, m.mst_ID DESC) rn
    FROM dbo.MovStock m
    JOIN dbo.CabMovS ms ON ms.cmsemp_Codigo=m.mstemp_Codigo AND ms.cmssuc_Cod=m.mstsuc_Cod AND ms.cms_ID=m.mstcms_ID
   WHERE m.mst_CantUM1 > 0 AND m.mstdep_Cod IN ('STL','STR','STC','VAL','SAR')
)
SELECT s.cliente AS cliente_cod, LTRIM(RTRIM(ISNULL(cl.cli_RazSoc,''))) AS cliente,
       s.partida AS numero_serie, LTRIM(RTRIM(ISNULL(a.art_DescGen,''))) AS equipo,
       s.saldo_alq AS muestra_la_consulta, k.en_casa AS stock_nuestro,
       u.tipo AS ultimo_alq_tipo, u.comp AS ultimo_alq_comp, CONVERT(char(10),u.fecha,103) AS ultimo_alq_fecha,
       v.tipo AS volvio_con, v.comp AS volvio_comp, CONVERT(char(10),v.fecha,103) AS volvio_fecha, v.dep AS volvio_a
  FROM saldo s
  JOIN stk k ON k.partida = s.partida
  LEFT JOIN ult_alq u ON u.partida=s.partida AND u.cliente=s.cliente AND u.rn=1
  LEFT JOIN vuelta  v ON v.partida=s.partida AND v.rn=1
  LEFT JOIN dbo.Clientes  cl ON LTRIM(RTRIM(cl.cli_Cod))   = s.cliente
  LEFT JOIN dbo.Articulos a  ON LTRIM(RTRIM(a.art_CodGen)) = s.articulo
 WHERE s.saldo_alq > 0 AND k.en_casa > 0
 ORDER BY cliente, numero_serie
