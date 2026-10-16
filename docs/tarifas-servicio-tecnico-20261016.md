# Tarifas de servicio técnico recibidas el 16/10/2026

Fuente: hoja «Mano de obra para facturar servicio técnico», julio de 2024, aportada por el usuario. Se confirmó aplicar **34 % únicamente a la tabla impresa de horas**. Los valores siguientes son netos y se redondean a centavos después del aumento.

| Grupo | Primera hora | De la 2.ª a la 6.ª | Desde la 7.ª |
| --- | ---: | ---: | ---: |
| Aspiradores | $ 24.909,21 | $ 10.073,57 | $ 6.959,92 |
| Bombas (todas) | $ 29.539,39 | $ 11.428,93 | $ 9.670,63 |
| BPAP | $ 38.829,06 | $ 16.336,92 | $ 12.067,05 |
| Concentradores | $ 30.770,19 | $ 12.067,05 | $ 10.210,57 |
| CPAP | $ 30.770,19 | $ 12.067,05 | $ 10.210,57 |
| Humidificadores | $ 29.539,39 | $ 11.428,93 | $ 9.670,63 |
| Incubadoras | $ 38.829,06 | $ 16.336,92 | $ 12.067,05 |
| Monitores | $ 38.829,06 | $ 16.336,92 | $ 12.067,05 |
| Oxímetros | $ 38.829,06 | $ 16.336,92 | $ 12.067,05 |
| Respiradores | $ 47.903,06 | $ 19.165,60 | $ 14.368,73 |
| Inogen | $ 80.711,44 | $ 40.355,72 | $ 32.284,58 |

Se cobra como mínimo la primera hora. Hasta completar seis horas, el adicional se cobra con el valor del segundo tramo; el tiempo que supera las seis horas utiliza el tercero. Las fracciones se calculan proporcionalmente. Inogen se aplica por marca y tiene prioridad sobre la tarifa del tipo de equipo.

Las variantes del catálogo se asignan según `RATES` en el comando `load_technical_hourly_rates`. La categoría ambigua «CPAP/BPAP» y los tipos sin correspondencia en la hoja quedan pendientes de configuración. La hoja no indica un importe de diagnóstico y limpieza sin falla: se conserva el que ya esté configurado por administración. Solo se retiran valores conocidos del seed al pasar por primera vez a la tabla de horas confirmada; las configuraciones administrativas se conservan.

## Conceptos fijos anotados en la hoja

Estos importes no reciben el 34 %. El usuario confirmó que los tres trabajos de Polywatch y la reparación de compresores son **precios finales del trabajo completo, con repuestos incluidos**. Se cargan como precios fijos de reparación, independientes de las tarifas por hora.

| Concepto | Importe leído |
| --- | ---: |
| Polywatch: cambio de cánula nasal | $ 35.000,00 |
| Polywatch: cambio de pila | $ 30.000,00 |
| Polywatch: cambio de pila y actualización de software | $ 40.000,00 |
| Reparación de compresores en general | $ 140.000,00 |
| EverFlo: cambio de zeolita y válvula | $ 424.500,00 |

Se consultó el catálogo real en SERVERDATA en una transacción de solo lectura. Se identificó el artículo existente **1504010 — Canister ceolita EVERFLO + Válvula**, asociado a **Philips Respironics | EVERFLO**. Se conserva esa referencia para vincular el trabajo sin duplicarlo ni modificar el catálogo productivo.

EverFlo permanece como referencia del repuesto existente; no se crea otro artículo ni se cambia su precio con la carga de los cuatro trabajos completos confirmados.

## Cobro de una reparación completa

Administración configura el importe en «Precios finales por reparación» y puede ajustarlo en la revisión de cada presupuesto. Si el técnico selecciona un único trabajo con precio final configurado, el borrador propone ese cobro. Si selecciona varios, se exige elegir expresamente el precio que cubre la reparación completa o revisar el cobro por horas antes de emitir.

El precio final se cobra una sola vez por orden. Las horas se conservan como información técnica, sin sumarlas al importe; los repuestos conservan sus cantidades, referencias y costos, pero su venta individual queda incluida. No se permiten cambios de precio o costo individuales que vuelvan a agregar un cargo. Si se vuelve al cobro por horas, se recuperan los precios individuales revisados previamente.

La selección y el importe quedan congelados en cada versión emitida. Los documentos indican que la reparación incluye mano de obra y repuestos. La aprobación descuenta las piezas presupuestadas, incluso cuando su venta individual está incluida. Una ampliación aplica solamente la diferencia de stock con la versión aprobada anterior.

## Aplicación y validación en desarrollo

```powershell
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py apply_technical_interventions_schema
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py load_technical_hourly_rates --apply --refresh-drafts
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py load_technical_fixed_prices --apply
```

La importación parte siempre de los valores originales y aplica el factor 1,34 una sola vez, incluso si se repite. Actualiza borradores vigentes enviados a revisión, conservando las horas revisadas y las bonificaciones. Las versiones emitidas mantienen sus tarifas y documentos. El seed ya no repone importes de demostración cuando encuentra tarifas de tres tramos.

Se verificaron las dos columnas nuevas y su índice en PostgreSQL, la coincidencia con `sql/schema.sql`, los once grupos importados, los límites de seis y siete horas, fracciones, prioridad por marca y conservación de documentos emitidos.

Para los precios completos se verificaron `technical_work_catalog.precio_final`, `quote_items.technical_final_price` y `quote_items.precio_u_fuera_precio_final`. La carga y el seed se repitieron sin duplicar trabajos, cambiar órdenes existentes ni alterar los cinco documentos emitidos anteriores. El seed incorpora las OS 22–25 con los cuatro casos completos y piezas ficticias identificadas como DEMO; los precios finales son los confirmados por el negocio.
