# Modificaciones directas sobre las bases de Bejerman (SQL Server / SAMTRONIC)

Procedimiento para diagnosticar y **corregir datos** directamente en las bases de
Bejerman cuando el SDK no alcanza (por ejemplo, corregir un número de serie /
partida mal tipeado que ya se propagó a stock e historial).

Escrito a partir de la corrección del 06/08/2026 (299 seriales KSOC-5 de Konsung
a los que les faltaba un `4`), que se aplicó con este método sin incidentes.

---

## 0. Regla de oro: usar el camino corto

| Camino | Cuándo |
|---|---|
| **PC local → SSH SAMTRONIC → PowerShell → SqlClient** | ✅ Siempre para lotes y correcciones |
| PC local → SSH `nexora` → WSL → `docker exec` → SSH SAMTRONIC | ❌ Nunca para lotes de escritura |

El camino largo pasa por la distro WSL2 que corre **todo** producción. El
27/07/2026 un lote de 78 UPDATEs, cada uno abriendo su propia conexión SSH
anidada, dejó el WSL sin responder y tumbó producción entera.

Dos reglas que salen de ahí:

1. **Una sola conexión SSH reutilizada** para las N consultas. Nunca abrir una
   conexión por fila dentro de un loop.
2. **Un solo lote de SQL** por operación, no N sentencias sueltas.

La herramienta `scripts/samtronic_sql.py` ya implementa ambas cosas.

---

## 1. Herramienta

```python
from samtronic_sql import Samtronic, DB_SEPID, DB_MGBIO, dump, sql_literal

with Samtronic() as s:                      # UNA conexión para todo el bloque
    filas = s.query(SQL, database=DB_SEPID)
    dump(filas)
    s.execute(SQL_ESCRITURA, database=DB_SEPID)
```

Consultas rápidas de lectura desde la terminal:

```bash
python scripts/samtronic_sql.py --db SBDASEP --sql "SELECT TOP 5 * FROM dbo.Articulos"
python scripts/samtronic_sql.py --db SBDAMGBI --file consulta.sql --csv salida.csv
```

Credenciales: se leen de `.env.prod` (`BEJERMAN_SSH_HOST` / `_USER` / `_PASSWORD`).
Nunca hardcodearlas. Bases: `SBDASEP` = SEPID, `SBDAMGBI` = MGBIO, mismo
SQL Server `10.0.200.155,1433`, accesibles cruzadas entre sí.

---

## 2. El procedimiento, fase por fase

### Fase 1 — Delimitar el alcance (solo lectura)

Nunca corregir "lo que dijo el usuario"; primero medir cuánto es en realidad.
En el caso KSOC-5 el usuario reportó un equipo y eran **299**.

Clasificar por longitud y por artículo suele separar lo malo de lo bueno de
un vistazo:

```sql
SELECT LTRIM(RTRIM(sp.stp_Partida)) AS partida,
       LEN(LTRIM(RTRIM(sp.stp_Partida))) AS largo,
       RTRIM(sp.stpart_CodGen)+RTRIM(sp.stpart_CodEle1)
        +RTRIM(sp.stpart_CodEle2)+RTRIM(sp.stpart_CodEle3) AS codigo,
       RTRIM(ISNULL(a.art_DescGen,'')) AS descripcion,
       RTRIM(sp.stpdep_Cod) AS dep, sp.stp_CantUM1 AS cant
  FROM dbo.StockPar sp
  LEFT JOIN dbo.Articulos a
    ON a.art_CodGen = sp.stpart_CodGen AND a.art_CodEle1 = sp.stpart_CodEle1
   AND a.art_CodEle2 = sp.stpart_CodEle2 AND a.art_CodEle3 = sp.stpart_CodEle3
 WHERE sp.stp_Partida LIKE 'MA26%'
 ORDER BY largo, partida
```

Validar la regla de transformación **en local** contra los datos ya
descargados, antes de escribir: que reproduzca el ejemplo que dio el usuario,
que no genere destinos duplicados y que no colisione con partidas existentes.

### Fase 2 — Barrer TODAS las columnas donde vive el dato

