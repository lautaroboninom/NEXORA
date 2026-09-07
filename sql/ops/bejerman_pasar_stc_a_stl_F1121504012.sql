-- ============================================================================
-- Bejerman / SAMTRONIC - base SBDASEP (SEPID)
--
-- Pasa la partida F1121504012 (MG 7112, BMC Alto Flujo H-80M, articulo 1102001)
-- del deposito STC (ST liberados) a STL (ST listos alq).
--
-- POR QUE: el equipo es del parque de alquiler y volvio de una baja de alquiler
-- el 02/09/2026 (RDA 00004-00005172, NOVAMED). Quedo en STC por el backfill de
-- jobs del 04/06/2026, que clasifico por "cliente = MG BIO" en vez de por numero
-- interno. Ese mismo dia administracion emitio el RTA 00001-00026268 que, como
-- todo alquiler, descuenta de STL: dejo STL en -1. El ENT correcto de la OS 29668
-- (02/09, STR->STL) solo relleno ese hueco, asi que el unico stock vivo quedo el
-- +1 de STC. Saldo de alquiler de NOVAMED por esta partida: 0 (verificado).
--
-- FORMA: replica el par SAL/ENT que el propio NEXORA genero para esta OS el
-- 02/09/2026 (cms_ID 16281/16282). Se usa ese circuito y no una TRA para no
-- pisar la numeracion propia del ERP: el numero 00329668 esta fuera de la
-- secuencia que lleva Bejerman, igual que el 00129668 que ya existe.
--
-- Las filas nuevas se copian con INSERT..SELECT desde ese par y se pisan solo
-- fecha, numero y deposito. No es comodidad: escribir los valores a mano fallo
-- el 04/09/2026 con
--   "The INSERT statement conflicted with the FOREIGN KEY constraint
--    FK__CabMovS__cmsape___6DD739FB ... table dbo.Apertura"
-- porque diez columnas de CabMovS y tres de MovStock estan en NULL, no en
-- cadena vacia, y dbo.Apertura esta VACIA: cualquier valor no nulo ahi viola la
-- FK, mientras que NULL la saltea. Un volcado por CSV no distingue NULL de ''.
-- Copiando la fila, cualquier columna en la que no se haya pensado viaja igual.
--
-- OJO CON LOS MILISEGUNDOS: el importador de Bejerman compara stk_FecMod y
-- stp_FecMod truncados al segundo, con concurrencia optimista. Una fila con
-- milisegundos queda bloqueada para siempre para futuros comprobantes. Por eso
-- @Ahora se trunca al segundo. Ver sql/ops/bejerman_fix_fecmod_milisegundos.sql.
--
-- Reversion: sql/ops/rollback_bejerman_pasar_stc_a_stl_F1121504012.sql
--
-- Dejar @Apply = 0 para el ensayo. Ponerlo en 1 recien despues del preflight.
-- ============================================================================

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [SBDASEP];

DECLARE @Apply   bit           = 0;
DECLARE @Partida NVARCHAR(50)  = N'F1121504012';
DECLARE @Art     NVARCHAR(20)  = N'1102001';
DECLARE @DepOri  NVARCHAR(10)  = N'STC';
DECLARE @DepDes  NVARCHAR(10)  = N'STL';
DECLARE @Nro     NVARCHAR(20)  = N'00329668';
DECLARE @Usu     NVARCHAR(20)  = N'SPST1';
DECLARE @IdSal   NVARCHAR(50)  = N'NEXORA-OS-29668-SAL-STC-STL';
DECLARE @IdEnt   NVARCHAR(50)  = N'NEXORA-OS-29668-ENT-STL-FIX';
-- Cabeceras molde: el par SAL/ENT 00129668 que NEXORA emitio el 02/09/2026.
DECLARE @TplSal  int           = 16281;
DECLARE @TplEnt  int           = 16282;
-- Truncados: al segundo para FecMod, al dia para las fechas de comprobante.
DECLARE @Ahora   datetime      = CONVERT(datetime, CONVERT(varchar(19), GETDATE(), 120), 120);
DECLARE @Hoy     datetime      = CONVERT(datetime, CONVERT(varchar(10), GETDATE(), 120), 120);

-- ---------- PREFLIGHT ----------
DECLARE @spOri float = (SELECT SUM(stp_CantUM1) FROM dbo.StockPar
                         WHERE LTRIM(RTRIM(stp_Partida))=@Partida
                           AND LTRIM(RTRIM(stpart_CodGen))=@Art
                           AND LTRIM(RTRIM(stpdep_Cod))=@DepOri);
