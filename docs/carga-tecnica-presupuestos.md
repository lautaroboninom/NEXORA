# Carga técnica y presupuestos por horas

La rama `feature/presupuestos-carga-tecnica` contiene este desarrollo separado. La emisión requiere revisión humana.

## Entorno de desarrollo

```powershell
docker compose -f docker-compose.technical-dev.yml up -d --build
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py apply_technical_interventions_schema
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py seed_technical_dev_preview --password 'CargaTecnica-demo-2026!'
```

La interfaz está en http://localhost:5177 y la API en http://localhost:18110. Los usuarios de demostración son `presupuestos@tecnica.invalid` y `tecnico@tecnica.invalid`, con la contraseña elegida al ejecutar el comando. El seed incluye clientes, equipos, repuestos con stock y **tarifas ficticias exclusivas de prueba**. No representan la tabla de precios del negocio. La base `nexora_technical_dev`, su volumen y sus contenedores son exclusivos de este trabajo. No se usan `.env.prod`, trabajadores de correo ni sincronizadores externos. El comando de demostración rechaza otras bases de datos y no reemplaza órdenes ni valores ya cargados.

Las imágenes incorporan el código y el esquema: después de modificar archivos, reconstruir el servicio correspondiente. Esta configuración evita montajes de archivos desde rutas UNC, que Docker Desktop puede interpretar como directorios.

## Casos disponibles para probar

La carga ampliada agrega quince órdenes, tres clientes, diez repuestos con códigos `DEMO-…` y seis tipos de equipo. Conserva la OS 1 y los cambios que ya se hayan realizado. Los números de OS de la tabla corresponden a la base de demostración actual; en una instalación nueva el comando informa sus identificadores.

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
| 14 | Aprobado, con 35 minutos reales: completar ejecución |
| 15 | Reparación terminada, con 100 minutos reales |
| 16 | Adicional enviado sobre una versión aprobada |

El técnico puede comenzar en http://localhost:5177/ingresos/2?tab=diagnostico. Administración puede comenzar en http://localhost:5177/pendientes-presupuesto o revisar la OS 6. Las tarifas de ejemplo usan un mínimo por primera hora y adicionales proporcionales; el cargo general sin falla de ejemplo es de $ 6.000 netos. Repetir el seed no reinicia los casos, no repone stock consumido y no duplica presupuestos.

## Operación

1. El técnico asignado abre Diagnóstico y Reparación y completa diagnóstico, trabajos y repuestos de catálogo, cantidades previstas y realizadas, y tiempos estimado y real. Guarda sus cambios y confirma «Enviar a presupuestar».
2. Si falta un artículo, solicita su alta. Administración lo crea o identifica en el catálogo y resuelve la solicitud; el artículo aprobado se agrega a la intervención. Las solicitudes pendientes impiden completar el envío.
3. En Pendientes de presupuesto se distingue la preparación, las altas pendientes, la revisión y las ampliaciones. Desde «Trabajos, tarifas y solicitudes de alta» se administran los catálogos y los valores monetarios.
4. Administración revisa los precios de los repuestos y el tiempo facturable. La lista técnica define los artículos; los cambios de alcance se hacen allí y se vuelven a enviar.
5. Una vez emitida, la versión queda cerrada y se conserva el PDF original. El avance de ejecución y las horas reales se registran sin modificar lo cotizado.
6. Para un adicional, el técnico modifica el alcance y vuelve a enviarlo. Administración crea una nueva versión, la revisa y la emite. La aprobación sustituye la anterior y aplica solamente la diferencia de stock. El Portal Cliente utiliza la misma regla.

## Cálculo

Cada tipo de equipo tiene primera hora y hora adicional, en pesos netos. Para `m > 0` minutos se calcula `primera_hora + max(m - 60, 0) × hora_adicional / 60`, redondeando el resultado final a centavos. Se conserva el tratamiento de IVA existente.

La modalidad sin falla utiliza una tarifa general de diagnóstico y limpieza, común a todos los equipos; administración puede bonificarla por completo. Los técnicos no reciben importes en los recursos de intervención ni de selección de repuestos. Si falta una tarifa, se permite enviar la carga técnica y se bloquea la emisión hasta completarla y actualizar el cálculo.

La acción existente «Controlado sin defecto» también activa esta modalidad. Un artículo desactivado del catálogo conserva su ejecución en las intervenciones donde ya estaba seleccionado.

Los tiempos reales no recalculan automáticamente el cobro. Las versiones guardan el tiempo facturable, las tarifas y el detalle técnico utilizados. Las órdenes anteriores conservan su texto histórico.

## Verificación

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_technical_dev.ps1
docker compose -f docker-compose.technical-dev.yml exec -T web npm run build
```

Las pruebas se ejecutan en `test_nexora_technical_dev`, clonada exclusivamente desde la base aislada. El script detiene temporalmente la API de esa configuración para permitir la clonación y luego vuelve a iniciarla. No ejecutar estas pruebas con configuraciones productivas.

La [validación del 15/10/2026](validacion-carga-tecnica-20261015.md) detalla las pruebas ejecutadas y el estado de la demostración.

## Informe de trabajos del último año

El comando `python manage.py report_technical_work_catalog` realiza una transacción de solo lectura y produce CSV con tipo de equipo, texto original y frecuencia. Utiliza fecha de servicio, con fecha de ingreso como respaldo, durante los últimos 365 días. No agrega trabajos automáticamente ni modifica las órdenes históricas.

El informe definitivo debe ejecutarse sobre el histórico real o una copia autorizada de ese histórico. Los datos ficticios del entorno técnico no sirven para definir el catálogo. Los textos requieren revisión para separar tareas generales de cambios de piezas y unificar variantes de escritura.

Se realizó la consulta real del [último año en SERVERDATA](reportes/carga_tecnica_trabajos_ultimo_ano_20261015.md). El parámetro `--proposals` genera las propuestas agrupadas por tipo de equipo. Las propuestas requieren revisión antes de ampliar el catálogo inicial.

## Esquema

`apply_technical_interventions_schema` es idempotente y está incluido en `prepare_production_schema`. El mismo DDL figura en `sql/schema.sql`. Incluye auditoría de alcance, cantidades, tiempos, altas y tarifas. Los documentos emitidos se almacenan aparte para conservar los bytes originales y evitar duplicar contenido binario en la auditoría de cambios.
