-- ============================================================================
-- Bejerman / SAMTRONIC - base SBDASEP (SEPID)
--
-- Destraba los ENT que el importador rechaza con
--   "El comprobante no se importo. No ha sido posible actualizar los datos en
--    la tabla."
--
-- CAUSA RAIZ (27/08/2026, confirmada contra el plan cache del SQL Server):
-- el importador de Bejerman actualiza Stock y StockPar con **concurrencia
-- optimista**: repite todos los valores originales en el WHERE, incluido el
-- sello de modificacion. La sentencia real, tal como quedo cacheada:
--
--   update Stock SET stk_CantUM1=@P1, ..., stk_FecMod=@P5, stkusu_Codigo=@P6
--    WHERE "stkdep_Cod"=@P7 AND "stkart_CodGen"=@P14 AND "stk_CantUM1"=@P10
--      AND "stk_FecMod"=@P16 AND "stkusu_Codigo"=@P13 AND ...
--
-- Su copia del FecMod viene truncada al segundo. Si la fila guardada tiene
-- milisegundos (la escribio otro camino, no el importador), el WHERE matchea
-- CERO filas y el comprobante se rechaza. Es permanente: nada mas reescribe
-- esa fila, asi que ese (articulo, deposito) queda bloqueado para siempre.
--
-- Comprobacion de solo lectura que lo demuestra: reproduciendo ese WHERE con
-- el FecMod truncado, las 7 filas trabadas dan 0 coincidencias y las que
-- funcionan dan 1.
--
-- ALCANCE MEDIDO: 8 filas en Stock y 49 en StockPar, todas de SBDASEP
-- (SBDAMGBI esta limpia). 7 de las 8 de Stock son los (articulo, STC) de los
-- jobs trabados; la otra es 1110003/SAR. Todas vienen del mismo lote de RSS
-- 00004761-00004767 importado el 06/07/2026 11:04:32 por SPADM6, mas una del
-- 29/07 por GERENCIA.
--
-- QUE HACE: deja el sello de modificacion en .000. No toca cantidades, ni
-- claves, ni genera movimientos. Ninguna fila se corre mas de un segundo.
--
-- Backup previo (correr una sola vez, es idempotente):
--   dbo.NEXORA_BKP_20260827_FECMOD_MS_Stock
--   dbo.NEXORA_BKP_20260827_FECMOD_MS_StockPar
-- ============================================================================

SET NOCOUNT ON;

-- --------------------------------------------------------------------------
-- Backup (idempotente)
-- --------------------------------------------------------------------------
IF OBJECT_ID('dbo.NEXORA_BKP_20260827_FECMOD_MS_Stock') IS NULL
  SELECT * INTO dbo.NEXORA_BKP_20260827_FECMOD_MS_Stock
    FROM dbo.Stock
   WHERE DATEPART(MILLISECOND, stk_FecMod) <> 0;

IF OBJECT_ID('dbo.NEXORA_BKP_20260827_FECMOD_MS_StockPar') IS NULL
  SELECT * INTO dbo.NEXORA_BKP_20260827_FECMOD_MS_StockPar
    FROM dbo.StockPar
   WHERE DATEPART(MILLISECOND, stp_FecMod) <> 0;

-- --------------------------------------------------------------------------
-- Correccion, en una sola transaccion.
--
-- El alcance medido el 27/08 fue 8 y 49, pero se mueve solo: una fila deja de
-- tener milisegundos apenas alguien la toca desde la pantalla de Bejerman (esa
-- si reescribe la fila entera). Por eso el guard es un techo y no un numero
-- exacto: aborta si aparecen muchas mas de las medidas, que seria senal de que
-- algo nuevo esta escribiendo milisegundos y hay que mirarlo antes de tocar.
-- --------------------------------------------------------------------------
SET XACT_ABORT ON;
BEGIN TRANSACTION;

UPDATE dbo.Stock
   SET stk_FecMod = CONVERT(datetime, CONVERT(varchar(19), stk_FecMod, 121))
 WHERE DATEPART(MILLISECOND, stk_FecMod) <> 0;
DECLARE @stock INT = @@ROWCOUNT;

UPDATE dbo.StockPar
   SET stp_FecMod = CONVERT(datetime, CONVERT(varchar(19), stp_FecMod, 121))
 WHERE DATEPART(MILLISECOND, stp_FecMod) <> 0;
DECLARE @par INT = @@ROWCOUNT;

IF @stock > 20 OR @par > 100
BEGIN
  ROLLBACK TRANSACTION;
  SELECT 'ABORTADO - alcance inesperado, revisar antes de aplicar' AS resultado,
         @stock AS stock_tocadas, @par AS stockpar_tocadas;
END
ELSE
BEGIN
  COMMIT TRANSACTION;
  SELECT 'APLICADO' AS resultado, @stock AS stock_tocadas, @par AS stockpar_tocadas;
END

-- --------------------------------------------------------------------------
-- Verificacion: las dos tienen que dar 0.
-- --------------------------------------------------------------------------
SELECT 'Stock' AS tabla, COUNT(*) AS con_milisegundos
  FROM dbo.Stock WHERE DATEPART(MILLISECOND, stk_FecMod) <> 0
UNION ALL
SELECT 'StockPar', COUNT(*)
  FROM dbo.StockPar WHERE DATEPART(MILLISECOND, stp_FecMod) <> 0;

-- --------------------------------------------------------------------------
-- Rollback, si hiciera falta:
--
-- UPDATE s SET s.stk_FecMod = b.stk_FecMod
--   FROM dbo.Stock s
--   JOIN dbo.NEXORA_BKP_20260827_FECMOD_MS_Stock b
--     ON b.stkart_CodGen = s.stkart_CodGen AND b.stkart_CodEle1 = s.stkart_CodEle1
--    AND b.stkart_CodEle2 = s.stkart_CodEle2 AND b.stkart_CodEle3 = s.stkart_CodEle3
--    AND b.stkdep_Cod = s.stkdep_Cod;
--
-- UPDATE p SET p.stp_FecMod = b.stp_FecMod
--   FROM dbo.StockPar p
--   JOIN dbo.NEXORA_BKP_20260827_FECMOD_MS_StockPar b
--     ON b.stpart_CodGen = p.stpart_CodGen AND b.stpart_CodEle1 = p.stpart_CodEle1
--    AND b.stpart_CodEle2 = p.stpart_CodEle2 AND b.stpart_CodEle3 = p.stpart_CodEle3
--    AND b.stpdep_Cod = p.stpdep_Cod AND b.stp_Partida = p.stp_Partida;
-- --------------------------------------------------------------------------
