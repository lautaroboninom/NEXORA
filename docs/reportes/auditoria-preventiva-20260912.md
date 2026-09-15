# Auditoría preventiva de NEXORA — 12 de septiembre de 2026

Las correcciones confirmadas quedaron integradas en el código compartido de desarrollo, sin commits ni despliegues. **Este informe no acredita la preparación de producción ni una revisión manual exhaustiva de cada archivo.** Los impedimentos para el cierre integral se detallan al final.

## Alcance y conservación del trabajo

Se conservó una copia aislada en `C:\Users\gracia\AppData\Local\Temp\nexora-audit-20260910-094635\source`, partiendo de HEAD `90428c9`, con 94 entradas de trabajo sin commit: 54 modificadas y 40 nuevas. Se copiaron inicialmente 932 archivos y se incorporaron cambios posteriores al corte de validación. El [estado inicial](auditoria-preventiva-20260912/estado-inicial.txt), el [inventario con hashes y profundidad](auditoria-preventiva-20260912/inventario.json) y el [registro de integración](auditoria-preventiva-20260912/integracion.json) delimitan la evidencia. Los archivos sin lectura profunda están identificados expresamente; inventariar no equivale a auditar su lógica completa.

Antes de integrar se compararon hashes y se guardaron las versiones anteriores en `integration-backup` dentro del directorio aislado. Se conservaron las modificaciones ajenas, incluidas las concurrentes. El cambio del conflicto parcial de alquileres ya estaba presente en el código compartido y no se sobrescribió.

## Aislamiento de las pruebas

Se utilizó Docker Desktop local, con red interna `nexora-audit-20260910`, PostgreSQL 16 descartable, bases exclusivas `audit_*` y `test_audit_*`, correo en memoria y servicios externos simulados. El ejecutor de pruebas restringe conexiones a PostgreSQL y aplica un límite por caso. Las pruebas de navegador utilizaron registros sintéticos y una API local mediante `127.0.0.1:18199`; se comprobó la configuración de base y correo antes de preparar sus esquemas. No se utilizaron bases, archivos operativos ni servicios de producción para probar.

No hubo escrituras directas en Bejerman, reparaciones históricas de datos, commits ni despliegues. La integración de código en el checkout compartido no equivale a instalarlo en el runtime productivo.

## Hallazgos corregidos

La gravedad considera la consecuencia potencial del fallo; no afirma que ya haya ocurrido en producción.

