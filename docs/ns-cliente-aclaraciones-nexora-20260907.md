# N/S por cliente: verificación de las aclaraciones de NEXORA

> **Corrección del 08/09/2026:** la comparación de textos sigue siendo válida,
> pero la reproducción simplificada de N/S usada aquí omitía filtros reales.
> El SQL recuperado exige `scv_ActStock = 1` y clasificación Equipamiento,
> además de operación Alquiler y cantidad no nula/distinta de cero.
> Se comprobaron cuatro bajas de MERNO que quedan excluidas por ese indicador,
> pese a tener cantidad -1 y descripción compatible con la entrega.
> Ver `ns-testigo-MZJ5S121193-20260908.md`. No atribuir el caso histórico MZ
> a este mecanismo sin su cabecera anterior a la corrección manual.

Fecha de consulta: 07/09/2026. Consultas de sólo lectura en SAMTRONIC.

## Pregunta y criterio de prueba

El usuario propone verificar si textos como `NEXORA-ING-000295` hacen que
Bejerman excluya los equipos del listado. El modelo identificado en el handoff
usa `SegCabV + SegDetV`, filtra `scvtop_Cod = 'ALQ'` y suma `sdv_CantUM1`,
agrupando por cliente, artículo, descripción del renglón y partida.

El filtro de operación y la agrupación por descripción cumplen funciones
distintas. Una diferencia de descripción puede separar una entrega y una baja
en dos grupos. Para probar ese mecanismo se buscan, para el mismo cliente,
artículo y partida, grupos de descripción con saldos de signos opuestos.
La consulta reproducible está en
`sql/ops/bejerman_ns_conflictos_descripcion.sql`.

También se buscaron literalmente `%NEXORA%` y `%NEXORA-ING%` en `sdv_Desc`
de todos los renglones cuya cabecera tiene operación ALQ, separados por
`sdv_TipoIt`. Esta medición no depende de las tablas de stock.

## Resultados actuales

| Medición | SEPID | MG BIO |
|---|---:|---:|
| Renglones de artículo ALQ examinados | 3.159 | 4.023 |
| Renglones ALQ con NEXORA en `sdv_Desc`, cualquier tipo de ítem | 0 | 0 |
| Renglones ALQ con NEXORA-ING en `sdv_Desc`, cualquier tipo de ítem | 0 | 0 |
| Cliente/artículo/partida con saldos de signos opuestos separados por descripción | 0 | 0 |

Una búsqueda más amplia de aclaraciones en los artículos de SEPID devolvió
nueve coincidencias por el nombre legítimo `KAIROS MX1`, que contiene `OS `;
no eran números de OS ni aclaraciones añadidas.

Ejemplo materializado: `SegCabV 18100` contiene una leyenda de recepción para
servicio técnico, tipo L, cantidad 0, y un artículo independiente
`1111002 / AJD20231157`, cantidad -1, descripción
`CALENTADOR HUMIDIFICADOR BMC G1`. La aclaración coexiste con el renglón de baja.

## Dónde se construyen los textos

- `bejerman_ris._register_stock_entry_item`: `NEXORA-ING-…` se envía en
  `Comprobante_IdOrigen` de una entrada del circuito STOCK. También genera
  `Comprobante_DescArticulo = NEXORA <partida>` y `Partida_Observaciones`.
  Esta constatación corresponde al payload construido; no es una afirmación
  sobre el nombre exacto de la columna donde el SDK guarda cada valor.
- `bejerman_sdk.legend_line`: las aclaraciones del comprobante de ventas se
  envían como ítems L con cantidad 0, sin artículo y sin partida.
- `build_service_ingress_comprobante`: el artículo se envía como ítem A;
  su descripción se obtiene por separado mediante `equipment_article_description`.

## Caso MZJ5S121748 y límites

El ingreso original fue el 14/08/2026. Su request y respuesta SDK conservados
usan cliente SIMPLE, operación ALQ, artículo 1115003, partida MZJ5S121748,
cantidad -1 y descripción `Concentrador de oxígeno Longfian JAY-5`.
La entrega anterior y la baja manual actual usan la misma descripción.

