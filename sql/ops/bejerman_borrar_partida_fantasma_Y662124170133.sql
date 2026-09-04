-- Elimina la partida fantasma Y662124170133 de SEPID (SBDASEP).
--
-- Origen: al liberar la OS 29648 el 26/08/2026, NEXORA emitio el ENT 00129648 con la
-- serie mal tipeada, creando una partida que no corresponde a ningun equipo real.
-- El SAL de contrapartida nunca se aplico, asi que quedo un +1 suelto en STC.
--
-- Se BORRA en vez de renombrar: la partida correcta (Y662125150133) ya tiene su +1 en
-- STR por el RIS 4988, y renombrar dejaria el humidificador con 2 unidades (STR + STC).
--
-- Alcance medido con el barrido de INFORMATION_SCHEMA (%Partida%/%Serie%) el 02/09/2026:
--   dbo.StockPar.stp_Partida      -> 1 fila
--   dbo.MovStock.mststp_Partida   -> 1 fila
--   dbo.CabMovSImport.csicms_ID   -> 1 fila (NEXORA-OS-29648-ENT-STC)
--   MG BIO (SBDAMGBI)             -> 0 filas
--   SegDetV / SegDetC             -> 0 filas  (no hay historia comercial que reescribir)
--
-- Dejar @Apply = 0 para el ensayo. Ponerlo en 1 recien despues de revisar el preflight.

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [SBDASEP];

DECLARE @Apply bit = 0;
DECLARE @Partida NVARCHAR(50) = N'Y662124170133';

-- ---------- PREFLIGHT ----------
SELECT 'StockPar' AS tabla, LTRIM(RTRIM(stp_Partida)) AS partida,
       LTRIM(RTRIM(stpart_CodGen)) AS articulo, LTRIM(RTRIM(stpdep_Cod)) AS deposito,
       stp_CantUM1 AS cant, LTRIM(RTRIM(COALESCE(stp_Obs,''))) AS obs
  FROM dbo.StockPar WHERE LTRIM(RTRIM(stp_Partida)) = @Partida;

SELECT 'MovStock' AS tabla, m.mst_ID, m.mstcms_ID,
       LTRIM(RTRIM(m.mstart_CodGen)) AS articulo, LTRIM(RTRIM(m.mstdep_Cod)) AS deposito,
       m.mst_CantUM1 AS cant, LTRIM(RTRIM(COALESCE(c.cmstco_Cod,''))) AS tipo,
       LTRIM(RTRIM(COALESCE(c.cms_Nro,''))) AS nro
  FROM dbo.MovStock m LEFT JOIN dbo.CabMovS c ON c.cms_ID = m.mstcms_ID
 WHERE LTRIM(RTRIM(m.mststp_Partida)) = @Partida;

-- Guarda: abortar si el alcance no es exactamente 1 + 1 y sin comprobantes.
DECLARE @nStock INT = (SELECT COUNT(*) FROM dbo.StockPar WHERE LTRIM(RTRIM(stp_Partida)) = @Partida);
DECLARE @nMov   INT = (SELECT COUNT(*) FROM dbo.MovStock WHERE LTRIM(RTRIM(mststp_Partida)) = @Partida);
DECLARE @nVta   INT = (SELECT COUNT(*) FROM dbo.SegDetV  WHERE LTRIM(RTRIM(sdvstp_Partida)) = @Partida);
DECLARE @nCmp   INT = (SELECT COUNT(*) FROM dbo.SegDetC  WHERE LTRIM(RTRIM(sdcstp_Partida)) = @Partida);
DECLARE @cms    INT = (SELECT TOP 1 mstcms_ID FROM dbo.MovStock WHERE LTRIM(RTRIM(mststp_Partida)) = @Partida);
DECLARE @nImport INT = (SELECT COUNT(*) FROM dbo.CabMovSImport WHERE csicms_ID = @cms);

IF @nStock <> 1 OR @nMov <> 1 OR @nVta <> 0 OR @nCmp <> 0 OR @nImport <> 1
BEGIN
  RAISERROR('Alcance inesperado (StockPar=%d MovStock=%d SegDetV=%d SegDetC=%d CabMovSImport=%d). Abortado.',
            16, 1, @nStock, @nMov, @nVta, @nCmp, @nImport);
  RETURN;
