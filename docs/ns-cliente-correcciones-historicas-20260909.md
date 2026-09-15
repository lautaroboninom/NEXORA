# NS x cliente: correcciones históricas del 09/09/2026

## Resultado final en producción

Base consultada y corregida: `SBDASEP` (SEPID real). Se reprodujo el SQL de
la vista de Bejerman, incluidos sus joins y filtros; no se verificó mediante
una captura nueva de la interfaz. Al refrescar la vista debe leerse esta base.

| Cliente | Serie comercial final | Saldo final | Estado |
| --- | --- | ---: | --- |
| DIA | 5143451 | 0 | Conciliado con la salida original, según instrucción del usuario |
| NATIVA | MZJ2P00005 | 0 | Salida y devolución con numeración correcta |
| NATIVA | MZJ2P00078 | 0 | Salida y devolución con numeración correcta |
| NATIVA | MZJ2P00096 | -1 | Numeración corregida; pendiente conciliar una salida y dos devoluciones |
| INTEG | MZJ5S117719 | -1 | Pendiente identificar la entrega original |

## Cambios efectuados y aclaración del usuario

Se hicieron dos transacciones autorizadas, ambas con respaldo previo,
guardas de coincidencia de registros, rollback ante error y comparación de
la consulta completa antes del COMMIT.

1. A las 11:19:00 -03:00 se cambió sólo `SegDetV.sdvstp_Partida` en las
   devoluciones 111088, 110836 y 110843 para usar la escritura de la salida.
   Diagnostic pasó de `N5143451` a `5143451`; NATIVA pasó temporalmente de
   `MZJ2P00005/78` a `MZJP00005/78`.
2. El usuario aclaró que en NATIVA **la escritura correcta es MZJ2P**, y que
   la omisión del `2` había sido un error histórico. A las 11:21:51 -03:00
   se corrigieron los seis renglones de NATIVA de la siguiente tabla. Esta
   segunda etapa sustituye el criterio inicial para NATIVA: no deben
   conservarse las series comerciales sin `2` en estos alquileres.

| sdv_ID | Comprobante | Cantidad | Serie final |
| ---: | --- | ---: | --- |
| 46171 | RTN R00001-00024353 | 1 | MZJ2P00005 |
| 110836 | RDA R00004-00005200 | -1 | MZJ2P00005 |
| 85357 | RTN R00001-00026001 | 1 | MZJ2P00078 |
| 110843 | RDA R00004-00005200 | -1 | MZJ2P00078 |
| 47953 | RTN R00001-00024438 | 1 | MZJ2P00096 |
| 95207 | RDA R00004-00004246 | -1 | MZJ2P00096 |

Diagnostic conserva el cambio de la devolución `sdv_ID=111088`,
RDA R00004-00005224, a `5143451`. No se alteraron las dos operaciones MC
del cliente TMD asociadas a la antigua escritura `MZJP00078`.

## Qué no se modificó

Únicamente se actualizaron los campos de partida comercial indicados. No
se tocaron cantidades, descripciones, `scv_ActStock`, cabeceras, vínculos a
movimientos, `StockPar` ni `MovStock`. No se emitieron, anularon ni reimprimieron
comprobantes. No se ejecutaron tests de aplicación en producción.

La verificación comparó todas las columnas de 12 filas de `SegDetV`,
11 cabeceras, 17 filas de `StockPar` y 39 de `MovStock` del alcance respaldado.
Las diferencias fueron exclusivamente las partidas comerciales declaradas.
Los alias históricos del stock se mantienen: no se normalizó físicamente
todo el historial de partidas, ni se reconstruyó aquí un saldo a otra fecha.

En los tres pares conciliados las descripciones agrupaban igual en SQL,
aunque tenían espacios finales diferentes; `scv_ActStock` ya valía 1. Por
eso no se atribuyó este resultado a esos campos ni se los modificó.

## Pendientes que no deben ocultarse con una corrección arbitraria

### NATIVA MZJ2P00096

Existe una salida RTN R00001-00024438, de 11/11/2024, por +1, y dos
devoluciones por -1 con fecha de emisión 06/05/2026:

- RDA R00004-00004246: `scv_ID=16919`, `sdv_ID=95207`.
- RDA R00004-00005240: `scv_ID=19178`, `sdv_ID=111225`, leyenda OS29270 / MG7332.

Antes de corregir la escritura, el primer par ya sumaba cero bajo `MZJP00096`.
La otra devolución restaba bajo `MZJ2P00096`. Ahora las tres filas suman -1
bajo la numeración correcta. Esto acredita el desbalance de comprobantes,
no cuál corresponde anular ni el motivo por el que existen ambas bajas.
Se necesita conciliar esos dos RDA con la devolución real y cualquier entrega
intermedia antes de autorizar una anulación con sus efectos asociados.

### INTEG MZJ5S117719

