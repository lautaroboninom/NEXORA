-- ============================================================================
-- Bejerman / SAMTRONIC - base SBDASEP (SEPID)
--
-- Borra los ENT fantasma que el restore de stock de NEXORA emitio el 08/06/2026
-- y que dejaron dos equipos con stock duplicado.
--
-- ORIGEN: el backfill del 04/06/2026 creo un job STR->STC para OS que YA tenian
-- un job STR->STL (el indice uq_bejerman_sync_jobs_type_ingreso es
-- (sync_type, ingreso_id), asi que dos transferencias de tipo distinto para la
-- misma OS conviven). Al correr `restore_target_stock_from_jobs`, cada job vio
-- su deposito destino vacio y emitio su propio ENT, con IdOrigen distinto
-- (`-ENT-STC` y `-ENT-STL`), que Bejerman no puede deduplicar. Ninguno tiene SAL
-- de contrapartida: son stock creado de la nada.
-- La guarda que hoy lo impediria (`stock_restore_duplicate_guard`) se agrego
-- despues, el 01/07/2026 (commit 7ac9302).
--
-- QUE SE BORRA (3 filas, todas ENT +1 del 08/06/2026, usuario SPST1):
--
--   MG 6522 / MZJ5S121244 / art 1115003
--     cms 14103, mst 99529, STC, NEXORA-OS-29119-ENT-STC   <- sobra
--     (queda el de STL: el equipo es del parque de alquiler y no lo tiene nadie)
--
--   MG 7206 / MA23050810004 / art 1108004
--     cms 14084, mst 99510, STC, NEXORA-OS-28886-ENT-STC   <- sobra
--     cms 14086, mst 99512, STL, NEXORA-OS-28886-ENT-STL   <- sobra
--     Los DOS sobran: la unidad real estaba en STR, la TRA 00001612 del 25/08 la
--     paso a STL y el RTA 00005103 del mismo dia se la llevo a ALCLA, que hoy la
--     tiene (saldo de alquiler = 1). Con el equipo en el cliente, el stock
--     correcto es 0 en todos los depositos.
--
-- Se BORRA y no se compensa con un SAL: son comprobantes que nunca existieron
-- fisicamente, no un movimiento a revertir. Mismo criterio que
-- sql/ops/bejerman_borrar_partida_fantasma_Y662124170133.sql.
--
-- OJO CON LOS MILISEGUNDOS: FecMod se escribe truncado al segundo o la fila
-- queda bloqueada para el importador. Ver bejerman_fix_fecmod_milisegundos.sql.
--
-- Reversion: sql/ops/rollback_bejerman_borrar_ent_duplicados_20260608.sql
--
-- Dejar @Apply = 0 para el ensayo.
-- ============================================================================

SET XACT_ABORT ON;
SET NOCOUNT ON;
USE [SBDASEP];

DECLARE @Apply bit = 0;
DECLARE @Usu   NVARCHAR(20) = N'SPST1';
DECLARE @Ahora datetime = CONVERT(datetime, CONVERT(varchar(19), GETDATE(), 120), 120);

DECLARE @objetivo TABLE (
  cms_ID   int,
  mst_ID   int,
  partida  NVARCHAR(50),
  articulo NVARCHAR(20),
  deposito NVARCHAR(10),
  idorigen NVARCHAR(50)
);
INSERT INTO @objetivo VALUES
  (14103, 99529, N'MZJ5S121244',   N'1115003', N'STC', N'NEXORA-OS-29119-ENT-STC'),
  (14084, 99510, N'MA23050810004', N'1108004', N'STC', N'NEXORA-OS-28886-ENT-STC'),
  (14086, 99512, N'MA23050810004', N'1108004', N'STL', N'NEXORA-OS-28886-ENT-STL');

-- ---------- PREFLIGHT ----------
-- Cada fila objetivo tiene que ser exactamente lo que se describe arriba: ENT de
-- +1, del 08/06/2026, con ese IdOrigen, ese deposito, esa partida y ese articulo,
-- y con un unico renglon en la cabecera (no se parte un comprobante en dos).
DECLARE @okFilas int = (
  SELECT COUNT(*)
    FROM @objetivo o
    JOIN dbo.CabMovS c ON c.cms_ID = o.cms_ID
    JOIN dbo.MovStock m ON m.mst_ID = o.mst_ID AND m.mstcms_ID = o.cms_ID
    JOIN dbo.CabMovSImport i ON i.csicms_ID = o.cms_ID
   WHERE LTRIM(RTRIM(c.cmstco_Cod)) = 'ENT'
     AND CONVERT(varchar(10), c.cms_FComp, 120) = '2026-06-08'
     AND LTRIM(RTRIM(i.csi_ID)) = o.idorigen
     AND LTRIM(RTRIM(m.mstdep_Cod)) = o.deposito
     AND LTRIM(RTRIM(m.mststp_Partida)) = o.partida
     AND LTRIM(RTRIM(m.mstart_CodGen)) = o.articulo
     AND m.mst_CantUM1 = 1
     AND (SELECT COUNT(*) FROM dbo.MovStock x WHERE x.mstcms_ID = o.cms_ID) = 1
);