La fila original `SegCabV 18692` ya no existe. La baja manual actual es
`SegCabV 18854`. El borrado y la nueva carga fueron parte de la corrección
informada por el usuario; no se los considera causa del reclamo previo.
No se encontró una salida N/S contemporánea ni un respaldo utilizable anterior
a esa corrección. Los datos actuales no permiten reconstruir cómo estaba
materializado el comprobante original cuando cobranzas observó el error.

La medición actual no sostiene la hipótesis de las aclaraciones. Tampoco
descarta que un comprobante ya corregido hubiera tenido una descripción
distinta. No se inspeccionó nuevamente la definición guardada de todas las
variantes de la consulta del ERP: un filtro adicional de otra vista necesitaría
su propia comprobación.

## Defecto de signo encontrado en la investigación previa

Se encontraron requests históricos de RDA con cantidad positiva y respuestas
SDK que conservan ese valor. En el último recurso de recuperación de partida,
`_comprobante_import_with_partidas` aplicaba `abs()` a la cantidad.
Se corrigió para respetar el signo semántico del tipo de comprobante; siete
pruebas aisladas pasaron. La corrección permanece en el código de trabajo,
sin despliegue realizado durante esta investigación.

Esto demuestra un defecto de construcción del request. No prueba por sí solo
que el SDK haya persistido ese mismo signo en las filas históricas eliminadas.
La consulta actual de RDA/RDN/RD con operación ALQ y cantidad positiva devuelve
cero filas en ambas empresas. El caso MZ tenía cantidad negativa en su request,
por lo que ese defecto no explica su incidente.

## Siguiente comprobación útil

Para un equipo que cobranzas confirme incorrecto y todavía no corregido,
conservar la salida real de N/S con sus filtros y compararla con las líneas
ALQ del cliente/artículo/partida, incluido `sdv_Desc`, y con el request y la
respuesta originales. Eso permite distinguir una baja mal materializada de
una diferencia en la vista concreta del ERP. No corresponde quitar leyendas
ni reemitir comprobantes basándose solamente en la sospecha sobre sus textos.

## Ampliación: espacios, tildes y caracteres invisibles

Se ejecutó `python scripts/audit_ns_text_bytes.py` contra ambas empresas,
con una única conexión SSH y sólo SELECT. Se examinaron los 7.182 renglones
de artículo ALQ existentes (3.159 SEPID y 4.023 MG BIO). El script recibe los
textos codificados en hexadecimal UTF-16 para conservar los caracteres y
espacios sin depender de cómo los muestre la terminal. Compara las variantes
reales con los grupos calculados por SQL Server mediante `DENSE_RANK`.

Las columnas de descripción, artículo y partida de ambas empresas usan
`SQL_Latin1_General_CP1_CI_AS`. Comparaciones SELECT con esa misma regla dieron:

| Diferencia | SQL las considera iguales |
|---|---|
| Tres espacios normales al final | Sí |
| Mayúsculas frente a minúsculas | Sí |
| Un espacio frente a dos espacios internos | No |
| Una vocal con tilde frente a la misma vocal sin tilde | No |
| Espacio normal frente a espacio no separable U+00A0 | No |

La descripción persistida `sdv_Desc` es `varchar(50)` en SEPID. La longitud
máxima admitida por el constructor del request no debe confundirse con la
longitud de esta columna; la auditoría compara lo persistido.

En SEPID existen 21 identidades cliente/artículo/partida con más de una
descripción distinta byte por byte. En 16 de ellas SQL las agrupa igual.
Ejemplo: `RES / 1101003 / A7-0423`, entrega `SegCabV 6542` con descripción
`Aspirador portátil MOVI VAC A-550` y baja `18930` con tres espacios adicionales
al final. Ambas integran el mismo grupo y su saldo conjunto es cero.
También ocurre con `NATIVA / MZJ5S121620`: la baja `19082` tiene tres espacios
finales y cancela la entrega `6557`.

Las otras cinco identidades sí contienen cambios de descripción que SQL separa:

| Cliente | Partida | Cambio | Saldos actuales por descripción |
|---|---|---|---|
| EDU | MZJ5S122131 | JAY-5Q / JAY-5AW | 0 / 0 |
| EDU | MZJ5S122012 | JAY-5Q / JAY-5AW | 0 / +1 |
| COC | T2A18604010 | c/frecuencia / c/frec. | 0 / +1 |
| ALCLA | B3C23204279 | BPAP G3 BMC / BPAP BMC G3 | 0 / 0 |
| NOV | 07-0559-10-A | uso continuo / uso continuo/Neb. | 0 / 0 |

