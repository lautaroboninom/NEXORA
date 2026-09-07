-- ============================================================================
-- Bejerman / SAMTRONIC - base SBDASEP (SEPID)
--
-- Ajuste final para liquidar stock duplicado remanente:
-- 1 equipo en STR (Causa B: habia tenido multiples ingresos/anulaciones)
-- 5 equipos en STC (Causa C: quedaron enganchados por el backfill de jobs)
-- 
-- Emite un SAL de ajuste de 6 renglones utilizando cms_ID = 16509 como molde.
--
-- Dejar @Apply = 0 para el ensayo.
-- ============================================================================

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [SBDASEP];

DECLARE @Apply bit = 1;
DECLARE @Usu   NVARCHAR(20) = N'SPST1';
DECLARE @Ahora datetime = CONVERT(datetime, CONVERT(varchar(19), GETDATE(), 120), 120);
DECLARE @Hoy   datetime = CONVERT(datetime, CONVERT(varchar(10), GETDATE(), 120), 120);
DECLARE @Nro   NVARCHAR(20) = N'99999998';
DECLARE @IdSal NVARCHAR(50) = N'NEXORA-AJUSTE-FINAL-6';
DECLARE @TplSal int = 16509;

DECLARE @objetivo TABLE (
  articulo NVARCHAR(20),
  partida  NVARCHAR(50),
  deposito NVARCHAR(10)
);
INSERT INTO @objetivo VALUES
  (N'1103001', N'E2120301020', N'STR'),
  (N'1105001', N'E2321302026', N'STC'),
  (N'1108010', N'M20406700017', N'STC'),
  (N'1109016', N'MBB2304749',  N'STC'),
  (N'1110001', N'S224D611008',  N'STC'),
  (N'1115003', N'MZJ5S121776',  N'STC');

-- ---------- PREFLIGHT ----------
DECLARE @filasValidas int = (
  SELECT COUNT(*)
    FROM @objetivo o
    JOIN dbo.StockPar sp
      ON LTRIM(RTRIM(sp.stp_Partida)) = o.partida
     AND LTRIM(RTRIM(sp.stpart_CodGen)) = o.articulo
     AND LTRIM(RTRIM(sp.stpdep_Cod)) = o.deposito
   WHERE sp.stp_CantUM1 > 0
);

DECLARE @moldeValido int = (
  SELECT COUNT(*)
    FROM dbo.CabMovS c
    JOIN dbo.MovStock m ON m.mstcms_ID = c.cms_ID
   WHERE c.cms_ID = @TplSal
     AND LTRIM(RTRIM(c.cmstco_Cod)) = 'SAL'
);

SELECT partida, articulo, deposito,
       stockpar_actual = (SELECT stp_CantUM1 FROM dbo.StockPar 
                           WHERE LTRIM(RTRIM(stp_Partida))=o.partida 
                             AND LTRIM(RTRIM(stpart_CodGen))=o.articulo 
                             AND LTRIM(RTRIM(stpdep_Cod))=o.deposito)
  FROM @objetivo o;

SELECT filas_con_stock = @filasValidas, esperadas = 6,
       renglones_molde = @moldeValido;

IF @filasValidas <> 6 OR @moldeValido <> 1
BEGIN
  RAISERROR('Preflight fallo. Revisar stock positivo y molde.', 16, 1);
  RETURN;
END

IF @Apply = 0
BEGIN
  PRINT 'ENSAYO: preflight correcto. Poner @Apply=1 para aplicar el ajuste.';
  RETURN;
END

