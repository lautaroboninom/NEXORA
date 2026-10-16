# Campo único de mano de obra en horas

El 16/10/2026 se reemplazaron los campos de tiempo estimado y real de la carga técnica por «Mano de obra (horas)». La revisión administrativa utiliza el mismo formato y los nuevos conceptos de mano de obra expresan su duración en horas decimales con coma. Los documentos emitidos anteriormente se conservan.

El campo admite coma o punto decimal, por ejemplo `0,5`, `1,25` y `1,5`. La entrada se convierte al almacenamiento existente de minutos enteros, redondeando al minuto más cercano; no se modificó el esquema. Los datos de tiempo real anteriores se conservan como históricos y la pantalla ya no los vuelve a enviar al guardar.

Se verificó exclusivamente en el proyecto `nexora-technical-dev`:

- Tres pruebas de conversión de horas aprobadas, con valores válidos e inválidos, límites y conservación de las duraciones anteriores durante un ciclo de abrir y guardar.
- 43 pruebas focalizadas de intervención técnica, cálculos y PDF aprobadas en `test_nexora_technical_dev`. La nueva regresión comprueba `1,5` horas, el importe proporcional y la conservación del dato histórico al modificar únicamente la mano de obra.
- Compilación del frontend correcta, con los avisos existentes de tamaño de paquetes y dependencias.
- Revisión en Chromium del campo único, la carga con coma y punto, el bloqueo de entradas inválidas y el uso del mismo formato en administración. Se inspeccionaron las solicitudes con respuestas simuladas, sin modificar las órdenes de demostración. No se registraron errores de JavaScript.

No se utilizaron la configuración de producción, su correo ni sus sincronizaciones.
