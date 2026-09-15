# Corrección histórica JAY-5Q / JAY-5AW en Bejerman (11/09/2026)

Aplicada con OK del usuario el 11/09/2026 a las 14:57 en `SBDASEP` (SEPID) y `SBDAMGBI` (MGBIO), y
en NEXORA producción a continuación. Todo lo ejecutado, los respaldos y los logs están en
`outputs/bejerman_jay5q_5aw_20260911/` (fuera de git).

## El problema

El artículo **1115005** se abrió como *Concentrador de oxígeno Longfian JAY-5Q*. En noviembre de
2024 llegaron equipos del modelo nuevo y administración le cambió el nombre a *JAY-5AW* en vez de
abrir un artículo nuevo. Desde entonces:

- los 5Q que habían salido con el nombre "JAY-5Q" volvían como "JAY-5AW";
- para salir del paso, el 17/06/2026 se creó **1115023** "JAY-5Q" y algunos 5Q empezaron a
  entrar por ahí, lo que partió la historia de cada serie en dos artículos (salía por uno y
  volvía por el otro) y descuadró los conteos.

## Cómo se identificó cada equipo

- **5AW real** = las 180 partidas del ingreso de compra **RT X 00001-00000470 del 06/11/2024**
  (Longfian, depósito SAR): `MZJ5S636567` a `MZJ5S636746`, correlativas. Es el primer comprobante
  de compra que ya dice "JAY-5AW"; la OC 00002-00000942 (03/06/2024) y la FCI 00024351 (02/07/2024)
  son de esos mismos 180 equipos, pedidos cuando el artículo todavía se llamaba JAY-5Q. En el mismo
  embarque vinieron 100 JAY-5 (`1115003`) con las series inmediatamente anteriores
  (`636467`–`636566`), sin solaparse. Lista: `outputs/.../partidas_5AW_RT_X_00001-00000470.csv`.
- **5Q real** = toda otra partida de 1115005 / 1115023 (lotes `122k` y `318k`, etc.). Los técnicos
  ya cargaban esos equipos como JAY-5Q en NEXORA desde 2020, antes del renombre.
- Los renglones de MGBIO de 2019–2023 que dicen "JAY-5AW" **no** son evidencia del modelo: todos los
  comprobantes de MGBIO anteriores a 2022 tienen `scv_FecMod` del 09–12/12/2024 (carga histórica
  hecha un mes después del renombre, que copió la descripción del maestro de ese momento).
- Renglones sin partida (facturas, NC, presupuestos, OC): se asignaron por relación entre renglones
  (`SegRelDetV`/`SegRelDetC`), por el RT que citan ("Venta según RT Nº …"), por el texto del
  presupuesto o por el cliente. Lista explícita en `scripts_generadores/gen_sql.py`.

## Qué quedó

| | SEPID | MGBIO |
|---|---|---|
| 1115005 | "Concentrador de oxígeno Longfian **JAY-5Q**", 49 unidades en stock | "… **JAY-5Q**", 0 unidades |
| 1115023 | "Concentrador de oxígeno Longfian **JAY-5AW**", 11 unidades (SAR 10, STR 1) | creado, "… **JAY-5AW**", 1 unidad (VAL) |
| Movimientos de stock movidos | 750 a 1115023, 17 a 1115005 | 91 a 1115023 |
| Stock por partida | 352 a 1115023, 10 a 1115005 (1 fusionada) | 39 a 1115023 |
| Renglones de venta / compra | 422 + 5 / 182 | 116 / 60 |
| Libros IVA (ItemVta / ItemComp) | 192 / 1 | 66 / 21 |
| Últimas compras / precios copiados | 1 / 5 | 4 / 2 |
| Texto de renglones corregido | 58 + 1 (+ 2 compra, 2 IVA) | 87 (+ 3 compra) |

Verificado después con consultas independientes: 0 partidas 5AW en 1115005 y 0 partidas 5Q en
1115023 en todas las tablas; `Stock` = suma de `StockPar`; movimientos netos = stock por partida;
sin milisegundos en `FecMod`. De paso se corrigió un descuadre previo (`Stock` 1115023/STR decía 9 y
sus partidas sumaban 10).

**NEXORA:** mapeos invertidos (modelo JAY-5Q → 1115005, JAY-5AW → 1115023, en SEPID y MGBIO); el
modelo JAY-5AW tenía tipo "Concentrador **portátil** de oxígeno" y por eso nadie podía elegirlo al
cargar un concentrador (quedó "Concentrador de oxígeno"); `MZJ5S636644` y `MZJ5S636569` pasaron a
JAY-5AW; 204 renglones del caché `bejerman_sale_items` pasaron a 1115023 con su clave recalculada.

## Reversión