DECLARE @spDes float = (SELECT SUM(stp_CantUM1) FROM dbo.StockPar
                         WHERE LTRIM(RTRIM(stp_Partida))=@Partida
                           AND LTRIM(RTRIM(stpart_CodGen))=@Art
                           AND LTRIM(RTRIM(stpdep_Cod))=@DepDes);
DECLARE @nSpOri int = (SELECT COUNT(*) FROM dbo.StockPar
                        WHERE LTRIM(RTRIM(stp_Partida))=@Partida
                          AND LTRIM(RTRIM(stpart_CodGen))=@Art
                          AND LTRIM(RTRIM(stpdep_Cod))=@DepOri);
DECLARE @nSpDes int = (SELECT COUNT(*) FROM dbo.StockPar
                        WHERE LTRIM(RTRIM(stp_Partida))=@Partida
                          AND LTRIM(RTRIM(stpart_CodGen))=@Art
                          AND LTRIM(RTRIM(stpdep_Cod))=@DepDes);
-- El agregado por articulo tiene que cuadrar con la suma por partida en los dos
-- depositos: si ya estuvieran desalineados, esta correccion agravaria el desvio.
DECLARE @nStOri int = (SELECT COUNT(*) FROM dbo.Stock
                        WHERE LTRIM(RTRIM(stkart_CodGen))=@Art AND LTRIM(RTRIM(stkdep_Cod))=@DepOri);
DECLARE @nStDes int = (SELECT COUNT(*) FROM dbo.Stock
                        WHERE LTRIM(RTRIM(stkart_CodGen))=@Art AND LTRIM(RTRIM(stkdep_Cod))=@DepDes);
DECLARE @stOri float = (SELECT SUM(stk_CantUM1) FROM dbo.Stock
                         WHERE LTRIM(RTRIM(stkart_CodGen))=@Art AND LTRIM(RTRIM(stkdep_Cod))=@DepOri);
DECLARE @stDes float = (SELECT SUM(stk_CantUM1) FROM dbo.Stock
                         WHERE LTRIM(RTRIM(stkart_CodGen))=@Art AND LTRIM(RTRIM(stkdep_Cod))=@DepDes);
DECLARE @spTotOri float = (SELECT SUM(stp_CantUM1) FROM dbo.StockPar
                            WHERE LTRIM(RTRIM(stpart_CodGen))=@Art AND LTRIM(RTRIM(stpdep_Cod))=@DepOri);
DECLARE @spTotDes float = (SELECT SUM(stp_CantUM1) FROM dbo.StockPar
                            WHERE LTRIM(RTRIM(stpart_CodGen))=@Art AND LTRIM(RTRIM(stpdep_Cod))=@DepDes);
DECLARE @nNro int = (SELECT COUNT(*) FROM dbo.CabMovS
                      WHERE LTRIM(RTRIM(cms_Nro))=@Nro AND LTRIM(RTRIM(cmstco_Cod)) IN ('SAL','ENT'));
DECLARE @nImp int = (SELECT COUNT(*) FROM dbo.CabMovSImport WHERE LTRIM(RTRIM(csi_ID)) IN (@IdSal,@IdEnt));
-- Los moldes tienen que ser el par de esta misma partida y articulo, con un
-- renglon cada uno y los signos esperados: si no, no se copia nada.
DECLARE @nTpl int = (SELECT COUNT(*) FROM dbo.CabMovS c
                       JOIN dbo.MovStock m ON m.mstcms_ID = c.cms_ID
                      WHERE c.cms_ID IN (@TplSal, @TplEnt)
                        AND LTRIM(RTRIM(m.mststp_Partida)) = @Partida
                        AND LTRIM(RTRIM(m.mstart_CodGen)) = @Art
                        AND ((c.cms_ID = @TplSal AND LTRIM(RTRIM(c.cmstco_Cod)) = 'SAL' AND m.mst_CantUM1 = -1)
                          OR (c.cms_ID = @TplEnt AND LTRIM(RTRIM(c.cmstco_Cod)) = 'ENT' AND m.mst_CantUM1 = 1)));

