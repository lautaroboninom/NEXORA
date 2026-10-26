# Carga técnica y presupuestos por horas

La rama `feature/presupuestos-carga-tecnica` contiene este desarrollo separado. La emisión requiere revisión humana.

## Entorno de desarrollo

```powershell
docker compose -f docker-compose.technical-dev.yml up -d --build
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py apply_technical_interventions_schema
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py seed_technical_dev_preview --password 'CargaTecnica-demo-2026!'
```

La interfaz está en http://localhost:5177 y la API en http://localhost:18110. Los usuarios de demostración son `presupuestos@tecnica.invalid` y `tecnico@tecnica.invalid`, con la contraseña elegida al ejecutar el comando. El seed de demostración incluye clientes, equipos, repuestos y stock ficticios. Las tarifas y los precios completos confirmados por el negocio se instalan con el esquema, mediante un seed separado y versionado. La base `nexora_technical_dev`, su volumen y sus contenedores son exclusivos de este trabajo. No se usan `.env.prod`, trabajadores de correo ni sincronizadores externos. El comando de demostración rechaza otras bases de datos y no reemplaza órdenes ni valores ya cargados.

Las imágenes incorporan el código y el esquema: después de modificar archivos, reconstruir el servicio correspondiente. Esta configuración evita montajes de archivos desde rutas UNC, que Docker Desktop puede interpretar como directorios.

## Casos disponibles para probar

La carga ampliada agrega veinticuatro órdenes, tres clientes y trece repuestos con códigos `DEMO-…`. Conserva la OS 1 y los cambios que ya se hayan realizado: la base actual tiene 25 órdenes. Los números de OS de la tabla corresponden a la base de demostración actual; en una instalación nueva el comando informa sus identificadores. Los ejemplos activos se ubican en Taller para aparecer en las listas de trabajo.

| OS | Caso |
| --- | --- |
| 2 | Ingreso nuevo para diagnosticar y cargar las listas |
| 3 | Ingreso sin técnico asignado |
| 4 | Carga técnica en preparación, aún sin enviar |
| 5 | Revisión de 30 minutos: mínimo de una hora |
| 6 | Revisión de 1 h 20 min, con dos repuestos |
| 7 | Revisión de 2 h 30 min, con turbina y manguera |
| 8 | Solicitud de alta de repuesto pendiente |
| 9 | Solicitud de alta de trabajo pendiente |
| 10 | Sin falla: tarifa general de diagnóstico y limpieza |
| 11 | Sin falla con diagnóstico y limpieza bonificados |
| 12 | Tipo de equipo sin tarifa: emisión bloqueada |
| 13 | Presupuesto emitido, listo para aprobar |
| 14 | Aprobado: completar los trabajos y repuestos |
| 15 | Reparación terminada |
| 16 | Adicional enviado sobre una versión aprobada |
| 17 | CPAP que no enciende, pendiente de diagnóstico |
| 18 | Aspirador con baja succión, pendiente de diagnóstico |
| 19 | Monitor con lectura intermitente, pendiente de diagnóstico |
| 20 | Nebulizador que no nebuliza, pendiente de diagnóstico |
| 21 | Concentrador con alarma, pendiente de diagnóstico |
| 22 | Polywatch: cánula nasal, reparación completa por $35.000 |
| 23 | Polywatch: pila, reparación completa por $30.000 |
| 24 | Polywatch: pila y software, reparación completa por $40.000 |
| 25 | Compresor: reparación completa por $140.000 |

El técnico puede comenzar en http://localhost:5177/ingresos/2?tab=diagnostico. Administración puede comenzar en http://localhost:5177/pendientes-presupuesto o revisar la OS 6. La preparación instala la [tabla aportada por el negocio con el 34 % confirmado](tarifas-servicio-tecnico-20261016.md), sin depender de cuentas de demostración. El diagnóstico sin falla conserva la configuración administrativa existente. Repetir el seed de demostración no reinicia los casos, no repone stock consumido ni restaura tarifas de ejemplo cuando hay tarifas de tres tramos.

Para empezar desde la lista de trabajo, ingresar como `tecnico@tecnica.invalid` en http://localhost:5177/tecnico. Las OS 17 a 21 tienen diagnóstico vacío y están asignadas a ese técnico. Administración puede verlas en http://localhost:5177/pendientes; «Mis pendientes» muestra únicamente las asignadas al usuario que inició sesión.

## Operación

1. El técnico asignado abre Diagnóstico y Reparación y completa diagnóstico, trabajos y repuestos de catálogo, una cantidad por artículo y un único campo «Mano de obra (horas)». Admite decimales con coma, por ejemplo `0,5`, `1,25` o `1,5`. Marca «Realizado» en los trabajos terminados e «Instalado» en las piezas colocadas. Guarda sus cambios y confirma «Enviar a presupuestar».
   Guardar conserva el estado de la orden. El pase inicial a «Diagnosticado» y su fecha automática se registran únicamente al completar el envío, después de las validaciones y de preparar el borrador. Editar los campos de diagnóstico o fecha desde la ficha general tampoco adelanta el estado. Se conservan las fechas cargadas manualmente y los estados posteriores o las resoluciones especiales.
   Cuando el envío termina correctamente, el botón queda gris, deshabilitado y con el texto «Enviado a presupuestar». Se vuelve a habilitar al modificar la carga; si el envío falla, permite reintentarlo.
   El selector de repuestos muestra código, descripción y equipos asociados cuando están configurados. Al seleccionar uno se puede consultar la lista completa de marcas, modelos y variantes asociadas. Esta información se obtiene del mismo catálogo administrativo y no incluye importes.
