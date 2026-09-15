# Guardia de NEXORA

Guía para Recepción, Servicio Técnico, Logística, Administración y Cobranzas.
Revisión del código: 12 de septiembre de 2026.

**Esta auditoría deja cambios en desarrollo. No los instala en producción.**
La presencia de un botón en el código no acredita que esté disponible en SERVERDATA.
Si la pantalla no ofrece la acción indicada, registre el caso y escálelo al responsable del área.

## Revisión diaria

1. Abra NEXORA y consulte una OS conocida. Compruebe que carga su información.
2. Revise las órdenes pendientes del área y el panel de procesos de Bejerman.
   Mire el estado, el último intento y el detalle del error, especialmente si una entrega está detenida.
3. Revise los avisos recibidos y los pendientes visibles en NEXORA.
   **La ausencia de correo no demuestra que todo esté bien.** Puede no haber pendientes,
   faltar un destinatario, estar deshabilitado el envío o fallar el proceso de correo.
4. Registre los casos sin resolver: fecha, OS u orden de entrega, usuario,
   empresa de Bejerman, comprobante, mensaje exacto y qué se hizo.

Que la página cargue no demuestra que funcionen la sincronización, los PDF ni el correo.
No espere hasta el día siguiente si el problema impide una entrega o afecta stock.

## Qué hacer según el estado

| Estado o situación | Acción operativa |
|---|---|
| Pendiente o ejecutando | Consulte el último intento. Si no avanza o bloquea una operación necesaria, registre el caso y avise al responsable. |
| Falló | Lea el detalle. Algunos procesos reintentan, pero no todos los errores son recuperables. No presuponga que se resolverá solo. |
| Bloqueado | Identifique y corrija la causa con el área responsable. Reintente únicamente cuando esté resuelta y no haya incertidumbre de emisión. |
| En revisión, enviado sin respuesta o resultado desconocido | Siga el procedimiento de comprobante incierto de abajo. |
| Completado | Compruebe el resultado requerido: comprobante, empresa, partidas, cantidades y estado de la entrega. |
| PDF pendiente | El comprobante puede estar emitido. Reintente la consulta o impresión del PDF; no emita otro remito para conseguirlo. |

Los reintentos necesitan procesos activos, credenciales válidas y servicios disponibles.
Un error de activación de Bejerman no garantiza que el servicio vaya a recuperarse solo.

## Comprobante incierto: evitar una emisión duplicada

Un corte de conexión o un tiempo de espera agotado **no demuestra que el remito no se emitió**.
También puede haberse emitido aunque NEXORA todavía no tenga el número o el PDF.

1. Conserve la OS, la orden, el mensaje y la hora. No vuelva a emitir ni cambie a carga manual para eludir el bloqueo.
2. Si aparece **Verificar en Bejerman**, úselo: consulta el comprobante sin emitir uno nuevo.
3. Si sigue incierto, el responsable de Administración o Cobranzas debe consultar Bejerman.
   Contraste empresa, tipo, punto de venta, número, cliente, fecha, artículos,
   partidas, cantidades y depósito. Encontrar solamente una serie no alcanza.
4. Si existe el comprobante, vincule o resuelva la revisión con su referencia mediante
   la función autorizada de NEXORA. No lo vuelva a emitir.
5. Sólo declare **No se emitió** cuando la consulta confirme la ausencia del comprobante
   y del movimiento correspondiente. Si hay más de un candidato o la consulta falla,
   mantenga el caso en revisión y escálelo.

Los mensajes que indican que el comprobante ya existe o que se importó con un cambio
contable no deben tratarse como autorización para repetirlo.
Una emisión duplicada requiere conciliación por Administración/Cobranzas antes de
anular nada; comprueben también el stock y los vínculos en NEXORA.

## Problemas habituales

| Problema | Responsable y siguiente paso |
|---|---|
| Falta stock | Logística contrasta artículo, partida, empresa y depósito con Bejerman. No haga transferencias a ciegas ni emita para probar. |
| Artículo sin vincular | El responsable del catálogo revisa el mapeo. Use Resolver artículo Bejerman si está disponible y verifique empresa y variante. |
| Credenciales faltantes | El usuario carga sus propias credenciales desde el perfil. Luego compruebe si el proceso se reabrió; no dé la recuperación por hecha. |
| Derivación activa | Servicio Técnico registra la devolución efectiva antes de liberar o entregar. El estado operativo de la OS se conserva por separado. |
| Presupuesto pendiente o inconsistente | Administración y Servicio Técnico revisan aprobación, rechazo o anulación. No cambie estados para eludir una validación. |
| Conjunto de remitos incompleto | Revise los comprobantes ya emitidos y las cantidades pendientes. No reinicie el conjunto ni duplique las entregas ya documentadas. |
| Ingreso anterior abierto | Recepción y Servicio Técnico determinan qué ocurrió con la OS anterior y su entrega. No la cierren sólo para quitar el aviso. |
| Tipo de sincronización sin implementación | Escale a soporte con el identificador y el detalle. No suponga que es un registro descartable. |