SELECT partida=@Partida, articulo=@Art, moldes_ok=@nTpl,
       stockpar_origen=@spOri, stockpar_destino=@spDes,
       filas_stockpar_origen=@nSpOri, filas_stockpar_destino=@nSpDes,
       stock_agregado_origen=@stOri, suma_partidas_origen=@spTotOri,
       stock_agregado_destino=@stDes, suma_partidas_destino=@spTotDes,
       numero_tomado=@nNro, idorigen_tomado=@nImp;

IF @spOri <> 1 OR @spDes <> 0 OR @nSpOri <> 1 OR @nSpDes <> 1
   OR @nStOri <> 1 OR @nStDes <> 1
   OR @stOri <> @spTotOri OR @stDes <> @spTotDes
   OR @nNro <> 0 OR @nImp <> 0 OR @nTpl <> 2
BEGIN
  -- RAISERROR no acepta float como sustitucion: se arma el mensaje aparte.
  DECLARE @msg NVARCHAR(600) =
      N'Preflight inesperado. StockPar origen=' + CONVERT(NVARCHAR(20), @spOri)
    + N' destino=' + CONVERT(NVARCHAR(20), @spDes)
    + N' (filas ' + CONVERT(NVARCHAR(10), @nSpOri) + N'/' + CONVERT(NVARCHAR(10), @nSpDes) + N')'
    + N'; Stock origen=' + CONVERT(NVARCHAR(20), @stOri) + N' vs partidas ' + CONVERT(NVARCHAR(20), @spTotOri)
    + N', destino=' + CONVERT(NVARCHAR(20), @stDes) + N' vs partidas ' + CONVERT(NVARCHAR(20), @spTotDes)
    + N'; filas Stock ' + CONVERT(NVARCHAR(10), @nStOri) + N'/' + CONVERT(NVARCHAR(10), @nStDes)
    + N'; numero tomado=' + CONVERT(NVARCHAR(10), @nNro)
    + N'; idorigen tomado=' + CONVERT(NVARCHAR(10), @nImp)
    + N'; moldes ok=' + CONVERT(NVARCHAR(10), @nTpl) + N'. Abortado.';
  RAISERROR(@msg, 16, 1);
  RETURN;
END

IF @Apply = 0
BEGIN
  PRINT 'ENSAYO: preflight correcto. Poner @Apply=1 para aplicar.';
  RETURN;
END

