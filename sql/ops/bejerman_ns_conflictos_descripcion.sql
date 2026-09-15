-- Auditoría de sólo lectura de la fuente de N/S por cliente.
-- Ejecutar en SBDASEP o SBDAMGBI con scripts/samtronic_sql.py --file.
-- Detecta un saldo positivo y otro negativo para el mismo cliente, artículo
-- y partida, separados exclusivamente por la descripción del renglón.
-- No determina quién introdujo la descripción ni autoriza una corrección.
-- Describe las filas que existen al ejecutarse: no reconstruye comprobantes
-- borrados ni una salida histórica de la consulta específica del ERP.
-- Conserva la descripción sin normalizar para respetar la agrupación SQL.

WITH grupos AS (
    SELECT c.scvcli_Cod AS cliente,
           d.sdvart_CodGen AS articulo,
           d.sdvstp_Partida AS partida,
           d.sdv_Desc AS descripcion,
           SUM(d.sdv_CantUM1) AS saldo_por_descripcion,
           COUNT(*) AS movimientos
      FROM dbo.SegCabV c
      JOIN dbo.SegDetV d
        ON d.sdvemp_Codigo = c.scvemp_Codigo
       AND d.sdvsuc_Cod = c.scvsuc_Cod
       AND d.sdvscv_ID = c.scv_ID
     WHERE c.scvtop_Cod = 'ALQ'
       AND d.sdv_TipoIt = 'A'
       AND LTRIM(RTRIM(ISNULL(d.sdvstp_Partida, ''))) <> ''
     GROUP BY c.scvcli_Cod, d.sdvart_CodGen, d.sdvstp_Partida, d.sdv_Desc
), conflictos AS (
    SELECT cliente, articulo, partida,
           SUM(saldo_por_descripcion) AS saldo_sin_descripcion
      FROM grupos
     GROUP BY cliente, articulo, partida
    HAVING COUNT(*) > 1
       AND MAX(saldo_por_descripcion) > 0
       AND MIN(saldo_por_descripcion) < 0
)
SELECT g.*, c.saldo_sin_descripcion
  FROM grupos g
  JOIN conflictos c
    ON c.cliente = g.cliente
   AND c.articulo = g.articulo
   AND c.partida = g.partida
 ORDER BY g.cliente, g.articulo, g.partida, g.descripcion;
