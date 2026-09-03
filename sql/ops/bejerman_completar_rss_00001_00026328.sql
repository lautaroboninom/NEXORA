-- Completa en SEPID el RSS R 00001-00026328 (SegCabV.scv_ID = 19041).
--
-- El comprobante fue registrado desde NEXORA con solo dos de los cuatro equipos
-- que figuran en el remito de papel. Se agregan las OS 29413 y 29496, junto con
-- sus movimientos -1 en STC, y se deja el stock de ambas partidas en cero.
--
-- Alcance verificado el 02/09/2026:
--   existentes: OS 29437 / 40966L0552 y OS 29369 / 40966K1229
--   faltantes:  OS 29413 / ES119301028 / articulo 1103002
--               OS 29496 / 40966K0796  / articulo 1107006
--
-- El script es de ensayo por defecto. La aplicacion real debe ejecutarse por el
-- canal corto scripts/samtronic_sql.py, reemplazando @Apply por 1 solo en memoria.

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [SBDASEP];

DECLARE @Apply bit = 0;
DECLARE @ScvId int = 19041;
DECLARE @CmsId int = 16284;
DECLARE @PartidaCpap varchar(26) = 'ES119301028';
DECLARE @PartidaPb560 varchar(26) = '40966K0796';

-- ---------- PREFLIGHT ----------
SELECT
  c.scv_ID,
  LTRIM(RTRIM(t.spvtco_Cod)) AS tipo,
  LTRIM(RTRIM(t.spv_Letra)) AS letra,
  LTRIM(RTRIM(t.spv_CodPvt)) AS punto,
  LTRIM(RTRIM(t.spv_Nro)) AS numero,
  CONVERT(varchar(10), c.scv_FEmision, 23) AS fecha,
  LTRIM(RTRIM(c.scvcli_Cod)) AS cliente,
  (SELECT COUNT(*) FROM dbo.SegDetV d WHERE d.sdvscv_ID = @ScvId) AS renglones,
  (SELECT COUNT(*) FROM dbo.MovStock m WHERE m.mstcms_ID = @CmsId) AS movimientos
FROM dbo.SegCabV c
JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
WHERE c.scv_ID = @ScvId;

SELECT
  LTRIM(RTRIM(sp.stp_Partida)) AS partida,
  LTRIM(RTRIM(sp.stpart_CodGen)) AS articulo,
  LTRIM(RTRIM(sp.stpdep_Cod)) AS deposito,
  sp.stp_CantUM1 AS cantidad,
  LTRIM(RTRIM(COALESCE(sp.stp_Obs, ''))) AS observacion
FROM dbo.StockPar sp
WHERE LTRIM(RTRIM(sp.stp_Partida)) IN (@PartidaCpap, @PartidaPb560)
  AND LTRIM(RTRIM(sp.stpdep_Cod)) = 'STC'
ORDER BY partida;

DECLARE @HeaderOk int = (
  SELECT COUNT(*)
  FROM dbo.SegCabV c
  JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
  WHERE c.scv_ID = @ScvId
    AND LTRIM(RTRIM(t.spvtco_Cod)) = 'RSS'
    AND LTRIM(RTRIM(t.spv_Letra)) = 'R'
    AND LTRIM(RTRIM(t.spv_CodPvt)) = '00001'
    AND LTRIM(RTRIM(t.spv_Nro)) = '00026328'
    AND LTRIM(RTRIM(c.scvcli_Cod)) = 'ARGAS'
);
DECLARE @ExistingDetail int = (
  SELECT COUNT(*) FROM dbo.SegDetV WHERE sdvscv_ID = @ScvId
);
DECLARE @ExistingMovement int = (
  SELECT COUNT(*) FROM dbo.MovStock WHERE mstcms_ID = @CmsId
);
DECLARE @TargetDetail int = (
  SELECT COUNT(*) FROM dbo.SegDetV
  WHERE sdvscv_ID = @ScvId
    AND LTRIM(RTRIM(sdvstp_Partida)) IN (@PartidaCpap, @PartidaPb560)
);
DECLARE @TargetMovement int = (
  SELECT COUNT(*) FROM dbo.MovStock
  WHERE mstcms_ID = @CmsId
    AND LTRIM(RTRIM(mststp_Partida)) IN (@PartidaCpap, @PartidaPb560)
);
DECLARE @TargetStockReady int = (
  SELECT COUNT(*) FROM dbo.StockPar
  WHERE LTRIM(RTRIM(stp_Partida)) IN (@PartidaCpap, @PartidaPb560)
    AND LTRIM(RTRIM(stpdep_Cod)) = 'STC'
    AND stp_CantUM1 = 1
    AND stp_CantUM2 = 1
);
DECLARE @TargetStockApplied int = (
  SELECT COUNT(*) FROM dbo.StockPar
  WHERE LTRIM(RTRIM(stp_Partida)) IN (@PartidaCpap, @PartidaPb560)
    AND LTRIM(RTRIM(stpdep_Cod)) = 'STC'
    AND stp_CantUM1 = 0
    AND stp_CantUM2 = 0
);
DECLARE @TemplatesOk int = (
  SELECT COUNT(*) FROM dbo.SegDetV WHERE sdv_ID IN (110534, 110535)
) + (
  SELECT COUNT(*) FROM dbo.MovStock WHERE mst_ID IN (106359, 106361)
);