| ID / gravedad | Escenario y consecuencia | Ubicación y corrección | Evidencia |
|---|---|---|---|
| A01 / Crítica | Bejerman emite y después falla la respuesta. Una transacción exterior podía revertir la evidencia local del intento y permitir otra emisión. | `api/service/delivery_remito_jobs.py`, `_execute`: se quitó la transacción exterior que anulaba la persistencia de reserva, envío y revisión. | `test_delivery_remito_jobs`: 19 casos; visibilidad desde otra conexión y persistencia ante fallo. Logs antes/después incluidos. |
| A02 / Crítica | Doble clic o llamadas concurrentes podían crear trabajos duplicados; una lectura anterior a una emisión confirmada podía reservar otra vez. | `delivery_remito_jobs.py`: bloqueo asesor transaccional por orden antes de deduplicar. `bejerman_delivery.py`: segunda comprobación bajo bloqueo de órdenes, incluyendo estados confirmados y secuencia del lote. | 19 casos de trabajos y 3 de reserva, con PostgreSQL real y coordinación de hilos; logs de fallo previo y corrección. |
| A03 / Alta | La actualización de trazabilidad de alquiler podía fallar porque `ON CONFLICT` no coincidía con el índice parcial. | `bejerman_rental_remito.py`, `_upsert_trace_job`: predicado `WHERE ingreso_id IS NOT NULL`. La corrección equivalente concurrente se conservó. | Módulos de remitos de alquiler e ingreso/alquiler aprobados; detalle de casos en resultados JSON. |
| A04 / Alta | Consultas con `COALESCE` sobre enumerados y texto podían provocar errores PostgreSQL y bloquear listados o movimientos. | `bejerman_particular_customers.py`, `stock_corrections.py`, `views/ingresos_views.py`: conversión explícita del estado a texto. | Regresiones y módulos de clientes, ingresos y stock ejecutados. |
| A05 / Alta | Una instalación o actualización podía omitir objetos de permisos y administración de usuarios. | `management/commands/prepare_production_schema.py`: inclusión de los comandos correspondientes; paridad documentada en `sql/schema.sql`. | Preparación estricta en PostgreSQL descartable; comandos repetidos sin error y comprobación de objetos. |
| A06 / Alta | El esquema limpio no representaba la empresa del mapeo de artículos ni su unicidad, con incompatibilidad entre instalación y actualización. | `sql/schema.sql`: `company_key`, valor inicial, normalización y unicidad por modelo, variante y empresa. Se conserva el apply existente correspondiente. | SQL limpio y dos ejecuciones de `prepare_production_schema --strict`; índice y campos comprobados. |
| A07 / Alta | La redirección de `pg_dump -Fc` en PowerShell podía alterar el respaldo binario; un error podía pasar inadvertido. | `scripts/backup-nexora.ps1`: archivo temporal dentro del contenedor, comprobación de salida y catálogo, copia binaria, publicación del archivo completo y limpieza. | Respaldo y restauración descartables; comprobación de datos binarios `00ff0180`. Se conserva log del respaldo. |
| A08 / Alta | Derivar y devolver bloqueaban filas en orden inverso, abriendo una posibilidad de interbloqueo. Repetir una devolución antigua podía ocultar una derivación nueva. | `views/derivaciones_views.py`: mismo orden de bloqueo desde ingreso y consulta de la derivación actualmente activa al repetir. | 13 casos de derivaciones y recorrido de navegador con persistencia. La inversión se confirmó por código; no se hizo una prueba de estrés que reprodujera el interbloqueo. |
| A09 / Media | El historial de entrega podía perder un evento porque un `CASE` producía texto donde se esperaba `ticket_state`; el error era absorbido. | `bejerman_delivery.py`, `_insert_service_release_delivery_events`: conservar la rama tipada como estado. | 23 casos de sincronización de entrega con PostgreSQL. |
| A10 / Alta | Una entrega agrupada asignaba a cada OS todos los números RSS del lote, aunque algunos correspondieran a otras OS. La lectura volvía a mostrar esa asociación incorrecta. | `bejerman_delivery.py`, `_sync_service_release_ingresos_after_remito`, y `views/ingresos_views.py`, `_service_release_remito_salida_for_ingreso`: asignación y lectura según las partidas vinculadas. Se mantiene la referencia general cuando no existen asignaciones conocidas. | Regresiones de 13 OS y 7 RSS verifican persistencia y lectura por OS, dentro de los 150 casos de notificaciones. |
| A11 / Media | Al seleccionar otra versión de protocolo, el GET podía devolver identificador y versión anteriores junto al protocolo nuevo. | `views/ingreso_tests_views.py`: metadatos del protocolo elegido, con respaldo a la fila cuando corresponde. | 11 casos del módulo de protocolos aprobados. |
| A12 / Alta | Después de confirmar que no hubo importación, la corrección de un remito comparaba otra vez los artículos congelados del envío anterior y rechazaba cambios válidos como si nada hubiera cambiado. | `bejerman_delivery.py`, `reissue_corrected_bejerman_remito_review`: conciliar con la evidencia original y, sólo cuando se permite corregir, releer los artículos actuales antes de comparar. | Casos de cantidad corregida, no importación confirmada y resultado todavía incierto; este último conserva el rechazo 409. Módulo de notificaciones: 150 casos aprobados. |
| A13 / Media | Cerca de medianoche, una derivación tomaba el día UTC en lugar del local; una fecha civil se mostraba como el día anterior. | `web/src/lib/ui-helpers.js`, `DerivarIngreso.jsx`, `Derivados.jsx`, `ServiceSheet/tabs/DerivacionesTab.jsx`: fecha civil local para formularios y `formatDateOnly` al mostrarla. | Regresión del límite horario de Argentina, 173 pruebas frontend y recorrido de devolución en navegador con fechas contrastadas en PostgreSQL. |