La entrega física y la emisión del comprobante son hechos distintos. Verifique ambos.
No haga correcciones históricas ni escrituras directas en tablas del ERP como procedimiento de guardia.

## Correo y procesos automáticos

La configuración de desarrollo del repositorio incluye alertas diarias, resumen de
facturación, cola de correos, sincronización de stock, búsqueda de PDF y vencimientos
de postergaciones. Su ejecución efectiva en producción depende de los servicios y
la configuración instalados en SERVERDATA.

El resumen de facturación no envía correo cuando no tiene destinatarios o pendientes.
Las alertas de presupuestos tampoco envían un resumen vacío si no encuentran casos.
Por eso, use las listas de NEXORA para comprobar pendientes; el correo es un aviso complementario.

Si un PDF no aparece, registre tipo, número, empresa y hora. Pedir el PDF no equivale
a emitir nuevamente el comprobante. Un técnico autorizado puede consultar el estado
de la cola y el latido del proceso antes de intervenir.

## Si NEXORA no carga o no guarda

1. Pruebe una recarga y verifique si otro puesto presenta el mismo problema.
   Antes de repetir una operación que guarda datos, consulte si ya quedó registrada.
2. Avise al responsable operativo y a soporte de infraestructura asignado a SERVERDATA.
   Informe desde cuándo ocurre, puestos afectados, URL y mensaje exacto.
3. Si no hay sistema, registre las operaciones pendientes mediante el procedimiento
   de contingencia del área y conserve sus referencias para conciliarlas al restablecerse.
   No genere comprobantes o movimientos sin coordinación entre Logística y Administración.
4. Reiniciar, reconstruir, actualizar o restaurar corresponde al soporte autorizado,
   tras identificar el servicio afectado y conservar evidencia. **Rebuild no es una
   acción rutinaria para personal operativo ni una garantía de recuperación.**

No es necesario localizar a la persona que está de vacaciones. Antes de la guardia,
la organización debe dejar asignados y accesibles el responsable operativo,
el suplente con permisos probados, soporte de SERVERDATA y soporte de Bejerman.
La auditoría no puede inventar esos nombres o sus medios de contacto.

## Diagnóstico técnico y recuperación

Comandos de consulta incluidos en el código, ejecutados por soporte en el contenedor
correcto de la instalación afectada:

```bash
python manage.py bejerman_pdf_queue_status
python manage.py recover_stuck_bejerman_work
python manage.py limpiar_procesos_bejerman_trabados
python manage.py suplencia --estado
```

Los comandos de recuperación anteriores, sin `--apply`, informan o simulan;
no confunda una simulación exitosa con una reparación aplicada. Verifique primero
que la versión instalada incluya el comando. No ejecute pruebas en producción.
Una suplencia debe prepararse antes de la guardia y comprobarse con el usuario suplente.
Su cierre restaura los valores guardados; revise cambios de permisos posteriores antes de cerrarla.

Un respaldo sólo es utilizable si se puede restaurar. Soporte debe conocer dónde se
guardan la base y los archivos, la fecha de la última copia comprobada y el procedimiento
de restauración. Nunca pruebe la restauración sobre la base activa.

## Mejoras de esta auditoría todavía no desplegadas

- Evidencia del intento de remito persistida aunque falle la respuesta del emisor.
- Exclusión de trabajos duplicados y rechazo de reservas basadas en una lectura anterior a un remito ya confirmado.
- Consulta coherente de la derivación activa al repetir una devolución antigua.
- Corrección de consultas de estados enumerados y paridad del esquema de instalación.
- Respaldo PostgreSQL binario validado mediante una restauración descartable.
- Referencias RSS por OS cuando las partidas están asignadas, e historial de entrega coherente.
- Corrección de remitos después de confirmar que no se importaron, manteniendo el bloqueo cuando el resultado sigue incierto.
- Fechas civiles de derivación y devolución coherentes con el día local.

El [informe de auditoría](reportes/auditoria-preventiva-20260912.md) registra las pruebas y los pendientes de validación.

Estas mejoras requieren una puesta en producción posterior y validación de la
instalación real. Mientras tanto, mantenga el procedimiento de revisión de comprobantes inciertos.
