# Control de presupuesto antes de liberar

Se completó el cambio pendiente del chat de Claude sobre la OS 29639. El control
anterior impedía liberar con presupuesto `presupuestado`, pero permitía una
reparación de un cliente con presupuesto `pendiente` o vacío.

La regla compartida en `api/service/release_gate.py` se usa en la emisión de la
orden, la lista de Listos para liberar y su contador. El frontend replica la regla
en el modal individual y en la clasificación del lote.

- Una reparación cobrable requiere presupuesto aprobado, rechazado o una decisión
  explícita de `no_aplica`. Pendiente, vacío, nulo, anulado o desconocido la bloquean.
- `presupuestado` y `emitido` bloquean hasta resolver la respuesta del cliente.
- Se conservan las excepciones existentes para garantías, parque propio, alquileres,
  demos y devoluciones sin reparación. Un MG vendido vuelve a requerir presupuesto.
- Se conservan las reimpresiones de órdenes ya emitidas.
- La API responde 409 antes de modificar la OS cuando falta resolver el presupuesto.
- El número interno nulo no evita el control. El frontend ya no interpreta una serie
  con formato MG como prueba suficiente de propiedad para este control.

## Verificación local

- 14 pruebas de `release-flow.test.js`: correctas.
- 33 pruebas de presupuesto y derivaciones del backend: correctas, sin base productiva.
- 320 combinaciones evaluadas con consultas CTE en `nexora-test-pg`: la expresión SQL
  y la regla Python dieron el mismo resultado. No se modificaron tablas ni datos.
- Compilación Vite correcta, con salida en una carpeta temporal local.
- La ampliación a las suites de no reparación, cobro de presupuesto rechazado y
  venta MG no llegó a ejecutar casos: sus fixtures carecen de
  `users.bejerman_seller_code`. Se comprobó en una base descartable creada y eliminada
  por el runner; esas suites no se consideran aprobadas.

No se modificó el esquema de la aplicación, no se desplegó en SERVERDATA y no se
cambió el estado histórico de la OS 29639 ni de la OS 29666. Sus estados actuales
en producción no fueron consultados en esta tarea.