Los cambios conservan los contratos de negocio: derivar no reemplaza el estado operativo, una derivación activa impide liberar/entregar, la incertidumbre de emisión no habilita otra emisión y la entrega física se distingue del movimiento de depósito. No se agregaron permisos ni estados nuevos como parte de estas correcciones.

## Pruebas y obstáculos encontrados

- **Backend: 121 módulos, 1881 casos, cero fallos, errores u omitidos**, agregando la última ejecución de cada módulo. No es una única ejecución monolítica. El corte anterior tenía 1879 casos; la integración de dos casos concurrentes de leyendas elevó el total a 1881. [Resultados por módulo y procedencia](auditoria-preventiva-20260912/resultados-backend.json).
- **Frontend: 173 casos aprobados**, sin cancelados ni omitidos. [Salida final](auditoria-preventiva-20260912/frontend-merge-tests.log).
- Compilación de frontend y generación del service worker aprobadas después de la corrección de fechas. Permanece el aviso por tamaño del paquete principal; no impidió compilar. [Salida de build](auditoria-preventiva-20260912/frontend-dates-build.log). La adición concurrente de una función de ubicación se conservó y pasó las pruebas frontend; ese cambio adicional no recibió otro build completo.
- [Instalación SQL limpia](auditoria-preventiva-20260912/schema-cutoff-clean.log) y preparación estricta [primera](auditoria-preventiva-20260912/schema-cutoff-apply-1.log) y [segunda](auditoria-preventiva-20260912/schema-cutoff-apply-2.log). Se comprobaron los objetos de permisos, campos de usuarios e índice por empresa. `collectstatic` se omitió por falta de `STATIC_ROOT`; no se presenta como validado.
- [Respaldo descartable](auditoria-preventiva-20260912/backup-validation.log) y restauración comprobada durante la auditoría. Esto no certifica el respaldo de SERVERDATA ni el de los adjuntos operativos.

La primera ejecución monolítica no llegó a completar la suite por presión de memoria. Se pasó a ejecución serial por módulos y límites de recursos. También se corrigió la preparación de pruebas: aplicar el esquema de modelos no administrados sólo en bases `test_*`, usar fixtures compatibles con claves foráneas, enumerados, booleanos y columnas generadas; recuperar IDs de la secuencia de la tabla en lugar de `LASTVAL`; cerrar conexiones de hilos y mantener la zona horaria esperada por Django. Varias pruebas antiguas tenían contratos desactualizados de PDF, correo, reintentos y partidas; se ajustaron contrastando el comportamiento implementado. Los tests puros que incorporaron consultas nuevas recibieron simulaciones explícitas, manteniendo pruebas separadas de esas consultas.

No se contaron como aprobadas ejecuciones que abortaron, no descubrieron casos o fallaron al preparar el entorno. Un intento final de frontend con un argumento no compatible con `node --test` tampoco ejecutó casos; se corrigió la invocación y el log enlazado corresponde a los 173 casos realmente ejecutados.

## Cobertura de circuitos

| Circuito | Profundidad alcanzada | Límite concreto |
|---|---|---|
| Ingresos y equipos | Pruebas API de alta individual/masiva, series, identificadores, clientes, propiedad e historial; revisión dirigida de consultas y fixtures. | No se recorrió cada variante de alta por pantalla. No se conciliaron datos históricos. |
| Servicio técnico | Pruebas de permisos, asignaciones, reparación, protocolos, presupuestos, rechazo, anulación, preventivos y cambio de equipo. Diagnóstico guardado y recuperado por navegador/API/DB. | No se hizo un recorrido completo de navegador para cada rol y combinación de estados. |
| Derivación, liberación y entrega | Revisión de guardas y estados; derivación y devolución completas en navegador; liberación bloqueada visible; pruebas individuales y agrupadas, historial y asignaciones de remito. | Sin ensayo de entrega física real, impresora u hoja de ruta con operadores. |
| Bejerman y stock | Revisión profunda de reserva, envío, ambigüedad, conciliación y reemisión; concurrencia en PostgreSQL; módulos de empresas, partidas, stock, alquileres, PDF y entrega. | SDK y ERP simulados. Sin homologación con Bejerman de prueba ni validación de credenciales reales. |
| Procesos automáticos | Módulos de colas, reintentos, PDF, correo, alertas, notificaciones y postergaciones. Revisión de durabilidad del intento. | Correo en memoria, sin entrega SMTP real; sin prueba prolongada de reinicios, cortes de máquina o watchdog productivo. |
| Funciones transversales | Pruebas de permisos efectivos, recepción, presupuestos, búsquedas, reportes y portal según los módulos enumerados; 33 rutas cargadas y dos roles usados en navegador. | Las 33 rutas son cobertura de carga, no 33 recorridos funcionales; algunas tenían un aviso de credenciales. No es una auditoría de seguridad ni de todas las exportaciones y archivos. |
| Instalación y recuperación | SQL limpio, apply estricto repetido, arranque de API local, build y service worker, respaldo/restauración PostgreSQL descartables. | Sin reconstrucción integral de todas las imágenes desde cero, restauración de adjuntos ni ensayo de recuperación de SERVERDATA. |

