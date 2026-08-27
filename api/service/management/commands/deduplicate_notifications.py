import logging
from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Deduplica notificaciones historicas que usaban dedupe_key con fecha."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica los cambios en la base de datos.",
        )
        parser.add_argument(
            "--purge",
            action="store_true",
            help=(
                "Borra las copias sobrantes en vez de marcarlas leidas. "
                "Marcarlas leidas conserva el historial pero las deja ocupando "
                "lugar en la lista de la campana, que viene cortada."
            ),
        )

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        purge = bool(options.get("purge"))

        if dry_run:
            self.stdout.write("Ejecutando en modo DRY-RUN. No se guardaran cambios (usa --apply para aplicar).")
        else:
            self.stdout.write("Ejecutando en modo APPLY. Se guardaran los cambios en la BD.")

        targets = [
            ("presupuesto_pendiente", True, "ingreso:{entity_id}:presupuesto_pendiente"),
            ("preventivo_vencido", False, "preventivo_vencido"),
            ("preventivo_proximo", False, "preventivo_proximo"),
            ("billing_pending_summary", False, "billing_pending_summary"),
            # El resumen operativo tambien traia la fecha adentro del dedupe_key
            # (work_summary:<periodo>:<fecha>): una fila nueva por dia y por jefe.
            ("resumen_operativo", True, "work_summary:{entity_id}"),
        ]

        with transaction.atomic():
            with connection.cursor() as cur:
                for notif_key, has_entity, dedupe_tpl in targets:
                    self.stdout.write(f"\nProcesando {notif_key}...")
                    
                    group_by = "user_id, entity_id" if has_entity else "user_id"
                    
                    # Buscamos los grupos que tienen mas de una notificacion (misma entity / user).
                    # Ojo con el SELECT: el COUNT(*) sobraba y el desempaquetado de
                    # abajo esperaba una columna menos, asi que el comando reventaba
                    # con "too many values to unpack" apenas encontraba un duplicado
                    # (o sea: nunca llego a limpiar nada).
                    cur.execute(f"""
                        SELECT {group_by}, MAX(id)
                        FROM notifications
                        WHERE notification_key = %s
                        GROUP BY {group_by}
                        HAVING COUNT(*) > 1
                    """, [notif_key])
                    
                    rows = cur.fetchall()
                    self.stdout.write(f"Encontrados {len(rows)} grupos con duplicados.")

                    for row in rows:
                        if has_entity:
                            user_id, entity_id, keep_id = row
                        else:
                            user_id, keep_id = row
                            entity_id = None
                            
                        new_dedupe_key = dedupe_tpl.format(entity_id=entity_id) if has_entity else dedupe_tpl
                        
                        if dry_run:
                            continue
                            
                        # Por defecto las viejas se marcan leidas (el historial importa).
                        # Conservan su dedupe_key original con fecha, por lo que no chocan
                        # en la constraint unique(user_id, notification_key, dedupe_key).
                        # Con --purge se borran: quedan cientos de copias diarias y, aun
                        # leidas, llenan la lista de la campana.
                        if purge:
                            action_sql = "DELETE FROM notifications"
                        else:
                            action_sql = (
                                "UPDATE notifications "
                                "SET read_at = COALESCE(read_at, CURRENT_TIMESTAMP)"
                            )
                        if has_entity:
                            # IS NOT DISTINCT FROM y no "=": hay filas con entity_id NULL
                            # que se agrupan juntas y con "=" no las alcanzaria ninguna.
                            cur.execute(f"""
                                {action_sql}
                                WHERE notification_key = %s AND user_id = %s
                                  AND entity_id IS NOT DISTINCT FROM %s AND id != %s
                            """, [notif_key, user_id, entity_id, keep_id])
                        else:
                            cur.execute(f"""
                                {action_sql}
                                WHERE notification_key = %s AND user_id = %s AND id != %s
                            """, [notif_key, user_id, keep_id])

                        # Reescribimos el dedupe_key de la que sobrevive (la mas reciente, MAX(id))
                        # Si justo chocara porque ya existe una con el new_dedupe_key (ej. si se corre 2 veces), 
                        # podemos ignorarlo porque ya estara consolidado.
                        cur.execute("""
                            UPDATE notifications
                            SET dedupe_key = %s
                            WHERE id = %s
                            AND NOT EXISTS (
                                SELECT 1 FROM notifications n2 
                                WHERE n2.user_id = notifications.user_id 
                                  AND n2.notification_key = notifications.notification_key 
                                  AND n2.dedupe_key = %s
                                  AND n2.id != notifications.id
                            )
                        """, [new_dedupe_key, keep_id, new_dedupe_key])
                        
        if not dry_run:
            self.stdout.write("\nProceso finalizado. Cambios aplicados con exito.")
        else:
            self.stdout.write("\nProceso finalizado. No se aplicaron cambios.")