-- ---------- APLICACION ----------
BEGIN TRANSACTION;

  -- 1. Cabecera SAL
  INSERT INTO dbo.CabMovS
    (cmsemp_Codigo, cmssuc_Cod, cms_FComp, cms_Circuito, cmstco_Cod, cmsptr_Cod,
     cms_CodPvt, cmspvt_CodIN, cms_Letra, cms_Nro, cmscli_Cod, cmspro_Cod,
     cmsape_CodIN, cms_CodApe, cmstrn_Cod, cms_FecMod, cmsusu_Codigo, cms_Convert,
     cms_FContab, cms_PasadoCG, cmstop_Cod, cmscvi_Cod, cms_TransfRentas,
     cmscco_codigo, cms_NroCAE)
  SELECT cmsemp_Codigo, cmssuc_Cod, @Hoy, cms_Circuito, cmstco_Cod, cmsptr_Cod,
         cms_CodPvt, cmspvt_CodIN, cms_Letra, @Nro, cmscli_Cod, cmspro_Cod,
         cmsape_CodIN, cms_CodApe, cmstrn_Cod, @Ahora, @Usu, cms_Convert,
         @Hoy, cms_PasadoCG, cmstop_Cod, cmscvi_Cod, cms_TransfRentas,
         cmscco_codigo, cms_NroCAE
    FROM dbo.CabMovS WHERE cms_ID = @TplSal;
    
  DECLARE @cms int = CAST(SCOPE_IDENTITY() AS int);

  -- 2. IdOrigen
  INSERT INTO dbo.CabMovSImport (csiemp_Codigo, csisuc_Cod, csicms_ID, csi_ID, csiemp_CodigoOrigen)
  SELECT csiemp_Codigo, csisuc_Cod, @cms, @IdSal, csiemp_CodigoOrigen
    FROM dbo.CabMovSImport WHERE csicms_ID = @TplSal;

  -- 3. Renglones
  INSERT INTO dbo.MovStock
    (mstemp_Codigo, mstsuc_Cod, mstcms_ID, mstcem_Cod, mstda1_Cod, mstda2_Cod,
     mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
     mstdep_Cod, mst_CantUM1, mst_CantUM2, mst_CantStockUM1, mst_CantStockUM2,
     mststp_Partida, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
     mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod)
  SELECT m.mstemp_Codigo, m.mstsuc_Cod, @cms, m.mstcem_Cod, m.mstda1_Cod, m.mstda2_Cod,
         a.art_Tipo, o.articulo, m.mstart_CodEle1, m.mstart_CodEle2, m.mstart_CodEle3,
         o.deposito, m.mst_CantUM1, m.mst_CantUM2, m.mst_CantStockUM1, m.mst_CantStockUM2,
         o.partida, m.mststs_Serie, m.mst_PrCostoT, m.mst_Kit, m.mst_PasadoCC,
         m.mst_Transferido, m.mst_PrCostoTME, m.mstsec_ID, m.msttpl_Cod
    FROM @objetivo o
   CROSS JOIN (SELECT * FROM dbo.MovStock WHERE mstcms_ID = @TplSal) m
    JOIN dbo.Articulos a ON LTRIM(RTRIM(a.art_CodGen)) = o.articulo;
    
  IF @@ROWCOUNT <> 6 RAISERROR('MovStock: no se insertaron 6 renglones.', 16, 1);

  -- 4. StockPar
  UPDATE sp
     SET sp.stp_CantUM1 = sp.stp_CantUM1 - 1,
         sp.stp_CantUM2 = sp.stp_CantUM2 - 1,
         sp.stp_FecMod  = @Ahora, sp.stpusu_Codigo = @Usu
    FROM dbo.StockPar sp
    JOIN @objetivo o
      ON LTRIM(RTRIM(sp.stp_Partida))   = o.partida
     AND LTRIM(RTRIM(sp.stpart_CodGen)) = o.articulo
     AND LTRIM(RTRIM(sp.stpdep_Cod))    = o.deposito;
   
  IF @@ROWCOUNT <> 6 RAISERROR('StockPar: no se actualizaron 6 filas.', 16, 1);

  -- 5. Stock (agregado)
  UPDATE st
     SET st.stk_CantUM1 = st.stk_CantUM1 - d.restar,
         st.stk_CantUM2 = st.stk_CantUM2 - d.restar,
         st.stk_FecMod  = @Ahora, st.stkusu_Codigo = @Usu
    FROM dbo.Stock st
    JOIN (SELECT articulo, deposito, COUNT(*) AS restar FROM @objetivo GROUP BY articulo, deposito) d
      ON LTRIM(RTRIM(st.stkart_CodGen)) = d.articulo
     AND LTRIM(RTRIM(st.stkdep_Cod))    = d.deposito;

COMMIT TRANSACTION;
PRINT 'APLICADO: Ajuste final ejecutado.';
