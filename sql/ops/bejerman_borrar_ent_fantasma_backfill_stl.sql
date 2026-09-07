-- ============================================================================
-- Bejerman / SAMTRONIC - base SBDASEP (SEPID)
--
-- Borra los 18 ENT fantasma que el restore de stock de NEXORA emitio el
-- 08/06/2026 y que dejaron equipos del parque de alquiler con stock duplicado
-- en STL (stock fantasma) y STR (stock real, por reparacion en curso).
--
-- ORIGEN: el `restore_target_stock_from_jobs` del 08/06 emitio un ENT a STL
-- para cada equipo cuyo job indicaba STL como destino. Pero muchos de esos
-- equipos ya estaban en STR (habian ingresado a reparacion). El ENT a STL no
-- tenia SAL de contrapartida: es stock creado de la nada. La guarda que hoy lo
-- impediria (`stock_restore_duplicate_guard`) se agrego despues del hecho
-- (commit 7ac9302, 01/07/2026).
--
-- CRITERIO: se borran SOLO las filas que son:
--   - ENT de +1 a STL
--   - Del 08/06/2026
--   - Con IdOrigen NEXORA-OS-xxxxx-ENT-STL
--   - Con exactamente 1 renglon por cabecera
--   - Donde la partida tiene stock >0 en STR al mismo tiempo
--
-- Se BORRA y no se compensa con un SAL: son comprobantes que nunca existieron
-- fisicamente, no un movimiento a revertir. Mismo criterio que
-- sql/ops/bejerman_borrar_ent_duplicados_20260608.sql (MG 6522 / MG 7206).
--
-- OJO CON LOS MILISEGUNDOS: FecMod se escribe truncado al segundo o la fila
-- queda bloqueada para el importador. Ver bejerman_fix_fecmod_milisegundos.sql.
--
-- Reversion: sql/ops/rollback_bejerman_borrar_ent_fantasma_backfill_stl.sql
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
  (14001, 99427, N'18433452',      N'1112002', N'STL', N'NEXORA-OS-29291-ENT-STL'),
  (14003, 99429, N'20663266',      N'1109010', N'STL', N'NEXORA-OS-28022-ENT-STL'),
  (14010, 99436, N'N5136790',      N'1115007', N'STL', N'NEXORA-OS-26082-ENT-STL'),
  (14015, 99441, N'N5158116',      N'1115007', N'STL', N'NEXORA-OS-20525-ENT-STL'),
  (14017, 99443, N'573',           N'1117001', N'STL', N'NEXORA-OS-26398-ENT-STL'),
  (14026, 99452, N'BJD16B41002',   N'1111002', N'STL', N'NEXORA-OS-29288-ENT-STL'),
  (14027, 99453, N'BJD18711077',   N'1111002', N'STL', N'NEXORA-OS-20384-ENT-STL'),
  (14028, 99454, N'BTX16B57007',   N'1104021', N'STL', N'NEXORA-OS-29287-ENT-STL'),
  (14029, 99455, N'BTX18417006',   N'1104021', N'STL', N'NEXORA-OS-28453-ENT-STL'),
  (14030, 99456, N'BTX18417150',   N'1104021', N'STL', N'NEXORA-OS-28422-ENT-STL'),
  (14038, 99464, N'CJD17211096',   N'1111002', N'STL', N'NEXORA-OS-22489-ENT-STL'),
  (14043, 99469, N'CTX1799J117',   N'1105005', N'STL', N'NEXORA-OS-28338-ENT-STL'),
  (14075, 99501, N'H2120301133',   N'1111001', N'STL', N'NEXORA-OS-26722-ENT-STL'),
  (14076, 99502, N'H2120301186',   N'1111001', N'STL', N'NEXORA-OS-26826-ENT-STL'),
  (14091, 99517, N'MA23060040035', N'1101002', N'STL', N'NEXORA-OS-29277-ENT-STL'),
  (14107, 99533, N'MZJ5S121710',   N'1115003', N'STL', N'NEXORA-OS-29264-ENT-STL'),
  (14131, 99557, N'T0017276',      N'1113004', N'STL', N'NEXORA-OS-27492-ENT-STL'),
  (14134, 99560, N'T9022575',      N'1113004', N'STL', N'NEXORA-OS-28054-ENT-STL');

-- ---------- PREFLIGHT ----------
-- Cada fila objetivo tiene que ser exactamente lo que se describe arriba: ENT de
-- +1, del 08/06/2026, con ese IdOrigen, ese deposito, esa partida y ese articulo,
-- y con un unico renglon en la cabecera.
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