-- ---------- BACKUP + APLICACION ----------
BEGIN TRANSACTION;

  IF OBJECT_ID('dbo.NEXORA_BKP_20260904_F1121504012_StockPar','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260904_F1121504012_Stock','U') IS NOT NULL
  BEGIN
    RAISERROR('Ya existen tablas de backup NEXORA_BKP_20260904_F1121504012_*. Revisar antes de continuar.', 16, 1);
  END;

  SELECT * INTO dbo.NEXORA_BKP_20260904_F1121504012_StockPar
    FROM dbo.StockPar WHERE LTRIM(RTRIM(stp_Partida))=@Partida;

  SELECT * INTO dbo.NEXORA_BKP_20260904_F1121504012_Stock
    FROM dbo.Stock WHERE LTRIM(RTRIM(stkart_CodGen))=@Art;

  -- SAL: sale de STC. Copia de la cabecera molde, pisando fecha y numero.
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
  DECLARE @cmsSal int = CAST(SCOPE_IDENTITY() AS int);

  -- El renglon tambien se copia: solo cambia la cabecera y el deposito.
  INSERT INTO dbo.MovStock
    (mstemp_Codigo, mstsuc_Cod, mstcms_ID, mstcem_Cod, mstda1_Cod, mstda2_Cod,
     mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
     mstdep_Cod, mst_CantUM1, mst_CantUM2, mst_CantStockUM1, mst_CantStockUM2,
     mststp_Partida, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
     mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod)
  SELECT mstemp_Codigo, mstsuc_Cod, @cmsSal, mstcem_Cod, mstda1_Cod, mstda2_Cod,
         mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
         @DepOri, mst_CantUM1, mst_CantUM2, mst_CantStockUM1, mst_CantStockUM2,
         mststp_Partida, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
         mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod
    FROM dbo.MovStock WHERE mstcms_ID = @TplSal;
  IF @@ROWCOUNT <> 1 RAISERROR('MovStock SAL: no se inserto exactamente 1 renglon.', 16, 1);

  INSERT INTO dbo.CabMovSImport (csiemp_Codigo, csisuc_Cod, csicms_ID, csi_ID, csiemp_CodigoOrigen)
  SELECT csiemp_Codigo, csisuc_Cod, @cmsSal, @IdSal, csiemp_CodigoOrigen
    FROM dbo.CabMovSImport WHERE csicms_ID = @TplSal;

  -- ENT: entra a STL
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
    FROM dbo.CabMovS WHERE cms_ID = @TplEnt;
  DECLARE @cmsEnt int = CAST(SCOPE_IDENTITY() AS int);

  INSERT INTO dbo.MovStock
    (mstemp_Codigo, mstsuc_Cod, mstcms_ID, mstcem_Cod, mstda1_Cod, mstda2_Cod,
     mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
     mstdep_Cod, mst_CantUM1, mst_CantUM2, mst_CantStockUM1, mst_CantStockUM2,
     mststp_Partida, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
     mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod)
  SELECT mstemp_Codigo, mstsuc_Cod, @cmsEnt, mstcem_Cod, mstda1_Cod, mstda2_Cod,
         mstart_Tipo, mstart_CodGen, mstart_CodEle1, mstart_CodEle2, mstart_CodEle3,
         @DepDes, mst_CantUM1, mst_CantUM2, mst_CantStockUM1, mst_CantStockUM2,
         mststp_Partida, mststs_Serie, mst_PrCostoT, mst_Kit, mst_PasadoCC,
         mst_Transferido, mst_PrCostoTME, mstsec_ID, msttpl_Cod
    FROM dbo.MovStock WHERE mstcms_ID = @TplEnt;
  IF @@ROWCOUNT <> 1 RAISERROR('MovStock ENT: no se inserto exactamente 1 renglon.', 16, 1);

  INSERT INTO dbo.CabMovSImport (csiemp_Codigo, csisuc_Cod, csicms_ID, csi_ID, csiemp_CodigoOrigen)
  SELECT csiemp_Codigo, csisuc_Cod, @cmsEnt, @IdEnt, csiemp_CodigoOrigen
    FROM dbo.CabMovSImport WHERE csicms_ID = @TplEnt;

  -- Saldos por partida
  UPDATE dbo.StockPar
     SET stp_CantUM1 = stp_CantUM1 - 1, stp_CantUM2 = stp_CantUM2 - 1,
         stp_FecMod = @Ahora, stpusu_Codigo = @Usu
   WHERE LTRIM(RTRIM(stp_Partida))=@Partida AND LTRIM(RTRIM(stpart_CodGen))=@Art
     AND LTRIM(RTRIM(stpdep_Cod))=@DepOri;
  IF @@ROWCOUNT <> 1 RAISERROR('StockPar origen: no se actualizo exactamente 1 fila.', 16, 1);

  UPDATE dbo.StockPar
     SET stp_CantUM1 = stp_CantUM1 + 1, stp_CantUM2 = stp_CantUM2 + 1,
         stp_FecMod = @Ahora, stpusu_Codigo = @Usu
   WHERE LTRIM(RTRIM(stp_Partida))=@Partida AND LTRIM(RTRIM(stpart_CodGen))=@Art
     AND LTRIM(RTRIM(stpdep_Cod))=@DepDes;
  IF @@ROWCOUNT <> 1 RAISERROR('StockPar destino: no se actualizo exactamente 1 fila.', 16, 1);

  -- Saldos agregados por articulo
  UPDATE dbo.Stock
     SET stk_CantUM1 = stk_CantUM1 - 1, stk_CantUM2 = stk_CantUM2 - 1,
         stk_FecMod = @Ahora, stkusu_Codigo = @Usu
   WHERE LTRIM(RTRIM(stkart_CodGen))=@Art AND LTRIM(RTRIM(stkdep_Cod))=@DepOri;
  IF @@ROWCOUNT <> 1 RAISERROR('Stock origen: no se actualizo exactamente 1 fila.', 16, 1);

  UPDATE dbo.Stock
     SET stk_CantUM1 = stk_CantUM1 + 1, stk_CantUM2 = stk_CantUM2 + 1,
         stk_FecMod = @Ahora, stkusu_Codigo = @Usu
   WHERE LTRIM(RTRIM(stkart_CodGen))=@Art AND LTRIM(RTRIM(stkdep_Cod))=@DepDes;
  IF @@ROWCOUNT <> 1 RAISERROR('Stock destino: no se actualizo exactamente 1 fila.', 16, 1);

COMMIT TRANSACTION;
PRINT 'APLICADO: F1121504012 paso de STC a STL.';