En ninguno queda una baja negativa separada de una entrega positiva. Los
cambios de descripción de estos cinco historiales ya aparecen en filas de
2024 o 2025. Los saldos se calculan sobre las filas existentes hoy, no sobre
una reconstrucción completa de lo que existía en aquellas fechas.

Se detectaron 30 renglones SEPID con espacios dobles internos y uno con espacio
no separable: `CAT / 1199029 / E013706`, entrega `SegCabV 14555` del 21/11/2025,
cantidad +1, texto `Electrocardiógrafo CARDIOTECNICA\u00a0RG606`. El byte `A0`
separa CARDIOTECNICA de RG606. No hay para esa identidad otra descripción ALQ
con la cual forme el conflicto propuesto. No se detectaron tabulaciones,
saltos de línea ni otros caracteres de control en los cuatro campos revisados.

En MG BIO no apareció ninguna identidad cliente/artículo/partida con
descripciones distintas byte por byte. La normalización adicional de cliente,
artículo y partida (espacios, mayúsculas, tildes y caracteres de formato) tampoco
reunió identidades que SQL mantuviera separadas en ninguna empresa.

Conclusión acotada: las diferencias invisibles existen y algunas podrían
afectar la agrupación, pero ninguna de las examinadas deja actualmente una
devolución sin cancelar por ese mecanismo. Esto no demuestra qué contenían
los comprobantes históricos eliminados ni descarta filtros adicionales en
una variante concreta de la vista del ERP.

## Control obligatorio de descripción del maestro para RDA

Por pedido del usuario, se implementó en el código de trabajo un control que
consulta `ObtenerArticulos` directamente al preparar el RDA, sin usar la caché
ni recurrir a nombres locales si la consulta falla. Toma la descripción literal
del artículo en la empresa correspondiente y la conserva sin quitar espacios,
normalizar tildes ni modificar mayúsculas. El request guarda ese texto en
`equipment.bejermanMasterDescription` (y en cada equipo de un lote).

Antes de enviar el comprobante, se compara por igualdad exacta esa descripción
con `Item_DescripArticulo`. Si no coincide, la emisión se detiene. También se
rechazan descripciones ausentes, duplicadas contradictorias, incompatibles con
la página de códigos 1252 o superiores a los 50 bytes admitidos por `sdv_Desc`.
Se verificó que la columna tiene ese tipo y capacidad en las dos empresas.
Los perfiles distintos de RDA mantienen el comportamiento anterior.

Se comparó además el catálogo SDK completo con `Articulos.art_DescGen` leído
directamente de SQL Server, conservando los caracteres mediante hexadecimal:

| Empresa | Artículos SQL | Artículos SDK | Descripciones exactamente iguales | Diferencias |
|---|---:|---:|---:|---:|
| SEPID | 1.629 | 1.629 | 1.629 | 0 |
| MG BIO | 1.225 | 1.225 | 1.225 | 0 |

El servidor declaró `text/xml; charset=utf-8`. Interpretar la respuesta desde
los bytes XML o desde el texto HTTP produjo exactamente el mismo `DatosJSON`.
La medición no encontró corrupción de caracteres en la lectura actual del
maestro. Script reproducible: `scripts/audit_bejerman_catalog_encoding.py`.
Este script sólo registra la sesión SDK, lee `ObtenerArticulos` y ejecuta SELECT;
no emite comprobantes ni modifica datos de negocio.

Validación en desarrollo: 36 pruebas de descripción, contratos de remitos y
catálogo, en contenedor efímero sin red. Incluyen la conservación de tildes,
eñes, espacios iniciales/finales/dobles, espacio U+00A0 y escape XML a través del
cuerpo HTTP UTF-8. Las fallas de catálogo impiden llegar a la emisión.

Estado: cambio implementado y validado en código; aún no desplegado. No se
emitió un RDA de prueba en producción. La comparación previa garantiza el
texto que NEXORA envía; no sustituye una lectura posterior de `SegDetV` para
comprobar qué persistió Bejerman ni demuestra la causa del reclamo histórico.
