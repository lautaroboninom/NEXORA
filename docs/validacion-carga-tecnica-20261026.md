# Diagnóstico confirmado al enviar a presupuestar

Guardar la intervención conserva la carga en preparación y no cambia el estado de la orden ni completa automáticamente su fecha de diagnóstico. Al finalizar un envío válido, la orden pasa de ingresada a diagnosticada, conservando una fecha de servicio previa si existe. Los reintentos no alteran esa fecha y los estados posteriores o las resoluciones especiales se mantienen.

Con el esquema de carga técnica habilitado, la edición de diagnóstico, trabajos o fecha desde la ficha general tampoco adelanta el estado. El circuito anterior conserva su comportamiento cuando ese esquema no está instalado.

Se reconstruyó la API del entorno aislado `nexora-technical-dev` y pasaron las 44 pruebas de intervenciones técnicas sobre `test_nexora_technical_dev`. La cobertura comprueba guardar sin diagnosticar, confirmar el envío, repetirlo, rechazar cargas incompletas o con altas pendientes, revertir un fallo al preparar el borrador, conservar fechas y estados avanzados, y evitar que los otros campos de la ficha adelanten la transición.

No se modificó la estructura de la base ni los registros históricos. La OS 21 de desarrollo ya tenía confirmada su revisión vigente y corresponde que permanezca diagnosticada. No se ejecutaron pruebas ni cambios en producción.

## Nombre del PDF al guardar

La vista del presupuesto abre la dirección HTTP autenticada del PDF en lugar de una dirección temporal `blob:`. La respuesta indica el nombre del archivo con `Content-Disposition`, utilizando codificación UTF-8 cuando corresponde. Se conserva el título de la versión, incluido el punto medio y las tildes; únicamente se retiran caracteres incompatibles con nombres de archivo.

En documentos ya emitidos se obtiene el título de sus metadatos, con el nombre guardado como respaldo. No se regeneran los PDF ni se modifica la base histórica. Esto conserva el nombre del documento aunque luego se cambie la ficha del cliente.

Pasaron 64 pruebas focalizadas de títulos, PDF e intervenciones, incluida la preservación de los bytes de un PDF emitido y la denegación de acceso comercial a un técnico sin permiso. La primera ejecución se interrumpió por un reinicio de PostgreSQL; se repitió correctamente con la base de desarrollo disponible. La compilación del frontend finalizó correctamente.

En una sesión de prueba de Edge se abrió el PDF mediante la cookie de autenticación y se comprobó una descarga real: el navegador propuso `OS 00021-1 Centro Respiratorio del Sur · DEMO.pdf`. El hash del archivo descargado coincidió con el PDF servido y no hubo errores de JavaScript. No se ejecutaron pruebas ni cambios en producción.
