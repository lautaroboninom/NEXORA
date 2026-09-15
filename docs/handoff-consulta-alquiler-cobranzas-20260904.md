# Consulta de alquiler por cliente (Bejerman) — estado de la investigación

> **Actualización verificada del 08/09/2026:** el modelo simplificado de este
> handoff estaba incompleto. Se recuperó el SQL real de las corridas
> `SBDASEP.dbo.consEspec0182` y `consEspec4065`: además de alquiler, exige
> `scv_ActStock = 1`, cantidad distinta de cero y `Defi1Art.da1_Desc LIKE '%Equipa%'`.
> La operación se filtra por `TipoOper.top_Desc LIKE '%Alq%'`.
> Hay un caso actual comprobado: el RDA 00001-00026468 tiene cuatro bajas -1
> que el informe excluye por `scv_ActStock = 0`; incluirlas lleva sus saldos de
> +1 a 0. Esto no demuestra un error del saldo físico de stock.
> El caso de DEV `MZJ5S121193` devuelve +1 en SBDPSEP y 0 en SBDASEP.
> Ver `docs/ns-testigo-MZJ5S121193-20260908.md` y el SQL original preservado
> en `docs/reportes/ns_sql_real_recuperado_20260908.txt` antes de reutilizar
> las conclusiones históricas de este documento.

**Fecha:** 04/09/2026 · **Reclamo:** cobranzas (Ezequiel) dice que en el listado
"N/S por cliente" los equipos alquilados no aparecen correctamente desde que se
empezó a emitir e ingresar por NEXORA. Pasan las dos cosas: faltan equipos que el
cliente tiene y sobran equipos que ya devolvió.

---

## 1. Lo único 100% verificado: qué calcula la consulta

Es una **consulta específica del ERP**, no SQL suelto. Bejerman materializa cada
corrida en una tabla `dbo.consEspecNNNN` / `dbo.CONSESPECNNNN`. Se listan con:

```sql
SELECT t.name, t.create_date, p.rows
  FROM sys.tables t
  JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
 WHERE t.name LIKE 'consEspec%' OR t.name LIKE 'CONSESPEC%'
 ORDER BY t.create_date DESC;
```

**Ahí se lee la salida real que ve el usuario.** Las corridas del 03/09/2026 fueron
`consEspec7633`, `consEspec9719`, `consEspec6268` (saldo agrupado) y
`consEspec1981`, `consEspec3351` (detalle). Columnas:

```
SCVCLI_COD · SCVCLI_RAZSOC · SDVART_CODGEN · SDV_DESC · SDVSTP_PARTIDA · SDV_CANTUM1
```

Modelo:

```
origen   : SegCabV + SegDetV        <-- RENGLONES DE COMPROBANTE, no MovStock
filtro   : SegCabV.scvtop_Cod = 'ALQ'
cantidad : SUM(SegDetV.sdv_CantUM1)
agrupado : cliente + artículo + descripción del renglón + partida
```

Tres cosas que hay que tener presentes:

- **El signo está invertido respecto de stock.** En el renglón, entrega al cliente
  es `+1` y devolución `-1`. En `MovStock` una salida va en `-1`. Son convenciones
  opuestas sobre el mismo hecho.
- **Agrupa por `sdv_Desc`**, el texto guardado al emitir, no la descripción del
  maestro de artículos. Si el mismo N/S sale con dos textos distintos, se parte en
  dos renglones.
- **El tipo de comprobante no importa: la consulta no lo mira.** Lo que decide si un
  equipo aparece es el **tipo de operación**.

### Validaciones hechas

| | corrida del ERP | reproducción propia |
|---|---|---|
| detalle (renglones sueltos) | 3.122 | 3.136 |
| saldo agrupado | 2.136 | 2.140 |
| equipos en poder de clientes | 1.312 | 1.305 |

Además se validó celda a celda contra un Excel exportado por cobranzas (cliente COC,
artículos 1111001/1111002): `H2120222300`=1, `H2120301186`=0, `MG7120`=1,
`AJD20231157`=0, `BJD16B41002`=0, `BJD18541053`=1. Coincide en los 8 casos.