-- Los saldos por partida y el agregado por articulo tienen que quedar en cero o
-- mas despues de restar: si alguno quedara negativo, el diagnostico esta mal.
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
-- Y el agregado tiene que cuadrar HOY con la suma por partida en cada deposito
-- tocado: si ya venia descuadrado, esto no es lo que hay que correr.
DECLARE @descuadre int = (
  SELECT COUNT(*)
    FROM (SELECT DISTINCT articulo, deposito FROM @objetivo) d
    OUTER APPLY (SELECT SUM(st.stk_CantUM1) AS c FROM dbo.Stock st
                  WHERE LTRIM(RTRIM(st.stkart_CodGen)) = d.articulo
                    AND LTRIM(RTRIM(st.stkdep_Cod)) = d.deposito) a
    OUTER APPLY (SELECT SUM(sp.stp_CantUM1) AS c FROM dbo.StockPar sp
                  WHERE LTRIM(RTRIM(sp.stpart_CodGen)) = d.articulo
                    AND LTRIM(RTRIM(sp.stpdep_Cod)) = d.deposito) b
   WHERE ISNULL(a.c, 0) <> ISNULL(b.c, 0)
);

SELECT o.partida, o.articulo, o.deposito, o.cms_ID, o.mst_ID, o.idorigen,
       stockpar_actual = (SELECT sp.stp_CantUM1 FROM dbo.StockPar sp
                           WHERE LTRIM(RTRIM(sp.stp_Partida)) = o.partida
                             AND LTRIM(RTRIM(sp.stpart_CodGen)) = o.articulo
                             AND LTRIM(RTRIM(sp.stpdep_Cod)) = o.deposito),
       stock_agregado_actual = (SELECT st.stk_CantUM1 FROM dbo.Stock st
                                 WHERE LTRIM(RTRIM(st.stkart_CodGen)) = o.articulo
                                   AND LTRIM(RTRIM(st.stkdep_Cod)) = o.deposito)
  FROM @objetivo o;

SELECT filas_objetivo_validadas = @okFilas, esperadas = 3,
       quedarian_negativas_stockpar = @negSp, quedarian_negativas_stock = @negSt,
       descuadres_previos = @descuadre;

IF @okFilas <> 3 OR @negSp <> 0 OR @negSt <> 0 OR @descuadre <> 0
BEGIN
  DECLARE @msg NVARCHAR(400) =
      N'Preflight inesperado. Filas validadas=' + CONVERT(NVARCHAR(10), @okFilas)
    + N' de 3; negativas StockPar=' + CONVERT(NVARCHAR(10), @negSp)
    + N'; negativas Stock=' + CONVERT(NVARCHAR(10), @negSt)
    + N'; descuadres previos=' + CONVERT(NVARCHAR(10), @descuadre) + N'. Abortado.';
  RAISERROR(@msg, 16, 1);
  RETURN;
END

IF @Apply = 0
BEGIN
  PRINT 'ENSAYO: preflight correcto. Poner @Apply=1 para borrar.';
  RETURN;
END

-- ---------- BACKUP + BORRADO ----------
BEGIN TRANSACTION;

  IF OBJECT_ID('dbo.NEXORA_BKP_20260907_ENTDUP_CabMovS','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_ENTDUP_MovStock','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_ENTDUP_CabMovSImport','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_ENTDUP_StockPar','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_ENTDUP_Stock','U') IS NOT NULL
  BEGIN
    RAISERROR('Ya existen tablas NEXORA_BKP_20260907_ENTDUP_*. Revisar antes de continuar.', 16, 1);
  END;

  SELECT c.* INTO dbo.NEXORA_BKP_20260907_ENTDUP_CabMovS
    FROM dbo.CabMovS c WHERE c.cms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT m.* INTO dbo.NEXORA_BKP_20260907_ENTDUP_MovStock
    FROM dbo.MovStock m WHERE m.mstcms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT i.* INTO dbo.NEXORA_BKP_20260907_ENTDUP_CabMovSImport
    FROM dbo.CabMovSImport i WHERE i.csicms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT sp.* INTO dbo.NEXORA_BKP_20260907_ENTDUP_StockPar
    FROM dbo.StockPar sp WHERE LTRIM(RTRIM(sp.stp_Partida)) IN (SELECT partida FROM @objetivo);
  SELECT st.* INTO dbo.NEXORA_BKP_20260907_ENTDUP_Stock
    FROM dbo.Stock st WHERE LTRIM(RTRIM(st.stkart_CodGen)) IN (SELECT articulo FROM @objetivo);

  DELETE FROM dbo.MovStock      WHERE mstcms_ID IN (SELECT cms_ID FROM @objetivo);
  IF @@ROWCOUNT <> 3 RAISERROR('MovStock: no se borraron exactamente 3 renglones.', 16, 1);

  DELETE FROM dbo.CabMovSImport WHERE csicms_ID IN (SELECT cms_ID FROM @objetivo);
  DELETE FROM dbo.CabMovS       WHERE cms_ID    IN (SELECT cms_ID FROM @objetivo);
  IF @@ROWCOUNT <> 3 RAISERROR('CabMovS: no se borraron exactamente 3 cabeceras.', 16, 1);

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
  IF @@ROWCOUNT <> 3 RAISERROR('StockPar: no se actualizaron exactamente 3 filas.', 16, 1);

  UPDATE st
     SET st.stk_CantUM1 = st.stk_CantUM1 - d.baja,
         st.stk_CantUM2 = st.stk_CantUM2 - d.baja,
         st.stk_FecMod  = @Ahora, st.stkusu_Codigo = @Usu
    FROM dbo.Stock st
    JOIN (SELECT articulo, deposito, COUNT(*) AS baja
            FROM @objetivo GROUP BY articulo, deposito) d
      ON LTRIM(RTRIM(st.stkart_CodGen)) = d.articulo
     AND LTRIM(RTRIM(st.stkdep_Cod))    = d.deposito;
  IF @@ROWCOUNT <> 3 RAISERROR('Stock: no se actualizaron exactamente 3 filas.', 16, 1);

COMMIT TRANSACTION;
PRINT 'APLICADO: borrados los 3 ENT duplicados del 08/06/2026.';