2. Si falta un artículo, solicita su alta. Administración lo crea o identifica en el catálogo y resuelve la solicitud; el artículo aprobado se agrega a la intervención. Las solicitudes pendientes impiden completar el envío.
3. En Pendientes de presupuesto se distingue la preparación, las altas pendientes, la revisión y las ampliaciones. Desde «Trabajos, tarifas y solicitudes de alta» se administran los catálogos y los valores monetarios.
4. Administración revisa los precios de los repuestos y el tiempo facturable. La lista técnica define los artículos; los cambios de alcance se hacen allí y se vuelven a enviar.
5. Una vez emitida, la versión queda cerrada y se conserva el PDF original. Marcar trabajos como realizados o repuestos como instalados no modifica lo cotizado. Un cambio de horas pasa por revisión en una nueva versión.
   «Ver/Descargar PDF» abre el documento desde su dirección autenticada y propone como nombre el título de esa versión, por ejemplo `OS 00021-1 Centro Respiratorio del Sur · DEMO.pdf`. Conserva tildes y punto medio, retirando solo caracteres incompatibles con nombres de archivo. Los PDF anteriores toman el título guardado en el propio documento, sin regenerar sus bytes ni usar el nombre actual de un cliente que haya cambiado.
6. Para un adicional, el técnico modifica el alcance y vuelve a enviarlo. Administración crea una nueva versión, la revisa y la emite. La aprobación sustituye la anterior y aplica solamente la diferencia de stock. El Portal Cliente utiliza la misma regla.

## Cálculo

Cada tarifa tiene primera hora, valor de la segunda a la sexta y valor desde la séptima, en pesos netos. Se cobra un mínimo de una hora y luego el tiempo proporcional del tramo correspondiente. Por ejemplo, `1,5` horas corresponde a la primera hora más la mitad de la segunda; `6,5` horas corresponde a la primera hora, cinco horas del segundo tramo y media hora del tercero. Una tarifa configurada por marca tiene prioridad sobre la del tipo de equipo, como sucede con Inogen. Internamente se conserva el almacenamiento en minutos enteros y el resultado monetario se redondea a centavos. Se conserva el tratamiento de IVA existente.

La modalidad sin falla utiliza una tarifa general de diagnóstico y limpieza, común a todos los equipos; administración puede bonificarla por completo. Los técnicos no reciben importes en los recursos de intervención ni de selección de repuestos. Si falta una tarifa, se permite enviar la carga técnica y se bloquea la emisión hasta completarla y actualizar el cálculo.

Los trabajos con precio final configurado cubren la reparación completa: se cobran una sola vez e incluyen todos los repuestos y la mano de obra de la orden. Las horas se registran sin agregarse al importe. Administración puede elegir y ajustar el precio completo antes de emitir; las piezas siguen detalladas para el stock. Al volver al cobro por horas se recuperan sus precios individuales. Cuando se seleccionan varios trabajos con precio final, la emisión exige revisar expresamente el modo de cobro.

La acción existente «Controlado sin defecto» también activa esta modalidad. Un artículo desactivado del catálogo conserva su ejecución en las intervenciones donde ya estaba seleccionado.

Las versiones guardan las horas de mano de obra, las tarifas y el detalle técnico utilizados. Las órdenes anteriores conservan su texto y sus datos históricos.

## Verificación

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_technical_dev.ps1
docker compose -f docker-compose.technical-dev.yml exec -T web npm run build
```

Las pruebas se ejecutan en `test_nexora_technical_dev`, clonada exclusivamente desde la base aislada. El script detiene temporalmente la API de esa configuración para permitir la clonación y luego vuelve a iniciarla. No ejecutar estas pruebas con configuraciones productivas.

La [validación del 15/10/2026](validacion-carga-tecnica-20261015.md) detalla las pruebas ejecutadas y el estado de la demostración.
El [ajuste del 16/10/2026](validacion-carga-tecnica-20261016.md) verifica el campo único de horas con coma decimal.

## Informe de trabajos del último año

El comando `python manage.py report_technical_work_catalog` realiza una transacción de solo lectura y produce CSV con tipo de equipo, texto original y frecuencia. Utiliza fecha de servicio, con fecha de ingreso como respaldo, durante los últimos 365 días. No agrega trabajos automáticamente ni modifica las órdenes históricas.

El informe definitivo debe ejecutarse sobre el histórico real o una copia autorizada de ese histórico. Los datos ficticios del entorno técnico no sirven para definir el catálogo. Los textos requieren revisión para separar tareas generales de cambios de piezas y unificar variantes de escritura.

Se realizó la consulta real del [último año en SERVERDATA](reportes/carga_tecnica_trabajos_ultimo_ano_20261015.md). El parámetro `--proposals` genera las propuestas agrupadas por tipo de equipo. Las propuestas requieren revisión antes de ampliar el catálogo inicial.

## Esquema

`apply_technical_interventions_schema` es idempotente y está incluido en `prepare_production_schema`. El mismo DDL figura en `sql/schema.sql`. Incluye auditoría de alcance, cantidades, tiempos, altas y tarifas. Los documentos emitidos se almacenan aparte para conservar los bytes originales y evitar duplicar contenido binario en la auditoría de cambios.

También instala los datos de negocio iniciales mediante el seed `technical_pricing_20261016_v1`, registrado en `technical_data_seeds`. Incluye los once grupos de horas con sus tres tramos y los cuatro precios completos de Polywatch y compresores. Se ejecuta una sola vez en instalaciones y actualizaciones; conserva configuraciones monetarias previas y no repone valores que administración retire después. No crea usuarios, órdenes, repuestos ni movimientos de stock. La [guía del futuro despliegue](despliegue-carga-tecnica.md) describe esta separación y la comprobación de la carga.