IF @HeaderOk = 1
   AND @ExistingDetail = 8
   AND @ExistingMovement = 4
   AND @TargetDetail = 2
   AND @TargetMovement = 2
   AND @TargetStockApplied = 2
BEGIN
  PRINT 'YA APLICADO: el RSS 00001-00026328 contiene los cuatro equipos y el stock objetivo esta en cero.';
  RETURN;
END;

IF @HeaderOk <> 1
   OR @ExistingDetail <> 4
   OR @ExistingMovement <> 2
   OR @TargetDetail <> 0
   OR @TargetMovement <> 0
   OR @TargetStockReady <> 2
   OR @TemplatesOk <> 4
BEGIN
  RAISERROR(
    'Preflight inesperado (header=%d detalle=%d mov=%d detalle_obj=%d mov_obj=%d stock=%d templates=%d). Abortado.',
    16, 1, @HeaderOk, @ExistingDetail, @ExistingMovement, @TargetDetail,
    @TargetMovement, @TargetStockReady, @TemplatesOk
  );
  RETURN;
END;

IF @Apply = 0
BEGIN
  PRINT 'ENSAYO CORRECTO: se agregaran 4 renglones, 2 movimientos y se dejaran 2 partidas STC en cero.';
  RETURN;
END;

-- ---------- BACKUP + APLICACION ----------
BEGIN TRY
  BEGIN TRANSACTION;

  -- Vuelve a bloquear y validar las filas dentro de la misma transaccion.
  IF (SELECT COUNT(*) FROM dbo.SegDetV WITH (UPDLOCK, HOLDLOCK) WHERE sdvscv_ID = @ScvId) <> 4
     OR (SELECT COUNT(*) FROM dbo.MovStock WITH (UPDLOCK, HOLDLOCK) WHERE mstcms_ID = @CmsId) <> 2
     OR (SELECT COUNT(*) FROM dbo.StockPar WITH (UPDLOCK, HOLDLOCK)
         WHERE LTRIM(RTRIM(stp_Partida)) IN (@PartidaCpap, @PartidaPb560)
           AND LTRIM(RTRIM(stpdep_Cod)) = 'STC'
           AND stp_CantUM1 = 1 AND stp_CantUM2 = 1) <> 2
  BEGIN
    RAISERROR('Los datos cambiaron despues del preflight. Abortado.', 16, 1);
  END;

  IF OBJECT_ID('dbo.NEXORA_BKP_20260902_RSS26328_SegDetV', 'U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260902_RSS26328_MovStock', 'U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260902_RSS26328_StockPar', 'U') IS NOT NULL
  BEGIN
    RAISERROR('Ya existen tablas de backup NEXORA_BKP_20260902_RSS26328_*. Revisar antes de continuar.', 16, 1);
  END;

  SELECT * INTO dbo.NEXORA_BKP_20260902_RSS26328_SegDetV
  FROM dbo.SegDetV WHERE sdvscv_ID = @ScvId;

  SELECT * INTO dbo.NEXORA_BKP_20260902_RSS26328_MovStock
  FROM dbo.MovStock WHERE mstcms_ID = @CmsId;

  SELECT * INTO dbo.NEXORA_BKP_20260902_RSS26328_StockPar
  FROM dbo.StockPar
  WHERE LTRIM(RTRIM(stp_Partida)) IN (@PartidaCpap, @PartidaPb560)
    AND LTRIM(RTRIM(stpdep_Cod)) = 'STC';

  DECLARE @MovCpap int;
  DECLARE @MovPb560 int;

  INSERT INTO dbo.MovStock (
    mstemp_Codigo, mstsuc_Cod, mstcms_ID, mstcem_Cod, mstda1_Cod, mstda2_Cod,
    mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
    mstdep_Cod, mst_CantUM1, mst_CantUM2, mst_CantStockUM1, mst_CantStockUM2,
    mststp_Partida, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
    mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod
  )
  SELECT
    mstemp_Codigo, mstsuc_Cod, @CmsId, mstcem_Cod, mstda1_Cod, '1103',
    mstart_Tipo, '1103002 ', mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
    mstdep_Cod, -1, -1, mst_CantStockUM1, mst_CantStockUM2,
    @PartidaCpap, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
    mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod
  FROM dbo.MovStock WHERE mst_ID = 106359;
  SET @MovCpap = CONVERT(int, SCOPE_IDENTITY());

  INSERT INTO dbo.MovStock (
    mstemp_Codigo, mstsuc_Cod, mstcms_ID, mstcem_Cod, mstda1_Cod, mstda2_Cod,
    mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
    mstdep_Cod, mst_CantUM1, mst_CantUM2, mst_CantStockUM1, mst_CantStockUM2,
    mststp_Partida, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
    mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod
  )
  SELECT
    mstemp_Codigo, mstsuc_Cod, @CmsId, mstcem_Cod, mstda1_Cod, mstda2_Cod,
    mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
    mstdep_Cod, -1, -1, mst_CantStockUM1, mst_CantStockUM2,
    @PartidaPb560, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
    mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod
  FROM dbo.MovStock WHERE mst_ID = 106361;
  SET @MovPb560 = CONVERT(int, SCOPE_IDENTITY());

  -- Clona un renglon de SegDetV preservando las 78 columnas no identity y
  -- reemplaza solamente el contexto que corresponde al equipo nuevo.
  DECLARE @Columns nvarchar(max);
  DECLARE @Expressions nvarchar(max);
  DECLARE @Sql nvarchar(max);
  SELECT @Columns = STUFF((
    SELECT ',' + QUOTENAME(c.name)
    FROM sys.columns c
    WHERE c.object_id = OBJECT_ID('dbo.SegDetV') AND c.is_identity = 0 AND c.is_computed = 0
    ORDER BY c.column_id
    FOR XML PATH(''), TYPE
  ).value('.', 'nvarchar(max)'), 1, 1, '');
  SELECT @Expressions = STUFF((
    SELECT ',' + CASE c.name
      WHEN 'sdvscv_ID' THEN '@NewScvId'
      WHEN 'sdv_NReng' THEN '@NewRenglon'
      WHEN 'sdv_Desc' THEN '@NewDescripcion'
      WHEN 'sdvart_CodGen' THEN 'COALESCE(@NewArticulo, src.' + QUOTENAME(c.name) + ')'
      WHEN 'sdvstp_Partida' THEN 'COALESCE(@NewPartida, src.' + QUOTENAME(c.name) + ')'
      WHEN 'sdvmst_ID' THEN 'COALESCE(@NewMovId, src.' + QUOTENAME(c.name) + ')'
      ELSE 'src.' + QUOTENAME(c.name)
    END
    FROM sys.columns c
    WHERE c.object_id = OBJECT_ID('dbo.SegDetV') AND c.is_identity = 0 AND c.is_computed = 0
    ORDER BY c.column_id
    FOR XML PATH(''), TYPE
  ).value('.', 'nvarchar(max)'), 1, 1, '');
  SET @Sql = N'INSERT INTO dbo.SegDetV (' + @Columns + N') SELECT ' + @Expressions
           + N' FROM dbo.SegDetV src WHERE src.sdv_ID = @SourceId;';

  EXEC sp_executesql @Sql,
    N'@SourceId int, @NewScvId int, @NewRenglon smallint, @NewDescripcion varchar(50), @NewArticulo varchar(20), @NewPartida varchar(26), @NewMovId int',
    110534, @ScvId, 5, 'Se entrega sin reparar - OS 29413 -', NULL, NULL, NULL;
  EXEC sp_executesql @Sql,
    N'@SourceId int, @NewScvId int, @NewRenglon smallint, @NewDescripcion varchar(50), @NewArticulo varchar(20), @NewPartida varchar(26), @NewMovId int',
    110535, @ScvId, 6, 'CPAP BMC CPAP G2S C20', '1103002 ', @PartidaCpap, @MovCpap;
  EXEC sp_executesql @Sql,
    N'@SourceId int, @NewScvId int, @NewRenglon smallint, @NewDescripcion varchar(50), @NewArticulo varchar(20), @NewPartida varchar(26), @NewMovId int',
    110534, @ScvId, 7, 'Se entrega sin reparar - OS 29496 -', NULL, NULL, NULL;
  EXEC sp_executesql @Sql,
    N'@SourceId int, @NewScvId int, @NewRenglon smallint, @NewDescripcion varchar(50), @NewArticulo varchar(20), @NewPartida varchar(26), @NewMovId int',
    110535, @ScvId, 8, 'Respirador PURITAN BENNETT PB 560', '1107006 ', @PartidaPb560, @MovPb560;

  UPDATE dbo.StockPar
     SET stp_CantUM1 = 0,
         stp_CantUM2 = 0,
         stp_FecMod = GETDATE(),
         stpusu_Codigo = 'SPDEP'
   WHERE LTRIM(RTRIM(stp_Partida)) IN (@PartidaCpap, @PartidaPb560)
     AND LTRIM(RTRIM(stpdep_Cod)) = 'STC'
     AND stp_CantUM1 = 1
     AND stp_CantUM2 = 1;

  IF (SELECT COUNT(*) FROM dbo.SegDetV WHERE sdvscv_ID = @ScvId) <> 8
     OR (SELECT COUNT(*) FROM dbo.MovStock WHERE mstcms_ID = @CmsId) <> 4
     OR (SELECT COUNT(*) FROM dbo.SegDetV
         WHERE sdvscv_ID = @ScvId
           AND LTRIM(RTRIM(sdvstp_Partida)) IN (@PartidaCpap, @PartidaPb560)) <> 2
     OR (SELECT COUNT(*) FROM dbo.MovStock
         WHERE mstcms_ID = @CmsId
           AND LTRIM(RTRIM(mststp_Partida)) IN (@PartidaCpap, @PartidaPb560)
           AND mstdep_Cod = 'STC' AND mst_CantUM1 = -1) <> 2
     OR (SELECT COUNT(*) FROM dbo.StockPar
         WHERE LTRIM(RTRIM(stp_Partida)) IN (@PartidaCpap, @PartidaPb560)
           AND LTRIM(RTRIM(stpdep_Cod)) = 'STC'
           AND stp_CantUM1 = 0 AND stp_CantUM2 = 0) <> 2
  BEGIN
    RAISERROR('La verificacion interna no dio los conteos esperados. Se revierte toda la transaccion.', 16, 1);
  END;

  COMMIT TRANSACTION;
