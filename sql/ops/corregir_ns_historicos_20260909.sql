-- Plantilla ejecutada exclusivamente por scripts/apply_ns_historicos_20260909.py.
-- Corrección de datos comerciales de SBDASEP autorizada el 09/09/2026.
-- No altera esquemas ni stock. Los marcadores sin completar impiden aplicarla.
SET NOCOUNT ON;
SET XACT_ABORT ON;
SET LOCK_TIMEOUT 10000;
IF DB_NAME() <> 'SBDASEP' THROW 51000, 'Base incorrecta.', 1;
BEGIN TRY
    BEGIN TRANSACTION;
    IF EXISTS (SELECT 1 FROM sys.triggers WHERE parent_id=OBJECT_ID('dbo.SegDetV') AND is_disabled=0)
        THROW 51000, 'Hay triggers: revisar sus efectos antes de modificar.', 1;
    -- {{GUARDS}}
    -- {{UPDATES}}
    ;
    -- {{REPORT}}
    SELECT TOP (0) * INTO #Esperado FROM #Actual;
    INSERT INTO #Esperado
    -- {{EXPECTED}}
    ;
    IF EXISTS (SELECT * FROM #Actual EXCEPT SELECT * FROM #Esperado)
        OR EXISTS (SELECT * FROM #Esperado EXCEPT SELECT * FROM #Actual)
        OR (SELECT COUNT(*) FROM #Actual) <> (SELECT COUNT(*) FROM #Esperado)
        THROW 51003, 'La consulta real no coincide con el resultado esperado.', 1;
    COMMIT TRANSACTION;
    SELECT 'COMMIT' AS estado, DB_NAME() AS base, SYSDATETIMEOFFSET() AS fecha_servidor,
           3 AS renglones_corregidos;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
