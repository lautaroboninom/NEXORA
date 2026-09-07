-- ============================================================================
-- Bejerman / SAMTRONIC - base SBDASEP (SEPID)
--
-- Borra 5 ingresos (ENT a STR) generados automaticamente por NEXORA que
-- duplicaron el stock de reparacion, porque las operadoras ya habian hecho el
-- ingreso a STR manualmente (a traves de RDA u otros comprobantes).
--
-- CRITERIO: se borran SOLO las filas que son:
--   - ENT de +1 a STR
--   - Con IdOrigen NEXORA-ING-*
--   - Con exactamente 1 renglon por cabecera
--
-- Dejar @Apply = 0 para el ensayo.
-- ============================================================================

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [SBDASEP];

DECLARE @Apply bit = 1;
DECLARE @Usu   NVARCHAR(20) = N'SPST1';
DECLARE @Ahora datetime = CONVERT(datetime, CONVERT(varchar(19), GETDATE(), 120), 120);

DECLARE @objetivo TABLE (
  cms_ID   int,
  mst_ID   int,
  partida  NVARCHAR(50),
  articulo NVARCHAR(20),
  deposito NVARCHAR(10),
  idorigen NVARCHAR(60)
);
INSERT INTO @objetivo VALUES
  (15133, 102276, N'AJD20231157', N'1111002', N'STR', N'NEXORA-ING-00029155'),
  (15495, 103441, N'S0119426',    N'1113003', N'STR', N'NEXORA-ING-00029587'),
  (15778, 104629, N'C3121A01294', N'1103003', N'STR', N'NEXORA-ING-00029622'),
  (15789, 104644, N'E2120301020', N'1103001', N'STR', N'NEXORA-ING-00029623'),
  (15836, 104821, N'H2123405080', N'1111001', N'STR', N'NEXORA-ING-00029630');

-- ---------- PREFLIGHT ----------
DECLARE @okFilas int = (
  SELECT COUNT(*)
    FROM @objetivo o
    JOIN dbo.CabMovS c ON c.cms_ID = o.cms_ID
    JOIN dbo.MovStock m ON m.mst_ID = o.mst_ID AND m.mstcms_ID = o.cms_ID
    JOIN dbo.CabMovSImport i ON i.csicms_ID = o.cms_ID
   WHERE LTRIM(RTRIM(c.cmstco_Cod)) = 'ENT'
     AND LTRIM(RTRIM(i.csi_ID)) = o.idorigen
     AND LTRIM(RTRIM(m.mstdep_Cod)) = o.deposito
     AND LTRIM(RTRIM(m.mststp_Partida)) = o.partida
     AND LTRIM(RTRIM(m.mstart_CodGen)) = o.articulo
     AND m.mst_CantUM1 = 1
     AND (SELECT COUNT(*) FROM dbo.MovStock x WHERE x.mstcms_ID = o.cms_ID) = 1
);

DECLARE @negSp int = (
  SELECT COUNT(*)
    FROM (SELECT o.partida, o.articulo, o.deposito, COUNT(*) AS baja
            FROM @objetivo o GROUP BY o.partida, o.articulo, o.deposito) d
    JOIN dbo.StockPar sp
      ON LTRIM(RTRIM(sp.stp_Partida)) = d.partida
     AND LTRIM(RTRIM(sp.stpart_CodGen)) = d.articulo
     AND LTRIM(RTRIM(sp.stpdep_Cod)) = d.deposito
   WHERE sp.stp_CantUM1 - d.baja < 0
);
DECLARE @negSt int = (
  SELECT COUNT(*)
    FROM (SELECT o.articulo, o.deposito, COUNT(*) AS baja
            FROM @objetivo o GROUP BY o.articulo, o.deposito) d
    JOIN dbo.Stock st
      ON LTRIM(RTRIM(st.stkart_CodGen)) = d.articulo
     AND LTRIM(RTRIM(st.stkdep_Cod)) = d.deposito
   WHERE st.stk_CantUM1 - d.baja < 0
);

SELECT o.partida, o.articulo, o.deposito, o.cms_ID, o.mst_ID, o.idorigen
  FROM @objetivo o;

SELECT filas_objetivo_validadas = @okFilas, esperadas = 5,
       quedarian_negativas_stockpar = @negSp, quedarian_negativas_stock = @negSt;

IF @okFilas <> 5 OR @negSp <> 0 OR @negSt <> 0
BEGIN
  RAISERROR('Preflight fallo.', 16, 1);
  RETURN;
END

IF @Apply = 0
BEGIN
  PRINT 'ENSAYO: preflight correcto. Poner @Apply=1 para borrar.';
  RETURN;
END

-- ---------- BACKUP + BORRADO ----------
BEGIN TRANSACTION;

  SELECT c.* INTO dbo.NEXORA_BKP_20260907_DELSTR_CabMovS
    FROM dbo.CabMovS c WHERE c.cms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT m.* INTO dbo.NEXORA_BKP_20260907_DELSTR_MovStock
    FROM dbo.MovStock m WHERE m.mstcms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT i.* INTO dbo.NEXORA_BKP_20260907_DELSTR_CabMovSImport
    FROM dbo.CabMovSImport i WHERE i.csicms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT sp.* INTO dbo.NEXORA_BKP_20260907_DELSTR_StockPar
    FROM dbo.StockPar sp WHERE LTRIM(RTRIM(sp.stp_Partida)) IN (SELECT partida FROM @objetivo);
  SELECT st.* INTO dbo.NEXORA_BKP_20260907_DELSTR_Stock
    FROM dbo.Stock st WHERE LTRIM(RTRIM(st.stkart_CodGen)) IN (SELECT articulo FROM @objetivo);

  DELETE FROM dbo.MovStock      WHERE mstcms_ID IN (SELECT cms_ID FROM @objetivo);
  DELETE FROM dbo.CabMovSImport WHERE csicms_ID IN (SELECT cms_ID FROM @objetivo);
  DELETE FROM dbo.CabMovS       WHERE cms_ID    IN (SELECT cms_ID FROM @objetivo);

  UPDATE sp
     SET sp.stp_CantUM1 = sp.stp_CantUM1 - d.baja,
         sp.stp_CantUM2 = sp.stp_CantUM2 - d.baja,
         sp.stp_FecMod  = @Ahora, sp.stpusu_Codigo = @Usu
    FROM dbo.StockPar sp
    JOIN (SELECT partida, articulo, deposito, COUNT(*) AS baja
            FROM @objetivo GROUP BY partida, articulo, deposito) d
      ON LTRIM(RTRIM(sp.stp_Partida))   = d.partida
     AND LTRIM(RTRIM(sp.stpart_CodGen)) = d.articulo
     AND LTRIM(RTRIM(sp.stpdep_Cod))    = d.deposito;

  UPDATE st
     SET st.stk_CantUM1 = st.stk_CantUM1 - d.baja,
         st.stk_CantUM2 = st.stk_CantUM2 - d.baja,
         st.stk_FecMod  = @Ahora, st.stkusu_Codigo = @Usu
    FROM dbo.Stock st
    JOIN (SELECT articulo, deposito, COUNT(*) AS baja
            FROM @objetivo GROUP BY articulo, deposito) d
      ON LTRIM(RTRIM(st.stkart_CodGen)) = d.articulo
     AND LTRIM(RTRIM(st.stkdep_Cod))    = d.deposito;

COMMIT TRANSACTION;
PRINT 'APLICADO: borrados los 5 ENT de STR.';