Cada base tiene las tablas `NEXORA_BKP_20260911_JAY5Q_*` (filas completas previas) y hay un respaldo
local en JSON. `rollback_SEPID.sql` / `rollback_MGBIO.sql` reponen desde esas tablas y **se niegan a
correr** si hubo movimientos o comprobantes nuevos de estos artículos después de la corrección (en
ese caso un rollback ciego borraría esa actividad). Se ensayaron ida y vuelta en `SBDPSEP` /
`SBDPMGBI` con checksums idénticos.

## Para que no vuelva a pasar

1. **Regla para administración:** a un artículo que tiene stock por partida e historia **nunca** se
   le cambia el nombre por el de otro modelo. Si llega un modelo nuevo, se abre un artículo nuevo.
   Cambiar el nombre reescribe hacia atrás lo que "es" cada serie que ya salió.
2. **NEXORA ya respeta la historia de la serie:** al ingresar stock (RIS/RDA) usa el artículo en el
   que Bejerman ya tiene la partida (`_apply_bejerman_partida_articles`), y los movimientos entre
   depósitos toman el artículo del stock de la partida. El mapeo del modelo sólo decide para un
   equipo sin historia en Bejerman.
3. **Guard nuevo** (`api/service/bejerman_article_rename.py`, usado en
   `bejerman_ris._validate_article_not_renamed`): si el artículo que eligió el mapeo se llamaba como
   el modelo cuando se confirmó el mapeo y hoy ya no (p. ej. mapeo "JAY-5Q" y maestro "JAY-5AW"),
   el remito no se emite hasta revisar el mapeo (`BEJERMAN_ARTICLE_RENAMED`). Reconfirmar el mapeo
   actualiza la descripción guardada y levanta el bloqueo.

## Segunda parte: modelos mal cargados en NEXORA (mismo día)

Con Bejerman corregido como referencia, se corrigió el modelo de **102 concentradores Longfian** en
NEXORA (con OK; log y SQL en `outputs/bejerman_jay5q_5aw_20260911/modelos_nexora/`):

| Antes | Después | Equipos | Evidencia |
|---|---|---|---|
| JAY (genérico) / sin modelo | JAY-5 | 73 | Bejerman propio o lote puro |
| JAY (genérico) | JAY-5Q | 16 | ídem |
| JAY (genérico) | JAY-10 / JAY-120 | 1 / 1 | Bejerman propio |
| JAY-5 | JAY-5Q | 2 + 2 | Bejerman propio / sólo lote |
| JAY-5Q | JAY-5 | 3 + 4 | Bejerman propio / sólo lote |

**Cómo se decidió cada uno** (evidencia por equipo en `modelos_concentradores_evidencia.csv`):

- *Evidencia propia*: artículo de Bejerman de esa serie con registros **anteriores a 05/2026**. Lo
  posterior no cuenta: desde entonces NEXORA emite con el artículo de su propio mapeo (el modelo
  genérico "JAY" estaba mapeado a JAY-5), así que sería circular.
- *Lote*: Longfian numera por lote y los modelos quedan en tramos. En el lote 2019 hay un corte
  limpio: hasta `MZJ5S121973` JAY-5, desde `121975` JAY-5Q; también son puros `318548`–`318727`
  (5Q), `416921`–`416989` (JAY-5) y `636467`–`636566` (JAY-5). Una serie sin evidencia propia toma
  el modelo de sus dos vecinas con evidencia (a ≤ 200 números) sólo si son del mismo modelo y los
  modelos que NEXORA tiene cargados en ese tramo no lo contradicen (≥ 80 % de acuerdo).
- Si la evidencia propia contradice al lote, no se tocó NEXORA.

**Quedaron sin determinar 64** ("JAY" o sin modelo sin evidencia en Bejerman ni lote puro, o sin
número de serie usable) más 2 para mirar a mano (`MZJ5S122041` cargado como BMC Alto flujo y
`MZJ5S171978`): `nexora_pendientes_verificar_etiqueta.csv`. Se resuelven leyendo la etiqueta en el
próximo ingreso.

**Bejerman con series dudosas** (15, en `bejerman_series_dudosas.csv`): artículo que no coincide con
su lote (p. ej. `122009`, `122041`, `122062`, `122160` como JAY-5 dentro del lote 5Q) o serie con
movimientos en dos artículos (`121252`, `121268`, `122102`, `122104`, `122187`, `636580`, `636582`).
No se tocaron.

## Pendientes que quedaron a la vista

- `1115023` en SEPID se abrió "a mano" en 2026 y no tiene la configuración de 1115005
  (descripción reducida con el PM ANMAT `2046-33`, rubro de crédito fiscal 5 vs 7, precio base en
  otra moneda). Conviene que administración la iguale.
- Las 15 series dudosas de Bejerman de la sección anterior.
- Los repuestos siguen con nombres mezclados ("Compresor completo JAY-5Q (AW)", "Compresor
  LONGFIAN JAY-5AW"): se dejan así por decisión del usuario.