END TRY
BEGIN CATCH
  IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
  THROW;
END CATCH;

-- ---------- VERIFICACION INDEPENDIENTE ----------
SELECT sdv_NReng AS renglon, LTRIM(RTRIM(sdv_Desc)) AS descripcion,
       LTRIM(RTRIM(sdvart_CodGen)) AS articulo,
       LTRIM(RTRIM(sdvstp_Partida)) AS partida,
       LTRIM(RTRIM(COALESCE(sdvdep_Cod, ''))) AS deposito,
       sdv_CantUM1 AS cantidad, sdvmst_ID AS movimiento
FROM dbo.SegDetV
WHERE sdvscv_ID = @ScvId
ORDER BY sdv_NReng;

SELECT LTRIM(RTRIM(mststp_Partida)) AS partida,
       LTRIM(RTRIM(mstart_CodGen)) AS articulo,
       LTRIM(RTRIM(mstdep_Cod)) AS deposito,
       mst_CantUM1 AS cantidad
FROM dbo.MovStock
WHERE mstcms_ID = @CmsId
ORDER BY mst_ID;

-- Reversion manual (solo si no hubo movimientos posteriores):
--   1. borrar primero los SegDetV 5..8 del scv_ID 19041;
--   2. borrar los MovStock del cms_ID 16284 para ES119301028/40966K0796;
--   3. restaurar StockPar desde NEXORA_BKP_20260902_RSS26328_StockPar.
