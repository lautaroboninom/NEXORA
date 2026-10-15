# Carga técnica y presupuestos por horas

La rama `feature/presupuestos-carga-tecnica` contiene este desarrollo separado. La emisión requiere revisión humana.

## Entorno de desarrollo

```powershell
docker compose -f docker-compose.technical-dev.yml up -d --build
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py apply_technical_interventions_schema
docker compose -f docker-compose.technical-dev.yml exec -T api python manage.py seed_technical_dev_preview --password 'CargaTecnica-demo-2026!'
```

La interfaz está en http://localhost:5177 y la API en http://localhost:18110. Los usuarios de demostración son `presupuestos@tecnica.invalid` y `tecnico@tecnica.invalid`, con la contraseña elegida al ejecutar el comando. Los registros son ficticios y las tarifas quedan vacías hasta cargarlas. La base `nexora_technical_dev`, su volumen y sus contenedores son exclusivos de este trabajo. No se usan `.env.prod`, trabajadores de correo ni sincronizadores externos. El comando de demostración rechaza otras bases de datos.

Las imágenes incorporan el código y el esquema: después de modificar archivos, reconstruir el servicio correspondiente. Esta configuración evita montajes de archivos desde rutas UNC, que Docker Desktop puede interpretar como directorios.

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