Se encontró la devolución RDA R00004-00005220 del 07/09/2026,
`scv_ID=19124`, `sdv_ID=111015`, artículo 1115023, OS29709. No se encontró
una entrega con esa serie en las búsquedas realizadas sobre `SBDASEP`
y `SBDAMGBI`. La ausencia en esas búsquedas no demuestra que no haya salido
con otra identificación. Se pidió al usuario el remito o la serie original.
No se eligió otra entrega por similitud ni se cambió de artículo para forzar cero.

### Continuación: comprobación de fechas y límite de acceso

Se releyeron todos los renglones de los tres RDA pendientes. Para NATIVA,
el RDA 04246 registra `scv_FecMod=2026-05-08 10:37:19`, usuario `SPADM7`;
el RDA 05240 registra `scv_FecMod=2026-09-08 14:42:13`, usuario `SPST1`,
aunque ambos mantienen fecha de emisión 06/05/2026. La segunda fecha es
**de modificación**, no una prueba aislada de cuándo se creó el RDA ni de
por qué se emitió. Es necesario confirmar si ambos documentan una sola
devolución física. No se anuló ninguno.

El renglón de INTEG sigue referenciado a OS29709, sin interno en su leyenda.
El interno MG7085 que figura en el mismo RDA pertenece al otro equipo,
OS29710 / MC22040040037; no sirve para identificar MZJ5S117719.

No se pudo ampliar esta revisión a los registros internos de NEXORA:
el acceso SSH a SERVERDATA por IP y por nombre de Tailscale venció por
tiempo de espera, al igual que las comprobaciones de acceso TCP a PostgreSQL
en esos destinos. Esto describe el acceso desde esta sesión, no demuestra
que NEXORA esté caído. La conexión a SAMTRONIC/Bejerman siguió funcionando.
No se cambiaron servicios ni configuraciones de red.

Consulta reproducible: `sql/ops/ns_historicos_pendientes_20260909.sql`.

## EDU / MZJ5S79784 (09/09/2026)

La salida `RTN R00001-00023841` del 01/05/2024 y la devolución `RDA
R00004-00005252` del 09/09/2026 tienen la misma serie y el mismo artículo
1115005. La salida dice `Concentrador de oxígeno Longfian JAY-5Q`; la
devolución había quedado como `JAY-5AW` porque el maestro actual del artículo
1115005 fue renombrado, aunque el equipo de NEXORA es JAY-5Q.

Se corrigió únicamente `SegDetV.sdv_Desc` del renglón `sdv_ID=111344` a
`Concentrador de oxígeno Longfian JAY-5Q`. La consulta reproducida quedó en
saldo **0**. No se modificaron artículo maestro, serie, cantidad, `scv_ActStock`,
cabecera ni movimiento `sdvmst_ID=106943`. Verificación:
`docs/reportes/ns_edu_jay5q_despues_20260909.json`.

El origen NEXORA confirmado en el código es `_apply_bejerman_article_descriptions`:
para todos los RDA reemplazaba la descripción del equipo por la descripción viva
del artículo Bejerman. Eso es incorrecto cuando un mismo código fue reutilizado
para modelos distintos a lo largo del tiempo (1115005 fue JAY-5Q y hoy es
JAY-5AW). Se corrigió el flujo en `api/service/bejerman_ris.py`: para una baja o
reparación de alquiler, la descripción del modelo físico que trae NEXORA tiene
prioridad y se conserva la consulta fresca al maestro para los demás casos.
La prueba de desarrollo cubre explícitamente el cambio de JAY-5Q a JAY-5AW y
pasó (`8 tests OK`).

> **Actualización 11/09/2026:** el renombre se corrigió de fondo. 1115005 volvió a llamarse JAY-5Q
> con todas las series 5Q, y 1115023 pasó a ser JAY-5AW con las 180 series del RT X 00001-00000470;
> también se corrigió el texto de los renglones. Ver `docs/bejerman-correccion-jay5q-jay5aw-20260911.md`.

La actualización de un registro productivo de NEXORA queda pendiente sólo si
aparece una fila histórica con el modelo equivocado: esta sesión no pudo leer la
base productiva, porque la API responde 401 sin sesión y SERVERDATA no acepta
SSH/WinRM desde esta conexión. No se falsificó el registro ni se tocó la base
local de desarrollo.

## Evidencia y mecanismos

- Respaldo inicial: `reportes/ns_historico_cinco_casos_antes_20260909.json`.
- Verificación inicial: `reportes/ns_historico_cinco_casos_despues_20260909.json`.
- Respaldo de la aclaración: `reportes/ns_historico_nativa_prefijo_antes_20260909.json`.
- Verificación final: `reportes/ns_historico_nativa_prefijo_despues_20260909.json`.
- Primera aplicación: `scripts/apply_ns_historicos_20260909.py --apply`.
- Segunda aplicación: `scripts/apply_ns_nativa_prefijo_20260909.py --apply`.

Ambos comandos son de lectura sin argumentos. La opción `--rollback`
revierte únicamente su etapa, exige el estado respaldado esperado y no se
ejecutó. Para deshacer ambas etapas, el orden es segunda y luego primera;
si hay cambios posteriores en los datos, las guardas detienen la operación.
No repetir una emisión de remito como sustituto de esta reversión acotada.