END

IF @Apply = 0
BEGIN
  PRINT 'ENSAYO: alcance correcto (StockPar=1, MovStock=1, CabMovSImport=1, sin comprobantes). Poner @Apply=1 para aplicar.';
  RETURN;
END

-- ---------- BACKUP + BORRADO ----------
BEGIN TRANSACTION;

  IF OBJECT_ID('dbo.NEXORA_BKP_20260902_Y662124170133_StockPar', 'U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260902_Y662124170133_MovStock', 'U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260902_Y662124170133_CabMovS', 'U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260902_Y662124170133_CabMovSImport', 'U') IS NOT NULL
  BEGIN
    RAISERROR('Ya existen tablas de backup NEXORA_BKP_20260902_Y662124170133_*. Revisar antes de continuar.', 16, 1);
  END;

  SELECT * INTO dbo.NEXORA_BKP_20260902_Y662124170133_StockPar
    FROM dbo.StockPar WHERE LTRIM(RTRIM(stp_Partida)) = @Partida;

  SELECT * INTO dbo.NEXORA_BKP_20260902_Y662124170133_MovStock
    FROM dbo.MovStock WHERE LTRIM(RTRIM(mststp_Partida)) = @Partida;

  SELECT c.* INTO dbo.NEXORA_BKP_20260902_Y662124170133_CabMovS
    FROM dbo.CabMovS c
   WHERE c.cms_ID IN (SELECT mstcms_ID FROM dbo.MovStock WHERE LTRIM(RTRIM(mststp_Partida)) = @Partida);

  SELECT * INTO dbo.NEXORA_BKP_20260902_Y662124170133_CabMovSImport
    FROM dbo.CabMovSImport WHERE csicms_ID = @cms;

  DELETE FROM dbo.MovStock WHERE LTRIM(RTRIM(mststp_Partida)) = @Partida;
  DELETE FROM dbo.StockPar WHERE LTRIM(RTRIM(stp_Partida))    = @Partida;
  DELETE FROM dbo.CabMovSImport WHERE csicms_ID = @cms;

  -- La cabecera y su registro de importación solo si quedaron sin ningún renglón.
  DELETE FROM dbo.CabMovS
   WHERE cms_ID = @cms
     AND NOT EXISTS (SELECT 1 FROM dbo.MovStock WHERE mstcms_ID = @cms);

COMMIT TRANSACTION;

-- ---------- VERIFICACION ----------
SELECT
  (SELECT COUNT(*) FROM dbo.StockPar WHERE LTRIM(RTRIM(stp_Partida))    = @Partida) AS resto_stockpar,
  (SELECT COUNT(*) FROM dbo.MovStock WHERE LTRIM(RTRIM(mststp_Partida)) = @Partida) AS resto_movstock,
  (SELECT COUNT(*) FROM dbo.CabMovSImport WHERE csicms_ID = @cms) AS resto_importacion;

-- La partida real debe quedar intacta: 1 unidad de cada articulo en STR.
SELECT LTRIM(RTRIM(stp_Partida)) AS partida, LTRIM(RTRIM(stpart_CodGen)) AS articulo,
       LTRIM(RTRIM(stpdep_Cod)) AS deposito, stp_CantUM1 AS cant
  FROM dbo.StockPar WHERE LTRIM(RTRIM(stp_Partida)) = N'Y662125150133'
 ORDER BY articulo, deposito;

-- ---------- REVERSION ----------
-- INSERT INTO dbo.CabMovS       SELECT * FROM dbo.NEXORA_BKP_20260902_Y662124170133_CabMovS;
-- INSERT INTO dbo.CabMovSImport SELECT * FROM dbo.NEXORA_BKP_20260902_Y662124170133_CabMovSImport;
-- INSERT INTO dbo.StockPar SELECT * FROM dbo.NEXORA_BKP_20260902_Y662124170133_StockPar;
-- INSERT INTO dbo.MovStock SELECT * FROM dbo.NEXORA_BKP_20260902_Y662124170133_MovStock;
