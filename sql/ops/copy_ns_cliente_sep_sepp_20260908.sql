-- Copia puntual de configuración de Queries. No modifica comprobantes ni stock.
-- Base: manager (catálogo compartido de Bejerman). Destino exclusivo: SEPP.
-- Vista original: 1ITEMSSEGVENTAS / 015, escenario SEP.
-- Vista nueva: 1ITEMSSEGVENTAS / 037, escenario SEPP.
-- Por defecto, sólo informa el estado. Aplicar con el script --apply asociado.
SET NOCOUNT ON;
SET XACT_ABORT ON;
SET LOCK_TIMEOUT 15000;
DECLARE @Apply bit = 0;
IF DB_NAME() <> 'manager' THROW 51000, N'La conexión debe ser a manager.', 1;

IF @Apply = 0
BEGIN
    SELECT v.avi_Cod, v.aviaes_Codigo, v.avi_Desc,
           a.arb_Cod, a.arb_CodPadre,
           (SELECT COUNT(*) FROM dbo.AsciiVistaFiltros f
            WHERE f.avfasc_Cod=v.aviasc_Cod AND f.avfavi_Cod=v.avi_Cod) AS filtros,
           (SELECT COUNT(*) FROM dbo.AsciiVistaGrilla g
            WHERE g.avgasc_Cod=v.aviasc_Cod AND g.avgavi_Cod=v.avi_Cod) AS columnas,
           (SELECT COUNT(*) FROM dbo.AsciiVistaGroup g
            WHERE g.agrasc_Cod=v.aviasc_Cod AND g.agravi_Cod=v.avi_Cod) AS agrupaciones
    FROM dbo.AsciiVista v
    LEFT JOIN dbo.AsciiArbolVista a
      ON a.arbasc_CodAVI=v.aviasc_Cod AND a.arbavi_Cod=v.avi_Cod
    WHERE v.aviasc_Cod='1ITEMSSEGVENTAS' AND v.avi_Cod IN ('015','037');
    RETURN;
END;