-- Detalle por fila para poder revisar
SELECT o.partida, o.articulo, o.deposito, o.cms_ID, o.mst_ID, o.idorigen,
       stockpar_actual = (SELECT sp.stp_CantUM1 FROM dbo.StockPar sp
                           WHERE LTRIM(RTRIM(sp.stp_Partida)) = o.partida
                             AND LTRIM(RTRIM(sp.stpart_CodGen)) = o.articulo
                             AND LTRIM(RTRIM(sp.stpdep_Cod)) = o.deposito),
       stock_agregado_actual = (SELECT st.stk_CantUM1 FROM dbo.Stock st
                                 WHERE LTRIM(RTRIM(st.stkart_CodGen)) = o.articulo
                                   AND LTRIM(RTRIM(st.stkdep_Cod)) = o.deposito)
  FROM @objetivo o;

SELECT filas_objetivo_validadas = @okFilas, esperadas = 18,
       quedarian_negativas_stockpar = @negSp, quedarian_negativas_stock = @negSt,
       descuadres_previos = @descuadre;

IF @okFilas <> 18 OR @negSp <> 0 OR @negSt <> 0 OR @descuadre <> 0
BEGIN
  DECLARE @msg NVARCHAR(400) =
      N'Preflight inesperado. Filas validadas=' + CONVERT(NVARCHAR(10), @okFilas)
    + N' de 18; negativas StockPar=' + CONVERT(NVARCHAR(10), @negSp)
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

  IF OBJECT_ID('dbo.NEXORA_BKP_20260907_BKFILL18_CabMovS','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_BKFILL18_MovStock','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_BKFILL18_CabMovSImport','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_BKFILL18_StockPar','U') IS NOT NULL
     OR OBJECT_ID('dbo.NEXORA_BKP_20260907_BKFILL18_Stock','U') IS NOT NULL
  BEGIN
    RAISERROR('Ya existen tablas NEXORA_BKP_20260907_BKFILL18_*. Revisar antes de continuar.', 16, 1);
  END;

  SELECT c.* INTO dbo.NEXORA_BKP_20260907_BKFILL18_CabMovS
    FROM dbo.CabMovS c WHERE c.cms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT m.* INTO dbo.NEXORA_BKP_20260907_BKFILL18_MovStock
    FROM dbo.MovStock m WHERE m.mstcms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT i.* INTO dbo.NEXORA_BKP_20260907_BKFILL18_CabMovSImport
    FROM dbo.CabMovSImport i WHERE i.csicms_ID IN (SELECT cms_ID FROM @objetivo);
  SELECT sp.* INTO dbo.NEXORA_BKP_20260907_BKFILL18_StockPar
    FROM dbo.StockPar sp WHERE LTRIM(RTRIM(sp.stp_Partida)) IN (SELECT partida FROM @objetivo);
  SELECT st.* INTO dbo.NEXORA_BKP_20260907_BKFILL18_Stock
    FROM dbo.Stock st WHERE LTRIM(RTRIM(st.stkart_CodGen)) IN (SELECT articulo FROM @objetivo);

  -- Borrar comprobantes fantasma
  DELETE FROM dbo.MovStock      WHERE mstcms_ID IN (SELECT cms_ID FROM @objetivo);
  IF @@ROWCOUNT <> 18 RAISERROR('MovStock: no se borraron exactamente 18 renglones.', 16, 1);

  DELETE FROM dbo.CabMovSImport WHERE csicms_ID IN (SELECT cms_ID FROM @objetivo);
  DELETE FROM dbo.CabMovS       WHERE cms_ID    IN (SELECT cms_ID FROM @objetivo);
  IF @@ROWCOUNT <> 18 RAISERROR('CabMovS: no se borraron exactamente 18 cabeceras.', 16, 1);

  -- Descontar saldos por partida (cada partida se resta 1, no hay dos iguales)
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
  IF @@ROWCOUNT <> 18 RAISERROR('StockPar: no se actualizaron exactamente 18 filas.', 16, 1);

  -- Descontar saldos agregados por articulo+deposito
  DECLARE @nArt int = (SELECT COUNT(DISTINCT articulo + '|' + deposito) FROM @objetivo);
  UPDATE st
     SET st.stk_CantUM1 = st.stk_CantUM1 - d.baja,
         st.stk_CantUM2 = st.stk_CantUM2 - d.baja,
         st.stk_FecMod  = @Ahora, st.stkusu_Codigo = @Usu
    FROM dbo.Stock st
    JOIN (SELECT articulo, deposito, COUNT(*) AS baja
            FROM @objetivo GROUP BY articulo, deposito) d
      ON LTRIM(RTRIM(st.stkart_CodGen)) = d.articulo
     AND LTRIM(RTRIM(st.stkdep_Cod))    = d.deposito;
  IF @@ROWCOUNT <> @nArt
  BEGIN
    DECLARE @msgArt NVARCHAR(200) = N'Stock: se actualizaron ' + CONVERT(NVARCHAR(10), @@ROWCOUNT)
      + N' filas pero se esperaban ' + CONVERT(NVARCHAR(10), @nArt) + N'.';
    RAISERROR(@msgArt, 16, 1);
  END;

COMMIT TRANSACTION;
PRINT 'APLICADO: borrados los 18 ENT fantasma del backfill 08/06/2026 a STL.';