La diferencia de una decena de renglones es de recorte de fechas; el modelo es el
mismo.

---

## 2. Maestros relevantes

**`dbo.TipoOper`** (tipo de operación — esto es lo que filtra la consulta):

| código | descripción |
|---|---|
| **ALQ** | **Alquiler** ← el único que entra en la consulta |
| MC | Mercadería |
| REP | Reparación |
| DEMO | Demostración |
| BUSO | Vta Bien de uso |
| FAB | Fabricación |
| MKT | Marketing |

**`dbo.TipComp`** (tipo de comprobante, circuito V):

| código | descripción |
|---|---|
| RTA | Remito Alta de Alquiler |
| RDA | Remito Baja de Alquiler |
| RTN | Remito No facturable |
| RDN | Remito Devolución no facturable |
| RIS | Remito de Ingreso a Servicio Técnico |
| RSS | Remito de Salida de Servicio Técnico |
| RT / RD | Remito / Remito por devolución |

Ojo: `TipComp.tco_FecMod` es fecha de **última modificación**, no de alta. No sirve
para datar cuándo se creó un tipo (RTN figura modificado el 24/08/2026 y está en uso
desde 2022).

**Depósitos:** VAL (Valdenegro, central), SAR (Sarmiento), STR (ST a reparar),
STL (ST listos alquiler), STC (ST liberados), TER (terceros), EJE (incompletos),
MUE (muestras), DES (bajas/descarte).

---

## 3. Hipótesis DESCARTADAS (no volver a recorrerlas)

Cada una con la medición que la tumbó.

### 3.1 "Cambió la convención RTN/RDN → RTA/RDA"

Ocurrió: RTN existe desde 08/08/2022 y RDN desde 21/09/2022; RTA y RDA arrancan el
13-19/03/2026. Y el corte fue limpio — **RTN con operación `ALQ`: 34 en marzo 2026 y
cero desde abril** (se ve en `SegCabV.scvtop_Cod`).

**Por qué no explica nada:** la consulta no mira el tipo de comprobante. Un informe
llegó a reportar 830 discrepancias sobre esta hipótesis; eran **falsas**.

### 3.2 "Los RIS/RSS de taller ensucian el saldo"

Cierto si el saldo se calcula sobre `MovStock` (dan 78 renglones en −1, porque un
equipo del cliente que entra a reparar le resta una unidad que nunca recibió). **Pero
la consulta no usa `MovStock`**, así que no la afecta. Un segundo informe reportó 100
renglones sobre esta hipótesis; también falsos.

Además, los tipos RIS/RSS **ya existían antes de integrar NEXORA** (corrección del
usuario). Lo único medible es que el primer comprobante RIS es del 10/12/2025 y el
primer RSS del 21/05/2026.

### 3.3 "El punto de venta"

NEXORA numera en 00004 y el escritorio en 00001. La consulta no lo mira y la forma
del comprobante es idéntica. Irrelevante.

### 3.4 "La columna de serie viene vacía"

`sdv_Serie` viene vacía en los dos orígenes **desde siempre**. El N/S vive en
`sdvstp_Partida` y está cargado en el 100% de los renglones.

### 3.5 "El signo del renglón está invertido"

Se midió mes a mes en 2026: **todo RTA y RTN va en `+1`, todo RDA y RDN en `-1`, sin
una sola excepción**, tanto por SDK (origen E) como cargados a mano (origen R).

### 3.6 "NEXORA pone mal la operación en el remito de alquiler"

**Falso.** Los **261** comprobantes RTA/RDA de SEPID llevan operación `ALQ`, sin
excepción (142 RTA + 119 RDA).

### 3.7 "Los equipos entregados con operación MC"

Hay 99 equipos del circuito de alquiler cuyo último movimiento es una entrega con
operación distinta de ALQ. El bloque grande —NOVAMED, `RTN 00004-00004509/10/11`,
08/06/2026, 46 equipos— lleva la leyenda *"Se entrega aporte de Ricardo a Novamed"*.
**Confirmado por el usuario: no es un error.** Se alquilaron y después el cliente se
los quedó; el MC está bien puesto. El otro bloque (OXICASTHOMECARE, 03/07/2025, 34
equipos) es anterior a NEXORA y se cargó a mano.