Evidencias del navegador: [diagnóstico persistido](auditoria-preventiva-20260912/navegador/diagnostico-db.txt), [bloqueo por derivación](auditoria-preventiva-20260912/navegador/derivacion-bloqueo.txt), [devolución y fechas en DB](auditoria-preventiva-20260912/navegador/derivacion-db.txt), [técnico sin acceso a usuarios](auditoria-preventiva-20260912/navegador/tecnico-usuarios-denegado.txt) y [carga de rutas](auditoria-preventiva-20260912/navegador/rutas.json). Las pruebas de navegador no incluyeron la última corrección de reemisión; esa corrección se validó por API y pruebas automatizadas.

## Cambios concurrentes y pendientes de cierre

Durante la integración siguió entrando código ajeno al corte probado. Se conservaron y comprobaron específicamente la adaptación de leyendas a 50 caracteres y la compatibilidad de las pruebas de remitos: 230 + 13 casos aprobados. También se conservó el helper frontend de identificación de estantería de alquiler.

Se detectaron cambios posteriores en `api/app/settings.py` (ubicaciones de alertas), `api/service/bejerman_ris.py` (control de artículo renombrado), el nuevo `bejerman_article_rename.py`, `api/service/constants.py` y `api/service/delivery_orders.py`. **La suite aprobada no certifica esas modificaciones ni cualquier otra posterior a los hashes del inventario.** Se debe revisar su delta y probar los módulos afectados antes de preparar una versión para producción; no corresponde repetir toda esta auditoría sin distinguir los cambios nuevos.

Impedimentos para afirmar el cierre integral solicitado:

1. Revisar los cambios concurrentes posteriores al corte y las áreas inventariadas sin lectura manual profunda. No queda demostrado que todo el código propio esté libre de fallos.
2. Homologar en un entorno externo de prueba las respuestas ambiguas, recuperación y PDF del SDK de Bejerman, con correo de prueba y sin documentos reales. Los simuladores no acreditan el comportamiento del servicio externo.
3. Completar los recorridos de navegador pendientes por rol, las entregas parciales y fallos de integración, y la recuperación tras interrupción de procesos y máquina en desarrollo.
4. Comprobar la restauración conjunta de base y adjuntos y el arranque de una instalación completa desde sus dependencias. La prueba de PostgreSQL no cubre todo el servicio.
5. La organización debe designar responsables y suplentes de las áreas, soporte de SERVERDATA y Bejerman, con contactos accesibles y permisos comprobados antes de la guardia.

No quedan abiertos, dentro del corte revisado, los fallos críticos y altos descritos arriba. Las limitaciones anteriores impiden declarar cumplido todo el plan o certificar la salida de vacaciones sin cobertura operativa. La [guía de guardia](../runbook-guardia-vacaciones.md) distingue las acciones actuales de las mejoras todavía no desplegadas y no depende de localizar a la persona de vacaciones.

## Reproducibilidad

La copia aislada conserva `run_modules.py`, `guarded_test.py`, resultados anteriores, scripts de preparación y respaldos sintéticos. Los logs adjuntos identifican la ejecución que aporta cada resultado. Los comandos de esquema se ejecutaron desde `/audit/source/api` con la configuración aislada. Para una nueva validación se debe crear una base descartable y servicios simulados equivalentes; no reutilizar variables de producción. Los artefactos temporales no sustituyen un respaldo operativo.