Esto es lo que hace la diferencia entre "corregí una tabla" y "la trazabilidad
quedó consistente". Cursor sobre `INFORMATION_SCHEMA` buscando el patrón en
toda columna de texto cuyo nombre sugiera partida o serie:

```sql
SET NOCOUNT ON;
DECLARE @hits TABLE (tabla SYSNAME, columna SYSNAME, coincidencias INT, ejemplo NVARCHAR(200));
DECLARE @schema SYSNAME, @table SYSNAME, @column SYSNAME, @dyn NVARCHAR(MAX);
DECLARE @cnt INT, @sample NVARCHAR(200), @err NVARCHAR(200);

DECLARE cur CURSOR LOCAL FAST_FORWARD FOR
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME
  FROM INFORMATION_SCHEMA.COLUMNS c
  JOIN INFORMATION_SCHEMA.TABLES t
    ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
   AND t.TABLE_TYPE = 'BASE TABLE'
 WHERE c.DATA_TYPE IN ('char','varchar','nchar','nvarchar')
   AND (c.COLUMN_NAME LIKE '%Partida%' OR c.COLUMN_NAME LIKE '%Serie%');

OPEN cur;
FETCH NEXT FROM cur INTO @schema, @table, @column;
WHILE @@FETCH_STATUS = 0
BEGIN
  SET @cnt = 0; SET @sample = NULL;
  SET @dyn = N'SELECT @cnt = COUNT(1), @sample = MIN(LTRIM(RTRIM(t.' + QUOTENAME(@column)
     + N'))) FROM ' + QUOTENAME(@schema) + N'.' + QUOTENAME(@table)
     + N' t WHERE LTRIM(RTRIM(t.' + QUOTENAME(@column) + N')) LIKE ''MI_PATRON''';
  BEGIN TRY
    EXEC sp_executesql @dyn, N'@cnt INT OUTPUT, @sample NVARCHAR(200) OUTPUT', @cnt OUTPUT, @sample OUTPUT;
  END TRY
  BEGIN CATCH
    SET @cnt = -1; SET @sample = LEFT(ERROR_MESSAGE(), 180);
  END CATCH
  IF @cnt <> 0 INSERT INTO @hits VALUES (@schema + '.' + @table, @column, @cnt, @sample);
  FETCH NEXT FROM cur INTO @schema, @table, @column;
END
CLOSE cur; DEALLOCATE cur;
SELECT * FROM @hits ORDER BY coincidencias DESC;
```

**Acotar el filtro por nombre de columna.** Barrer las ~1900 columnas de texto
de la base hace un scan completo de cada tabla y es abusivo contra el SQL Server
de producción. Filtrar por `%Partida%`/`%Serie%` baja el costo a nada.

Correrlo también en la **otra empresa** (`SBDAMGBI`), no solo en la afectada.

Tablas donde vive históricamente una partida:

| Tabla | Columna | Qué es |
|---|---|---|
| `dbo.StockPar` | `stp_Partida` | Stock por partida y depósito |
| `dbo.MovStock` | `mststp_Partida` | Movimientos de stock |
| `dbo.SegDetV` | `sdvstp_Partida` | Detalle de comprobantes de **venta** |
| `dbo.SegDetC` | `sdcstp_Partida` | Detalle de comprobantes de **compra** |
| `dbo.StockPartSer` | `spsstp_Partida`, `sps_Serie` | Series por partida |

> Si `SegDetV` da 0 filas, ningún equipo se vendió ni entregó todavía: la
> corrección es un renombre de identificador y no reescribe historia comercial.
> Es el escenario más seguro posible.

### Fase 3 — Preflight + backup

El preflight tiene que dar **exactamente** lo esperado en las tres dimensiones,
si no, abortar:

```
tabla=StockPar | filas_patron=299 | del_articulo=299 | largo12=299 | colisiones=0
```

- `filas_patron == del_articulo` → no hay nada de otro artículo dentro del patrón.
- `filas_patron == largo12` → todas tienen la forma esperada.
- `colisiones == 0` → ningún destino ya existe.

