# Validación de carga técnica y presupuestos por horas

Verificación realizada el 15/10/2026 en el proyecto Docker `nexora-technical-dev`, con datos ficticios. La base de trabajo fue `nexora_technical_dev` y las pruebas automatizadas utilizaron `test_nexora_technical_dev`.

## Backend

Se ejecutaron **100 pruebas, todas aprobadas, sin omisiones**, de los siguientes módulos:

- `test_technical_interventions`
- `test_quotes_generated_totals`
- `test_quote_pdf_warranty`
- `test_quotes_emitir_autoasignacion`
- `test_quotes_anular`
- `test_portal_integration`
- `test_pendientes_presupuesto`
- `test_permission_coverage`
- `test_repuestos_compra`

La comprobación de equivalencia de permisos también leyó `web/src/lib/permissions.js`, copiado al contenedor de pruebas en `/web/src/lib/permissions.js`.

Los casos incluyen el circuito hasta aprobación y reparación, tarifas mínimas y proporcionales para 30, 60, 80, 120 y 150 minutos, tarifa general sin falla, bonificación, tarifa faltante, permisos individuales efectivos, respuestas técnicas sin datos comerciales, altas de catálogo, conflictos de revisión, reintentos, ampliaciones y diferencias de stock. Se comprobaron las aprobaciones internas y del Portal Cliente, la conservación del PDF emitido, las ampliaciones de presupuestos anteriores a la carga estructurada y el registro de ejecución de artículos posteriormente desactivados.

## Esquema

Se ejecutó `apply_technical_interventions_schema` y se repitió su aplicación para verificar idempotencia. Se consultó PostgreSQL y se confirmó la existencia de las siete tablas técnicas, las cinco columnas nuevas de `quotes`, la vinculación de artículos de presupuesto y el estado `sustituido`. El DDL del comando coincide con la sección correspondiente de `sql/schema.sql`.

## Interfaz y documentos

`npm run build` finalizó correctamente. Permanecen avisos de dependencias y tamaño de paquetes existentes; no hubo errores de compilación.

Se utilizó Chromium para revisar la ficha del técnico en escritorio y en un ancho de 390 píxeles, el envío a presupuestar, la configuración administrativa, el cálculo y la emisión desde la interfaz. Se descargó y validó un PDF de 735.284 bytes. Luego se guardaron 40 minutos reales y se comprobó que la versión y el importe emitidos permanecieran iguales. No se registraron errores de JavaScript.

La demostración final quedó en la OS 1, con versión 2 pendiente y versión 1 conservada como ejemplo emitido. Se retiraron los valores monetarios ficticios utilizados en las pruebas. La tarifa faltante se informa en pantalla y bloquea la emisión tanto en la interfaz como en la API. Se revisó que la vista muestre cantidades y observaciones técnicas, y que los artículos del alcance se modifiquen desde la intervención.

## Histórico real

El único acceso a producción fue la [consulta histórica de solo lectura](reportes/carga_tecnica_trabajos_ultimo_ano_20261015.md), por SSH a SERVERDATA, dentro de una transacción PostgreSQL con `transaction_read_only = on`. No se ejecutaron pruebas, migraciones ni despliegues en producción. El comando de informe también se ejecutó en desarrollo para verificar su salida CSV.

## Configuración pendiente

Los valores reales de primera hora, hora adicional y diagnóstico y limpieza deben cargarse desde la tabla del negocio. La emisión continúa requiriendo revisión manual. Las propuestas del histórico quedan disponibles para decidir qué otros trabajos incorporar al catálogo.
