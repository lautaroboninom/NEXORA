# Campo único de mano de obra en horas

El 16/10/2026 se reemplazaron los campos de tiempo estimado y real de la carga técnica por «Mano de obra (horas)». La revisión administrativa utiliza el mismo formato y los nuevos conceptos de mano de obra expresan su duración en horas decimales con coma. Los documentos emitidos anteriormente se conservan.

El campo admite coma o punto decimal, por ejemplo `0,5`, `1,25` y `1,5`. La entrada se convierte al almacenamiento existente de minutos enteros, redondeando al minuto más cercano; no se modificó el esquema. Los datos de tiempo real anteriores se conservan como históricos y la pantalla ya no los vuelve a enviar al guardar.

Se verificó exclusivamente en el proyecto `nexora-technical-dev`:

- Tres pruebas de conversión de horas aprobadas, con valores válidos e inválidos, límites y conservación de las duraciones anteriores durante un ciclo de abrir y guardar.
- 43 pruebas focalizadas de intervención técnica, cálculos y PDF aprobadas en `test_nexora_technical_dev`. La nueva regresión comprueba `1,5` horas, el importe proporcional y la conservación del dato histórico al modificar únicamente la mano de obra.
- Compilación del frontend correcta, con los avisos existentes de tamaño de paquetes y dependencias.
- Revisión en Chromium del campo único, la carga con coma y punto, el bloqueo de entradas inválidas y el uso del mismo formato en administración. Se inspeccionaron las solicitudes con respuestas simuladas, sin modificar las órdenes de demostración. No se registraron errores de JavaScript.

No se utilizaron la configuración de producción, su correo ni sus sincronizaciones.

## Estado visual del envío a presupuestar

Tras un envío exitoso, el botón queda gris y deshabilitado, con el texto «Enviado a presupuestar». El estado se conserva al recargar, utilizando la revisión técnica enviada. Modificar la carga habilita el reenvío y un fallo de red no la marca como enviada.

Se verificaron en Chromium el estado gris, la recarga, la edición y el reenvío, además de un error de envío simulado seguido de un reintento. La comprobación utilizó respuestas simuladas para conservar las órdenes de demostración. La compilación del frontend finalizó correctamente.

## Tabla de tarifas aportada por el negocio

Se incorporó la [tabla de horas con el aumento confirmado del 34 %](tarifas-servicio-tecnico-20261016.md). El cálculo utiliza primera hora, segunda a sexta y séptima en adelante. Se agregó una tarifa por marca con prioridad sobre el tipo para Inogen.

Se ejecutó `apply_technical_interventions_schema` en desarrollo y en su base aislada de pruebas, y se verificaron las columnas `marca` y `hora_desde_septima` y el índice único de marca. El DDL coincide con `sql/schema.sql`. Se cargaron los once grupos de la hoja en 33 referencias de tipos o marca y se actualizaron los borradores vigentes enviados a revisión.

Pasaron 47 pruebas focalizadas de intervención, cálculos y PDF. Se probaron los límites de seis y siete horas, minutos proporcionales, compatibilidad con tarifas anteriores, prioridad de marca, rechazo de marcas duplicadas y conservación de una versión emitida después de cambiar las tarifas. La compilación del frontend y la revisión visual de los tres tramos y de Inogen en administración finalizaron correctamente, sin errores de JavaScript.

Se repitió la importación y se confirmó que no acumula el 34 %. También se repitió el seed y se verificó que conserva las tarifas confirmadas. Los cinco PDF emitidos de demostración mantuvieron sus hashes. Las consultas al catálogo de SERVERDATA se realizaron dentro de transacciones de solo lectura; no hubo cambios ni pruebas en producción.

## Precios finales de reparaciones completas

El usuario confirmó que Polywatch y compresores incluyen los repuestos. Se cargaron $35.000, $30.000, $40.000 y $140.000, sin aumento, mediante `load_technical_fixed_prices --apply`. Administración puede configurar y revisar estos precios completos; los técnicos solo reciben nombres y datos técnicos.

Se ejecutó `apply_technical_interventions_schema` en desarrollo y en la base de pruebas. Se verificaron en PostgreSQL el precio opcional del catálogo de trabajos y los dos campos de artículos de presupuesto que identifican el cargo fijo y conservan el precio separado. El DDL se reflejó también en `sql/schema.sql`.

Pasaron 55 pruebas focalizadas. La cobertura nueva comprueba cobro único sin sumar horas o repuestos, preservación de costos, precios individuales recuperables al volver a horas, permisos y respuestas técnicas, precio congelado, ampliaciones con diferencias de stock y documentos anteriores, selección explícita ante varios trabajos completos y texto de inclusión en el PDF.

La carga de precios y el seed se repitieron sin duplicados ni alteraciones de los cinco PDF anteriores. Se agregaron cuatro casos en revisión, OS 22–25, con piezas de demostración. La compilación del frontend finalizó correctamente.

La revisión en Chromium verificó los cuatro importes, la edición y persistencia del precio completo, los campos de piezas incluidos deshabilitados y la ausencia de importes en la vista y el catálogo del técnico. Se inspeccionaron las capturas de escritorio y móvil, sin errores de JavaScript.

## Seed de negocio para el futuro despliegue

Se incorporó `technical_pricing_20261016_v1` al SQL aplicado por `apply_technical_interventions_schema` y a `sql/schema.sql`. El mecanismo de preparación existente lo ejecutará en instalaciones y actualizaciones, sin depender de cuentas ni comandos de demostración. El seed inicial conserva las tarifas y los importes previamente configurados; su registro impide que posteriores reinicios restauren precios retirados o valores anteriores a una edición administrativa.

Se ejecutó el apply en desarrollo y en su base de pruebas. Se comprobó la existencia de `technical_data_seeds`, sus columnas y el registro persistido. Tras recrear la API y volver a preparar la base, el registro mantuvo su fecha y se conservaron las 33 tarifas, los cuatro precios completos, las 25 órdenes y la configuración de diagnóstico y limpieza existente.

Pasaron 57 pruebas de seed, preparación e intervenciones. Las pruebas nuevas comparan los valores iniciales con la tabla confirmada, comprueban la creación de los cuatro trabajos, conservan configuraciones previas y una tarifa Inogen con referencia propia, y verifican que dos reaplicaciones respetan importes editados, precios vacíos, bajas de tarifas y trabajos desactivados. También se comprobó que el seed no crea usuarios u órdenes ni cambia stock o documentos emitidos. El SQL de instalación y actualización coincide. No se ejecutaron comandos ni pruebas en producción.
