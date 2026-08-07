# Endurecimiento del circuito RSS — 27/07/2026

## Hallazgo sobre el job bloqueado con dos intentos

El valor versionado de `BEJERMAN_MAX_ATTEMPTS` es 8 tanto en `.env.prod` como
en `api/app/settings.py`. Sin embargo, ese límite no intervenía ante el mensaje:

> El comprobante no se importó. No ha sido posible actualizar los datos en la tabla.

El cliente de sincronización convertía toda respuesta SOAP no exitosa en
`BejermanBlockedError`. Luego, `_process_claimed_job` marcaba esos errores como
`blocked` de inmediato, sin pasar por `_retry_job`. Como `_claim_job` incrementa
`attempts` antes de procesar, un error transitorio podía dejar el job bloqueado
con uno o dos intentos aunque el máximo general fuera 8.

La comprobación del valor efectivo dentro del contenedor productivo quedó
pendiente de autorización explícita. Esta tarea no ejecutó tests ni comandos
contra producción.

## Decisiones implementadas

- El máximo general permanece en 8.
- Los bloqueos de actualización de tabla y las carreras internas reconocidas
  se tratan como transitorios.
- Esa familia dispone de hasta 12 intentos, con una espera inicial de 300
  segundos y backoff progresivo.
- Después de enviar la entrada de transferencia, NEXORA confirma que la partida
  tenga stock positivo en el depósito destino antes de marcar el job como
  `succeeded`.
- Un job bloqueado por un error transitorio puede reabrirse automáticamente
  después de 30 minutos. El contador queda dentro del JSONB existente y se
  limita a dos reaperturas automáticas. El worker busca candidatos cada cinco
  minutos y procesa los reabiertos en su loop normal.
- Antes de emitir un remito de Orden de Entrega, NEXORA intenta resolver una
  sola vez y en línea los jobs bloqueados vinculados, usando las credenciales
  del usuario que está emitiendo. Si continúan pendientes, corta antes de llamar
  al SDK de remitos con `BEJERMAN_REMITO_BLOCKED_BY_SYNC`. El control considera
  todas las transferencias `STR` vinculadas, sin límite de cinco, y excluye los
  jobs legacy de ingreso/salida que quedan bloqueados deliberadamente. Si la
  cola no puede consultarse, falla cerrado con
  `BEJERMAN_REMITO_SYNC_PREFLIGHT_UNAVAILABLE`.
- La resolución fiscal de clientes usa primero la copia sincronizada en
  `customers`, separada por empresa. Si la copia está ausente o incompleta,
  consulta Bejerman y hace un único reintento corto ante un error reconocido de
  resolución.
- Los jobs que permanecen bloqueados más de dos horas generan una alerta por
  correo a administración/soporte mediante el outbox existente. La deduplicación
  se hace por episodio de bloqueo y la revisión corre cada 15 minutos.
- Los contenedores productivos validan `BEJERMAN_WSDL_URL` antes de iniciar
  Gunicorn o el worker de sincronización.

Los valores son configurables con `BEJERMAN_SYNC_TRANSIENT_MAX_ATTEMPTS`,
`BEJERMAN_SYNC_TRANSIENT_RETRY_BASE_SECONDS`,
`BEJERMAN_SYNC_TRANSIENT_REOPEN_INTERVAL_SECONDS`,
`BEJERMAN_SYNC_TRANSIENT_REOPEN_COOLDOWN_SECONDS`,
`BEJERMAN_SYNC_TRANSIENT_REOPEN_MAX_CYCLES`,
`BEJERMAN_SYNC_STOCK_VERIFY_ATTEMPTS`,
`BEJERMAN_SYNC_STOCK_VERIFY_DELAY_MS`,
`BEJERMAN_SYNC_BLOCKED_ALERT_AFTER_HOURS` y
`BEJERMAN_SYNC_BLOCKED_ALERT_INTERVAL_SECONDS`.

## Auditorías menores

No quedó ninguna referencia suelta vigente a `delivery_type`: las lecturas
actuales están asociadas a un parámetro o a una asignación previa. No se agregó
un parche especulativo sin el traceback histórico.

Una reconciliación periódica de stock es viable como comando de auditoría de
solo lectura. Debería agrupar la demanda de órdenes pendientes por empresa,
artículo, depósito y partida, compararla con `ObtenerStockPartida` y clasificar
`ok`, `missing`, `insufficient` o `query_error`. Se deja fuera de este cambio
porque necesita definir el alcance operativo y las credenciales del actor; no
debe reutilizar el comando de restauración, que sí emite movimientos ENT.

No hubo cambios de estructura de base de datos en estas etapas.