Chequear colisiones con un `LEFT JOIN` contra un `SELECT DISTINCT`, **no** con
`SUM(CASE WHEN EXISTS (...))`: SQL Server rechaza agregar sobre un subquery
("Cannot perform an aggregate function on an expression containing an aggregate
or a subquery").

Backup: bajar a JSON local las filas afectadas (`partida_actual`,
`partida_nueva`, `articulo`) **antes** de escribir.

### Fase 4 — Aplicar en una sola transacción

```sql
SET XACT_ABORT ON;
BEGIN TRANSACTION;

UPDATE t
   SET stp_Partida = LEFT(LTRIM(RTRIM(t.stp_Partida)),7) + '4'
                   + SUBSTRING(LTRIM(RTRIM(t.stp_Partida)),8,5)
  FROM dbo.StockPar t
 WHERE LTRIM(RTRIM(t.stp_Partida)) LIKE 'MA26040[0-9][0-9][0-9][0-9][0-9]'
   AND LEN(LTRIM(RTRIM(t.stp_Partida))) = 12
   AND RTRIM(t.stpart_CodGen)+RTRIM(t.stpart_CodEle1)
      +RTRIM(t.stpart_CodEle2)+RTRIM(t.stpart_CodEle3) = '1115001'
   AND NOT EXISTS (
       SELECT 1 FROM dbo.StockPar x
        WHERE LTRIM(RTRIM(x.stp_Partida)) = LEFT(LTRIM(RTRIM(t.stp_Partida)),7) + '4'
                                          + SUBSTRING(LTRIM(RTRIM(t.stp_Partida)),8,5)
   );

-- ... el mismo UPDATE para MovStock y SegDetC ...

COMMIT TRANSACTION;
```

Guardas que conviene repetir siempre, aunque parezcan redundantes:

- patrón `LIKE` **sin** `%` (así fija la longitud exacta),
- `LEN(...) = N` explícito,
- filtro por **código de artículo**, para no arrastrar otro producto,
- `NOT EXISTS` contra el destino, para no violar la PK.

`SET XACT_ABORT ON` + transacción explícita garantiza que no queda nada a medio
aplicar: o entran las 897 filas o no entra ninguna.

### Fase 5 — Verificar de forma independiente

No confiar en el `rows=N` que devuelve el UPDATE. Volver a consultar:

- que queden **0** filas con el patrón viejo en *todas* las tablas del barrido,
- que el stock siga cuadrando (misma cantidad de unidades y mismo depósito),
- la **trazabilidad punta a punta** de una unidad concreta: compra → movimiento
  de stock → stock actual, las tres con el identificador nuevo.

### Fase 6 — Dejar la reversión escrita

Un script de rollback con la transformación inversa y **guardas más estrechas
que las de ida**. En el caso KSOC-5 la reversión filtra por secuencia `6####`
porque los KSOC-10 comparten el prefijo de lote `MA260404` pero usan `8####`:
una reversión ingenua les hubiera roto la partida.

---

## 3. Gotchas del canal (todos costaron tiempo al menos una vez)

1. **`rc=0` con el error solo en stderr.** Una consulta con un nombre de columna
   inexistente vuelve como **"0 filas"**, no como error. Tratar cualquier
   `stderr` no vacío como fallo — `samtronic_sql.py` ya lo hace. Sin esto se
   sacan conclusiones falsas ("no hay nada que corregir").
2. **BOM UTF-8 obligatorio** al subir el `.ps1` por SFTP. PowerShell 5.1 sin BOM
   corrompe las tildes (`á` → 2 caracteres), lo que puede desbordar un
   `varchar(50)` y tirar *"String or binary data would be truncated"*. Escribir
   en binario con `.encode("utf-8-sig")`.
3. **Columnas `float` con coma decimal.** Bejerman guarda `"1628,16"`; hay que
   pasar `,` → `.` antes de mandarlo como literal numérico, si no da
   *"Incorrect syntax near '16'"*.
4. **Blanco debe ir como `NULL`, no `''`.** En columnas con FK, mandar `''`
   viola la foreign key si `''` no es un código real de la tabla referenciada.
   Peor todavía cuando la tabla referenciada está **vacía**: ahí *cualquier*
   valor no nulo falla y sólo `NULL` pasa, porque SQL Server no valida la FK
   cuando la columna es nula. Pasó el 04/09/2026 insertando en `CabMovS`:
   *"conflicted with the FOREIGN KEY constraint `FK__CabMovS__cmsape___6DD739FB`
   ... table `dbo.Apertura`"*, con `Apertura` sin una sola fila. En esa tabla son
   **diez** las columnas en `NULL` (`cmspvt_CodIN`, `cmscli_Cod`, `cmspro_Cod`,
   `cmsape_CodIN`, `cmstrn_Cod`, `cmstop_Cod`, `cmscvi_Cod`, `cms_TransfRentas`,
   `cmscco_codigo`, `cms_NroCAE`) y tres en `MovStock` (`mstcem_Cod`,
   `mstsec_ID`, `msttpl_Cod`).

   **El volcado de una fila modelo no alcanza para distinguirlas**: el canal
   devuelve CSV vía PowerShell, donde `NULL` y `''` se ven exactamente igual.
   Para verlo hay que pedirlo explícito:
   `CASE WHEN col IS NULL THEN '<NULL>' ELSE '['+col+']' END`.

   **La forma robusta de insertar un comprobante nuevo es no tipear los valores:**
   `INSERT ... SELECT` desde una fila que Bejerman ya aceptó, pisando sólo lo que
   cambia (fecha, número, depósito). Así viajan bien hasta las columnas en las
   que uno no pensó. Ejemplo completo:
   `sql/ops/bejerman_pasar_stc_a_stl_F1121504012.sql`.
5. **Columnas `bit`**: mandar `1`/`0` sin comillas.
6. **Prefijo `N`** en todos los literales de texto: sin él, SQL Server usa el
   codepage no-Unicode de la base y las tildes se guardan como `?`.
7. **Nombres de columna reales** (no son los que uno supondría):
   - IDs: `SegCabC.scc_ID` ← `SegDetC.sdcscc_ID`; `CabMovS.cms_ID` ← `MovStock.mstcms_ID`
   - Comprobante de stock: `cmstco_Cod` (tipo), `cms_Letra`, `cms_Nro`, `cms_FComp`
   - `SegCabC` **no** tiene `sccti_Cod` ni `scc_Numero`; `CabMovS` **no** tiene `cms_Numero`
   - Comprobantes de venta: `SegCabV`/`SegTiposV`/`SegDetV` (`CabVenta` está vacía)

---

## 4. Checklist rápido

- [ ] ¿Estoy usando el camino corto (PC local → SAMTRONIC)?
- [ ] ¿Una sola conexión SSH y un solo lote de SQL?
- [ ] ¿Medí el alcance real en vez de asumir el que me reportaron?
- [ ] ¿Barrí `INFORMATION_SCHEMA` para saber en qué columnas vive el dato?
- [ ] ¿Corrí el barrido también en la otra empresa?
- [ ] ¿Validé la regla de transformación en local (sin duplicados, sin colisiones)?
- [ ] ¿Preflight limpio en las tres dimensiones?
- [ ] ¿Backup a JSON de las filas afectadas?
- [ ] ¿`SET XACT_ABORT ON` + `BEGIN/COMMIT TRANSACTION`?
- [ ] ¿Guardas por artículo, longitud y `NOT EXISTS`?
- [ ] ¿Verificación independiente (0 restantes + stock + trazabilidad de una unidad)?
- [ ] ¿Script de reversión escrito, con guardas más estrechas que las de ida?

---

## 5. Precedente reutilizable en el repo

`api/service/airsep_serial_fix.py` + `manage.py fix_airsep_serials` resuelven el
mismo tipo de problema (corregir seriales) pero corrigiendo **NEXORA y Bejerman
a la vez**, con detección de duplicados y merge de equipos. Vale leerlo antes de
escribir algo nuevo: ya tiene el generador de SQL, el barrido de columnas
`%Partida%` y el manejo de conflictos en `StockPar`.

Documentos relacionados: `docs/bejerman-test-environment.md`.