---

## 4. La pista abierta — CON SU LIMITACIÓN

**Idea:** si un equipo está en stock en un depósito nuestro y la consulta lo muestra
en poder del cliente, la baja no se registró.

**Medición (03/09/2026):** 124 renglones cumplen las dos condiciones — 119 en SEPID y
5 en MG BIO, 33 clientes (COCUZZA 31, SIED 14, Pereletegui 13, La Vie 6, Guardis 5,
Praxair 5). Listados en:

- `docs/reportes/bajas_alquiler_no_registradas_SEPID_20260903.csv`
- `docs/reportes/bajas_alquiler_no_registradas_MGBIO_20260903.csv`
- detector: `sql/ops/bejerman_alquiler_por_cliente.sql`

Cómo volvieron esos equipos (**ninguno de estos movimientos lleva cliente**, por eso
no descuentan nada):

| movimiento | equipos |
|---|---|
| `ENT` (entrada de stock, circuito S) | 104 — 76 a STR, 24 a STL, 3 a STC, 1 a VAL |
| `TRA` (transferencia entre depósitos) | 15 |
| RT / RDN / RIS sueltos | 5 |

Atribución del último movimiento:

- **75 equipos en un solo evento**: 23/04/2026, ENT a STR, usuario `SPADM7`. Es el
  backfill de pendientes de abril.
- **17 por `SPST1`** (usuario de Servicio Técnico): 08/06 (11 a STL + 3 a STC),
  26/06 (1), 12/08 (2).
- El resto, TRA sueltas de depósito por `SPADM6` / `SPDEP`.

### ⚠️ Por qué esto NO está probado

1. **Es una comparación de estado actual contra estado actual.** Muestra una
   inconsistencia viva hoy, pero **no prueba el mecanismo causal**: no demuestra que
   sea "la baja nunca se emitió" en lugar de "el equipo volvió después y todavía no
   se cargó".
2. **No se verificó el stock histórico.** En el caso testigo `MZJ5S121748` se usó el
   stock actual (1 en STL) como evidencia, pero **el caso ya había sido corregido a
   mano**, así que ese 1 no dice nada sobre lo que veía cobranzas antes. Es circular.
   **Esto hay que rehacerlo bien.**

### Cómo verificarlo de verdad

Reconstruir el stock **a la fecha en que cobranzas vio el error**, sumando
movimientos hasta esa fecha en vez de leer `StockPar` (que es sólo el saldo de hoy):

```sql
SELECT m.mstdep_Cod, SUM(m.mst_CantUM1) AS stock_a_la_fecha
  FROM dbo.MovStock m
  JOIN dbo.CabMovS ms
    ON ms.cmsemp_Codigo = m.mstemp_Codigo
   AND ms.cmssuc_Cod    = m.mstsuc_Cod
   AND ms.cms_ID        = m.mstcms_ID
 WHERE LTRIM(RTRIM(m.mststp_Partida)) = @serie
   AND ms.cms_FComp <= @fecha_del_reclamo
 GROUP BY m.mstdep_Cod;
```

Con eso, para cada uno de los 124: ¿estaba en nuestro stock **en el momento** en que
la consulta lo mostraba alquilado? Si sí, la baja faltaba de verdad. Si no, el equipo
volvió después y no hay nada que reclamar.

---

## 5. Caso testigo

**`MZJ5S121748`** — Concentrador Longfian JAY-5, artículo 1115003.
Cobranzas lo veía en 1 con SIMPLE SALUD siendo que ya había vuelto. Se corrigió
borrando lo de NEXORA y cargando el RDA directo en Bejerman.

Renglones de comprobante (todos con operación ALQ):