BEGIN TRY
    BEGIN TRANSACTION;
    IF (SELECT COUNT(*) FROM dbo.RelAsciiEscBasExt r WITH (UPDLOCK,HOLDLOCK)
        JOIN dbo.AsciiBasExt b WITH (UPDLOCK,HOLDLOCK) ON b.bex_codigo=r.esxbex_Codigo
        WHERE r.esxaes_Codigo='SEPP') <> 1
        THROW 51001, N'SEPP debe tener exactamente una base externa.', 1;
    IF NOT EXISTS (SELECT 1 FROM dbo.RelAsciiEscBasExt r
        JOIN dbo.AsciiBasExt b ON b.bex_codigo=r.esxbex_Codigo
        WHERE r.esxaes_Codigo='SEPP' AND b.bex_fullpath='SBDPSEP'
          AND b.bex_server='SAMTRONIC' AND b.bexssis_codigo='WFLEX')
        THROW 51002, N'SEPP no apunta a la base de prueba esperada.', 1;
    IF EXISTS (SELECT 1 FROM dbo.RelAsciiEscBas WITH (UPDLOCK,HOLDLOCK)
               WHERE esbaes_Codigo='SEPP')
        THROW 51003, N'SEPP contiene otras empresas. No se copia.', 1;
    IF NOT EXISTS (SELECT 1 FROM dbo.AsciiVista WITH (UPDLOCK,HOLDLOCK)
        WHERE avi_Cod='015' AND aviasc_Cod='1ITEMSSEGVENTAS'
          AND aviaes_Codigo='SEP' AND avi_Desc='NS x cliente')
        THROW 51004, N'No coincide la vista original.', 1;
    IF NOT EXISTS (SELECT 1 FROM dbo.AsciiArbolVista WITH (UPDLOCK,HOLDLOCK)
        WHERE arb_Cod=10128 AND arbavi_Cod='015'
          AND arbasc_CodAVI='1ITEMSSEGVENTAS' AND arb_CodPadre=43)
        THROW 51005, N'No coincide la ubicación original.', 1;

    DECLARE @Existing bit = 0;
    IF EXISTS (SELECT 1 FROM dbo.AsciiVista WITH (UPDLOCK,HOLDLOCK)
               WHERE aviasc_Cod='1ITEMSSEGVENTAS' AND avi_Cod='037')
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM dbo.AsciiVista
            WHERE aviasc_Cod='1ITEMSSEGVENTAS' AND avi_Cod='037'
              AND aviaes_Codigo='SEPP' AND avi_Desc='NS x cliente')
            THROW 51006, N'El código 037 ya está ocupado por otra vista.', 1;
        SET @Existing = 1;
    END;
    IF EXISTS (SELECT 1 FROM dbo.AsciiVista WITH (UPDLOCK,HOLDLOCK)
        WHERE aviaes_Codigo='SEPP' AND avi_Desc='NS x cliente'
          AND NOT (aviasc_Cod='1ITEMSSEGVENTAS' AND avi_Cod='037'))
        THROW 51007, N'Ya existe otra copia en SEPP. No se duplica.', 1;
    IF @Existing=0 AND EXISTS (SELECT 1 FROM dbo.AsciiArbolVista WITH (UPDLOCK,HOLDLOCK)
                               WHERE arb_Cod=10314)
        THROW 51008, N'El identificador de árbol previsto ya está ocupado.', 1;

    -- Sólo estas cinco tablas tenían registros propios de la vista original.
    -- Filtros y campos usan base 0 / tipo T: se resuelven por el escenario.
    DECLARE @Tables TABLE (
        pos int, tabla sysname, vista_col sysname, ascii_col sysname, esperadas int
    );
    INSERT INTO @Tables VALUES
      (1,'AsciiVista','avi_Cod','aviasc_Cod',1),
      (2,'AsciiVistaFiltros','avfavi_Cod','avfasc_Cod',4),
      (3,'AsciiVistaGrilla','avgavi_Cod','avgasc_Cod',18),
      (4,'AsciiVistaGroup','agravi_Cod','agrasc_Cod',6),
      (5,'AsciiArbolVista','arbavi_Cod','arbasc_CodAVI',1);
    DECLARE @Table sysname, @ViewCol sysname, @AsciiCol sysname, @Count int,
            @Cols nvarchar(max), @Expr nvarchar(max), @SQL nvarchar(max),
            @WhereSource nvarchar(max), @WhereDest nvarchar(max);
    DECLARE copy_cursor CURSOR LOCAL FAST_FORWARD FOR
        SELECT tabla,vista_col,ascii_col,esperadas FROM @Tables ORDER BY pos;
    OPEN copy_cursor;
    FETCH NEXT FROM copy_cursor INTO @Table,@ViewCol,@AsciiCol,@Count;
    WHILE @@FETCH_STATUS=0
    BEGIN
        IF EXISTS (SELECT 1 FROM sys.triggers
                   WHERE parent_id=OBJECT_ID('dbo.'+@Table) AND is_disabled=0)
            THROW 51009, N'Hay un trigger no contemplado en la copia.', 1;
        SELECT @Cols=STUFF((SELECT ','+QUOTENAME(c.name)
            FROM sys.columns c WHERE c.object_id=OBJECT_ID('dbo.'+@Table)
            AND c.is_identity=0 AND c.is_computed=0 ORDER BY c.column_id
            FOR XML PATH(''),TYPE).value('.','nvarchar(max)'),1,1,'');
        SELECT @Expr=STUFF((SELECT ','+
            CASE WHEN c.name=@ViewCol THEN '''037'''
                 WHEN @Table='AsciiVista' AND c.name='aviaes_Codigo' THEN '''SEPP'''
                 WHEN @Table='AsciiArbolVista' AND c.name='arb_Cod' THEN '10314'
                 ELSE QUOTENAME(c.name) END
            FROM sys.columns c WHERE c.object_id=OBJECT_ID('dbo.'+@Table)
            AND c.is_identity=0 AND c.is_computed=0 ORDER BY c.column_id
            FOR XML PATH(''),TYPE).value('.','nvarchar(max)'),1,1,'');
        SET @WhereSource=QUOTENAME(@ViewCol)+'=''015'' AND '
                         +QUOTENAME(@AsciiCol)+'=''1ITEMSSEGVENTAS''';
        SET @WhereDest=QUOTENAME(@ViewCol)+'=''037'' AND '
                       +QUOTENAME(@AsciiCol)+'=''1ITEMSSEGVENTAS''';
        SET @SQL=N'IF (SELECT COUNT(*) FROM dbo.'+QUOTENAME(@Table)
          +N' WITH (UPDLOCK,HOLDLOCK) WHERE '+@WhereSource+N') <> @n
              THROW 51010, N''Cambió la cantidad de registros originales.'', 1;
          IF @exists=0 BEGIN
            IF EXISTS (SELECT 1 FROM dbo.'+QUOTENAME(@Table)
              +N' WITH (UPDLOCK,HOLDLOCK) WHERE '+@WhereDest+N')
              THROW 51011, N''El destino contiene registros previos.'', 1;
            INSERT INTO dbo.'+QUOTENAME(@Table)+N' ('+@Cols+N')
            SELECT '+@Expr+N' FROM dbo.'+QUOTENAME(@Table)+N' WHERE '+@WhereSource+N';
          END;
          IF (SELECT COUNT(*) FROM dbo.'+QUOTENAME(@Table)+N' WHERE '+@WhereDest+N')<>@n
              THROW 51012, N''Cantidad incorrecta en la copia.'', 1;
          IF EXISTS (SELECT '+@Expr+N' FROM dbo.'+QUOTENAME(@Table)+N' WHERE '+@WhereSource
              +N' EXCEPT SELECT '+@Cols+N' FROM dbo.'+QUOTENAME(@Table)+N' WHERE '+@WhereDest+N')
             OR EXISTS (SELECT '+@Cols+N' FROM dbo.'+QUOTENAME(@Table)+N' WHERE '+@WhereDest
              +N' EXCEPT SELECT '+@Expr+N' FROM dbo.'+QUOTENAME(@Table)+N' WHERE '+@WhereSource+N')
              THROW 51013, N''La copia no coincide con el original.'', 1;';
        EXEC sys.sp_executesql @SQL,N'@n int,@exists bit',@n=@Count,@exists=@Existing;
        FETCH NEXT FROM copy_cursor INTO @Table,@ViewCol,@AsciiCol,@Count;
    END;
    CLOSE copy_cursor;
    DEALLOCATE copy_cursor;
    COMMIT TRANSACTION;
    SELECT CASE WHEN @Existing=1 THEN 'YA_EXISTIA_VERIFICADA' ELSE 'COPIA_APLICADA' END AS resultado,
           'SEPP' AS escenario,'037' AS vista,10314 AS arbol,
           'Ventas / Items Seguimiento / NS x cliente' AS ubicacion;
END TRY
BEGIN CATCH
    IF XACT_STATE()<>0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
