# Trabajos generales para el catálogo técnico

Consulta realizada el 15 de octubre de 2026 sobre el histórico real de NEXORA en SERVERDATA: contenedor `nexora-api`, base `servicio_tecnico`. La transacción utilizó `REPEATABLE READ, READ ONLY`; se verificó `transaction_read_only = on`. No se ejecutaron pruebas ni escrituras en producción.

El período analizado comprende del 15/10/2025 al 15/10/2026. Se tomó la fecha de servicio y, cuando falta, la fecha de ingreso. Incluye **1.327 órdenes**, de las cuales **1.062 tienen texto de trabajos**. Se obtuvieron 1.659 combinaciones distintas de tipo de equipo y fragmento de texto.

| Trabajo propuesto | Órdenes con menciones relacionadas |
| --- | ---: |
| Revisión técnica | 485 |
| Limpieza y desinfección interna y externa | 479 |
| Ajustes y regulación | 102 |
| Calibración | 88 |
| Reparación electrónica y soldadura | 25 |
| Pruebas y verificación de funcionamiento | 12 |
| Mantenimiento general | 1 |

El catálogo inicial incorpora limpieza y desinfección, y revisión técnica. Los demás grupos quedan como propuestas para revisar y dar de alta desde la nueva administración del catálogo. Configuración y programación no tuvieron coincidencias con los criterios utilizados.

Los conteos agrupan menciones por palabras y expresiones; una orden puede aparecer en varios grupos. El texto histórico combina trabajos previstos y realizados. Por eso, este informe sirve para definir nombres del catálogo y requiere revisar el alcance de cada trabajo. En particular, el grupo de limpieza reúne menciones de limpieza, desinfección o higiene; la ejecución de cada tarea se confirmará en la lista estructurada.

El [CSV por tipo de equipo](carga_tecnica_trabajos_ultimo_ano_20261015.csv) permite revisar la distribución. Los criterios están en `WORK_CANDIDATES`, dentro del comando `report_technical_work_catalog`; este admite `--proposals` para generar el mismo formato de propuestas. Los cambios de piezas se registran mediante el catálogo de repuestos.

Las horas y las tarifas se cargarán con el nuevo circuito. Este análisis conserva las órdenes y sus textos originales.