| fecha | comprobante | cliente | cant | origen | usuario |
|---|---|---|---|---|---|
| 01/05/2024 | RTN 00001-00023864 | SIED | +1 | R | SPADM4 |
| 24/10/2025 | RDN 00001-00025698 | SIED | −1 | R | SPADM7 |
| 22/12/2025 | RTN 00001-00025838 | SIMPLE | +1 | R | SPADM7 |
| 14/08/2026 | RDA 00004-00005049 | SIMPLE | −1 | R | SPADM7 |

El último es **la corrección manual**. El comprobante que NEXORA había emitido fue
borrado y no queda rastro en `SegCabV`.

**Preguntas sin responder sobre este caso:**

1. ¿Qué decía el stock de esa partida **antes** del 14/08/2026? (reconstruir con la
   query de arriba)
2. ¿Qué emitió NEXORA originalmente y por qué no descontó? Eso **no está en
   Bejerman** (se borró) — está en la base de NEXORA:
   `bejerman_ingreso_remitos.request_payload`, `bejerman_integration_attempts`,
   `bejerman_remito_groups`. **Esa base no se consultó en toda la investigación.**

---

## 6. Herramientas y accesos

- **`scripts/samtronic_sql.py`** — canal directo PC local → SSH SAMTRONIC →
  PowerShell → SqlClient. Lee credenciales de `.env.prod`.
  ```
  python scripts/samtronic_sql.py --db SBDASEP --sql "SELECT ..."
  python scripts/samtronic_sql.py --db SBDAMGBI --file consulta.sql --csv salida.csv
  ```
  Bases: **SBDASEP** = SEPID, **SBDAMGBI** = MG BIO.
  Gotcha del canal: devuelve `rc=0` con el error sólo en stderr — una columna mal
  escrita parece "0 filas" en vez de fallar.
- **Runbook completo:** `docs/modificaciones-directas-bejerman-sql.md`
- **Base de NEXORA (producción):** Postgres `servicio_tecnico`, en el server `nexora`
  vía Tailscale, docker sobre WSL2. **Pendiente de consultar.**

Tablas clave de Bejerman:

| tabla | qué es |
|---|---|
| `SegCabV` / `SegDetV` / `SegTiposV` | cabecera / renglones / tipo y numeración de comprobantes de venta |
| `CabMovS` / `MovStock` | movimientos de stock (fecha `cms_FComp`, partida `mststp_Partida`) |
| `StockPar` | saldo actual por partida y depósito |
| `TipoOper` / `TipComp` | maestros de operación y de tipo de comprobante |
| `consEspec*` / `CONSESPEC*` | resultados materializados de las consultas del ERP |

---

## 7. Próximos pasos sugeridos

1. **Reconstruir el stock histórico** de `MZJ5S121748` al 13/08/2026 (día anterior a
   la corrección manual). Si estaba en un depósito nuestro, la hipótesis de "baja no
   registrada" se sostiene; si no, se cae y hay que buscar otra cosa.
2. **Repetir eso para los 124** y quedarse sólo con los que estaban en casa en el
   momento del reclamo.
3. **Mirar la base de NEXORA** para el caso testigo: qué payload se mandó, qué
   respondió Bejerman, en qué estado quedó el job. Es el único lugar donde está el
   comprobante borrado.
4. Pedirle a Ezequiel **2 o 3 casos más** que él sepa que están mal **y que todavía
   no haya corregido a mano** — sin eso no se puede verificar nada contra el estado
   real.

---

## 8. Errores cometidos en esta investigación (para no repetirlos)

- Se dieron **tres diagnósticos equivocados** antes de conseguir el modelo real de la
  consulta: cambio de convención de tipos (830 discrepancias falsas), RIS/RSS sobre
  MovStock (100 falsas) y operación MC (descartado por el usuario).
- La causa de fondo fue **teorizar sobre el reporte sin reproducirlo**. Lo que
  destrabó todo fue: (a) una captura del Excel del usuario y (b) encontrar las tablas
  `consEspec*`. **Las dos cosas debieron ser el primer paso, no el último.**
- El caso testigo se "verificó" con datos posteriores a su corrección manual, que es
  un razonamiento circular.
