# Diagnóstico confirmado al enviar a presupuestar

Guardar la intervención conserva la carga en preparación y no cambia el estado de la orden ni completa automáticamente su fecha de diagnóstico. Al finalizar un envío válido, la orden pasa de ingresada a diagnosticada, conservando una fecha de servicio previa si existe. Los reintentos no alteran esa fecha y los estados posteriores o las resoluciones especiales se mantienen.

Con el esquema de carga técnica habilitado, la edición de diagnóstico, trabajos o fecha desde la ficha general tampoco adelanta el estado. El circuito anterior conserva su comportamiento cuando ese esquema no está instalado.

Se reconstruyó la API del entorno aislado `nexora-technical-dev` y pasaron las 44 pruebas de intervenciones técnicas sobre `test_nexora_technical_dev`. La cobertura comprueba guardar sin diagnosticar, confirmar el envío, repetirlo, rechazar cargas incompletas o con altas pendientes, revertir un fallo al preparar el borrador, conservar fechas y estados avanzados, y evitar que los otros campos de la ficha adelanten la transición.

No se modificó la estructura de la base ni los registros históricos. La OS 21 de desarrollo ya tenía confirmada su revisión vigente y corresponde que permanezca diagnosticada. No se ejecutaron pruebas ni cambios en producción.
