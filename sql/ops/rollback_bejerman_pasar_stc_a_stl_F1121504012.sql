-- ============================================================================
-- REVERSION de sql/ops/bejerman_pasar_stc_a_stl_F1121504012.sql
--
-- Deja la partida F1121504012 como estaba: 1 en STC, 0 en STL, y sin el par
-- SAL/ENT 00329668. Restaura StockPar y Stock desde las tablas de backup que
-- creo el script de ida, no recalculando saldos a mano.
--
-- Las guardas son MAS ESTRECHAS que las de ida: si el par ya no esta intacto
-- (por ejemplo porque despues alguien movio el equipo de nuevo) aborta y no
-- toca nada. En ese caso hay que reconciliar a mano, no revertir a ciegas.
--
-- Dejar @Apply = 0 para el ensayo.
-- ============================================================================

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [SBDASEP];

DECLARE @Apply   bit          = 0;
DECLARE @Partida NVARCHAR(50) = N'F1121504012';
DECLARE @Art     NVARCHAR(20) = N'1102001';
DECLARE @Nro     NVARCHAR(20) = N'00329668';
DECLARE @IdSal   NVARCHAR(50) = N'NEXORA-OS-29668-SAL-STC-STL';
DECLARE @IdEnt   NVARCHAR(50) = N'NEXORA-OS-29668-ENT-STL-FIX';

DECLARE @cmsSal int = (SELECT c.cms_ID FROM dbo.CabMovS c
                        JOIN dbo.CabMovSImport i ON i.csicms_ID = c.cms_ID
                       WHERE LTRIM(RTRIM(i.csi_ID)) = @IdSal);
DECLARE @cmsEnt int = (SELECT c.cms_ID FROM dbo.CabMovS c
                        JOIN dbo.CabMovSImport i ON i.csicms_ID = c.cms_ID
                       WHERE LTRIM(RTRIM(i.csi_ID)) = @IdEnt);

DECLARE @nMov int = (SELECT COUNT(*) FROM dbo.MovStock
                      WHERE mstcms_ID IN (@cmsSal, @cmsEnt)
                        AND LTRIM(RTRIM(mststp_Partida)) = @Partida);
DECLARE @nBkpSp int = (SELECT COUNT(*) FROM dbo.NEXORA_BKP_20260904_F1121504012_StockPar);
DECLARE @nBkpSt int = (SELECT COUNT(*) FROM dbo.NEXORA_BKP_20260904_F1121504012_Stock);
-- Movimientos posteriores al par: si los hay, revertir dejaria saldos falsos.
DECLARE @nPost int = (SELECT COUNT(*) FROM dbo.MovStock m
                       WHERE LTRIM(RTRIM(m.mststp_Partida)) = @Partida
                         AND m.mstcms_ID NOT IN (@cmsSal, @cmsEnt)
                         AND m.mst_ID > (SELECT MAX(x.mst_ID) FROM dbo.MovStock x
                                          WHERE x.mstcms_ID IN (@cmsSal, @cmsEnt)));

SELECT cms_sal = @cmsSal, cms_ent = @cmsEnt, renglones = @nMov,
       backup_stockpar = @nBkpSp, backup_stock = @nBkpSt, movimientos_posteriores = @nPost;

IF @cmsSal IS NULL OR @cmsEnt IS NULL OR @nMov <> 2 OR @nBkpSp < 1 OR @nBkpSt < 1 OR @nPost <> 0
BEGIN
  RAISERROR('Reversion no aplicable (sal=%d ent=%d renglones=%d bkpSp=%d bkpSt=%d posteriores=%d). Reconciliar a mano.',
            16, 1, @cmsSal, @cmsEnt, @nMov, @nBkpSp, @nBkpSt, @nPost);
  RETURN;
END

IF @Apply = 0
BEGIN
  PRINT 'ENSAYO: reversion aplicable. Poner @Apply=1 para revertir.';
  RETURN;
END

BEGIN TRANSACTION;

  DELETE FROM dbo.MovStock       WHERE mstcms_ID IN (@cmsSal, @cmsEnt);
  DELETE FROM dbo.CabMovSImport  WHERE csicms_ID IN (@cmsSal, @cmsEnt);
  DELETE FROM dbo.CabMovS        WHERE cms_ID    IN (@cmsSal, @cmsEnt);

  UPDATE sp
     SET sp.stp_CantUM1 = b.stp_CantUM1, sp.stp_CantUM2 = b.stp_CantUM2,
         sp.stp_FecMod  = b.stp_FecMod,  sp.stpusu_Codigo = b.stpusu_Codigo
    FROM dbo.StockPar sp
    JOIN dbo.NEXORA_BKP_20260904_F1121504012_StockPar b
      ON LTRIM(RTRIM(b.stp_Partida))   = LTRIM(RTRIM(sp.stp_Partida))
     AND LTRIM(RTRIM(b.stpart_CodGen)) = LTRIM(RTRIM(sp.stpart_CodGen))
     AND LTRIM(RTRIM(b.stpdep_Cod))    = LTRIM(RTRIM(sp.stpdep_Cod))
   WHERE LTRIM(RTRIM(sp.stp_Partida)) = @Partida;

  UPDATE st
     SET st.stk_CantUM1 = b.stk_CantUM1, st.stk_CantUM2 = b.stk_CantUM2,
         st.stk_FecMod  = b.stk_FecMod,  st.stkusu_Codigo = b.stkusu_Codigo
    FROM dbo.Stock st
    JOIN dbo.NEXORA_BKP_20260904_F1121504012_Stock b
      ON LTRIM(RTRIM(b.stkart_CodGen)) = LTRIM(RTRIM(st.stkart_CodGen))
     AND LTRIM(RTRIM(b.stkdep_Cod))    = LTRIM(RTRIM(st.stkdep_Cod))
   WHERE LTRIM(RTRIM(st.stkart_CodGen)) = @Art;

COMMIT TRANSACTION;
PRINT 'REVERTIDO: F1121504012 volvio a STC.';
