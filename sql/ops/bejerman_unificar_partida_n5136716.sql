-- ============================================================================
-- Bejerman / SAMTRONIC - base SBDASEP (SEPID)
--
-- Unifica la capitalizacion de la partida N5136716 (AirSep NewLife Elite,
-- articulo 1115007, OS 29237).
--
-- El backfill masivo de STC del 08/06/2026 escribio la fila de ese deposito en
-- minuscula (`n5136716`) mientras que la de STR quedo en mayuscula. SQL Server
-- no distingue mayusculas, asi que las dos filas conviven sin chocar (difieren
-- en el deposito) y el stock queda duplicado; pero **el SDK si distingue**:
-- consultando `ObtenerStockPartida` con 'N5136716' la fila de STC no aparece,
-- asi que NEXORA nunca la ve y el circuito no puede corregirla solo.
--
-- Preflight (27/08/2026): 1 fila en StockPar, 1 en MovStock, 0 en SegDetV
-- (ninguna historia comercial usa esa grafia) y 0 colisiones con la mayuscula.
--
-- Despues de esto hay que descontar el sobrante: la OS 29237 esta liberada, o
-- sea que el equipo no esta en el taller, asi que sobra el +1 de STR. Esa parte
-- se emite por el SDK como una SAL normal, no por SQL.
-- ============================================================================

SET NOCOUNT ON;

IF OBJECT_ID('dbo.NEXORA_BKP_20260827_N5136716_StockPar') IS NULL
  SELECT * INTO dbo.NEXORA_BKP_20260827_N5136716_StockPar
    FROM dbo.StockPar
   WHERE stp_Partida COLLATE Latin1_General_BIN = 'n5136716';

IF OBJECT_ID('dbo.NEXORA_BKP_20260827_N5136716_MovStock') IS NULL
  SELECT * INTO dbo.NEXORA_BKP_20260827_N5136716_MovStock
    FROM dbo.MovStock
   WHERE mststp_Partida COLLATE Latin1_General_BIN = 'n5136716';

SET XACT_ABORT ON;
BEGIN TRANSACTION;

UPDATE dbo.StockPar
   SET stp_Partida = 'N5136716'
 WHERE stp_Partida COLLATE Latin1_General_BIN = 'n5136716'
   AND NOT EXISTS (
       SELECT 1 FROM dbo.StockPar b
        WHERE b.stp_Partida COLLATE Latin1_General_BIN = 'N5136716'
          AND b.stpart_CodGen = dbo.StockPar.stpart_CodGen
          AND b.stpart_CodEle1 = dbo.StockPar.stpart_CodEle1
          AND b.stpart_CodEle2 = dbo.StockPar.stpart_CodEle2
          AND b.stpart_CodEle3 = dbo.StockPar.stpart_CodEle3
          AND b.stpdep_Cod = dbo.StockPar.stpdep_Cod
   );
DECLARE @par INT = @@ROWCOUNT;

UPDATE dbo.MovStock
   SET mststp_Partida = 'N5136716'
 WHERE mststp_Partida COLLATE Latin1_General_BIN = 'n5136716';
DECLARE @mov INT = @@ROWCOUNT;

IF @par <> 1 OR @mov <> 1
BEGIN
  ROLLBACK TRANSACTION;
  SELECT 'ABORTADO - alcance inesperado' AS resultado,
         @par AS stockpar_tocadas, @mov AS movstock_tocadas;
END
ELSE
BEGIN
  COMMIT TRANSACTION;
  SELECT 'APLICADO' AS resultado, @par AS stockpar_tocadas, @mov AS movstock_tocadas;
END

-- Verificacion: no debe quedar ninguna fila en minuscula.
SELECT 'StockPar minuscula' AS que, COUNT(*) AS filas
  FROM dbo.StockPar WHERE stp_Partida COLLATE Latin1_General_BIN = 'n5136716'
UNION ALL
SELECT 'MovStock minuscula', COUNT(*)
  FROM dbo.MovStock WHERE mststp_Partida COLLATE Latin1_General_BIN = 'n5136716';
