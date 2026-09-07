"""Pone y saca a alguien como reemplazo temporal de otra persona.

El caso real: el jefe se va de vacaciones y hay que dejar a alguien recibiendo sus
avisos y con permiso para resolver lo que llegue. A mano son tres cosas —cambiar el
rol, agregar un permiso, prender doce notificaciones— y una cuarta que es la que
falla: acordarse de deshacer las tres al volver.

Acá va todo junto y, sobre todo, **queda anotado cómo estaba antes**. Cerrar la
suplencia no es "volver a poner lo que me parece que tenía": es reescribir el estado
exacto que se guardó al abrirla.

Qué hace `--activar`:

1. Le pone el rol `jefe_suplente`, que es la unión de técnico y jefatura menos la
   configuración de usuarios. La unión importa: si el suplente es un técnico tiene
   que **seguir siendo asignable y seguir arreglando equipos**. Ponerle un rol de
   jefatura a secas lo sacaba de las listas de técnicos y dejaba de poder recibir
   equipos ("400: técnico inválido").
2. Le agrega los permisos sueltos que la suplencia necesite además del rol. Hoy
   ninguno: el rol ya los trae todos.
3. Le prende las notificaciones que el titular recibe por su rol y que el rol de
   suplencia permite pero no trae prendidas.

Como el resto de los comandos que tocan datos, **por defecto no escribe nada**:
imprime lo que haría. Con `--apply` lo aplica.
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from service.notifications import (
    CATALOG_BY_KEY,
    NOTIFICATION_CATALOG,
    _item_channels,
    _role_allowed_for_item,
    _role_default_enabled,
)

ROL_SUPLENCIA = "jefe_suplente"
ROL_TITULAR_POR_DEFECTO = "jefe"

# El rol `jefe_suplente` ya es la unión de técnico y jefatura menos la
# configuración de usuarios (ver `permission_catalog.SUPLENTE_EXCLUSIONES`), así que
# no hace falta agregarle ningún permiso suelto. Queda el mecanismo por si alguna
# suplencia puntual necesita uno de más.
PERMISOS_EXTRA = ()


def _rows(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        if not cur.description:
            return []
        columnas = [col[0] for col in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def _uno(sql, params=None):
    filas = _rows(sql, params)
    return filas[0] if filas else None


def notificaciones_a_prender(rol_suplente, rol_titular):
    """Las que el titular recibe por su rol y el suplente todavía no.

    Se calcula del catálogo en vez de escribirse a mano: si mañana se agrega un aviso
    nuevo para jefatura, la suplencia lo cubre sin que nadie se acuerde de venir acá.
    """

    claves = []
    for item in NOTIFICATION_CATALOG:
        if not _role_allowed_for_item(item, rol_suplente):
            continue
        if not _role_default_enabled(item["key"], rol_titular):
            continue
        if _role_default_enabled(item["key"], rol_suplente):
            continue  # Ya le llega sola.
        claves.append(item["key"])
    return claves


class Command(BaseCommand):
    help = "Activa o cierra una suplencia temporal (rol, permisos y notificaciones)"

    def add_arguments(self, parser):
        grupo = parser.add_mutually_exclusive_group(required=True)
        grupo.add_argument("--activar", action="store_true", help="Abre la suplencia")
        grupo.add_argument("--cerrar", action="store_true", help="Cierra la suplencia y revierte todo")
        grupo.add_argument("--estado", action="store_true", help="Lista las suplencias activas")

        parser.add_argument("--usuario", default="", help="Mail del suplente")
        parser.add_argument("--titular", default="", help="Mail del titular (opcional, queda registrado)")
        parser.add_argument("--rol", default=ROL_SUPLENCIA, help=f"Rol de suplencia (por defecto {ROL_SUPLENCIA})")
        parser.add_argument("--motivo", default="", help="Por qué se abre (queda en el registro)")
        parser.add_argument("--apply", action="store_true", help="Escribe los cambios (por defecto sólo informa)")

    # -- utilidades ---------------------------------------------------------

    def _usuario(self, mail, etiqueta):
        limpio = str(mail or "").strip().lower()
        if not limpio:
            raise CommandError(f"Falta --{etiqueta} (mail del usuario).")
        fila = _uno(
            "SELECT id, nombre, email, rol, activo FROM users WHERE lower(email) = %s",
            [limpio],
        )
        if not fila:
            raise CommandError(f"No existe un usuario con el mail {limpio}.")
        return fila

    def _preferencia_actual(self, user_id, clave):
        return _uno(
            """
            SELECT enabled, bell_enabled, email_enabled, push_enabled
              FROM notification_user_preferences
             WHERE user_id = %s AND notification_key = %s
            """,
            [user_id, clave],
        )

    # -- acciones -----------------------------------------------------------

    def handle(self, *args, **opts):
        if opts["estado"]:
            return self._estado()
        if opts["activar"]:
            return self._activar(opts)
        return self._cerrar(opts)

    def _estado(self):
        filas = _rows(
            """
            SELECT s.id, s.motivo, s.abierta_at,
                   u.nombre AS suplente, u.email AS suplente_mail, u.rol AS rol_actual,
                   t.nombre AS titular,
                   s.snapshot
              FROM suplencias s
              JOIN users u ON u.id = s.suplente_user_id
              LEFT JOIN users t ON t.id = s.titular_user_id
             WHERE s.estado = 'activa'
             ORDER BY s.abierta_at
            """
        )
        if not filas:
            self.stdout.write("No hay suplencias activas.")
            return
        for fila in filas:
            snapshot = fila["snapshot"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            desde = fila["abierta_at"].strftime("%d/%m/%Y %H:%M") if fila.get("abierta_at") else "-"
            self.stdout.write(
                f"#{fila['id']}  {fila['suplente']} <{fila['suplente_mail']}> cubre a "
                f"{fila.get('titular') or '-'} desde {desde}"
            )
            self.stdout.write(
                f"    rol ahora: {fila['rol_actual']} · antes era: {snapshot.get('rol_original')} · "
                f"permisos dados: {len(snapshot.get('permisos_otorgados') or [])} · "
                f"notificaciones tocadas: {len(snapshot.get('preferencias') or {})}"
            )
            if fila.get("motivo"):
                self.stdout.write(f"    motivo: {fila['motivo']}")

    def _activar(self, opts):
        aplicar = bool(opts["apply"])
        suplente = self._usuario(opts["usuario"], "usuario")
        titular = self._usuario(opts["titular"], "titular") if opts["titular"] else None
        rol_suplencia = str(opts["rol"] or ROL_SUPLENCIA).strip()
        rol_titular = (titular or {}).get("rol") or ROL_TITULAR_POR_DEFECTO

        if not suplente.get("activo"):
            raise CommandError(f"{suplente['nombre']} está desactivado: no puede cubrir a nadie.")
        abierta = _uno(
            "SELECT id FROM suplencias WHERE suplente_user_id = %s AND estado = 'activa'",
            [suplente["id"]],
        )
        if abierta:
            raise CommandError(
                f"{suplente['nombre']} ya tiene la suplencia #{abierta['id']} abierta. "
                "Ciérrela antes de abrir otra."
            )

        self.stdout.write(
            f"Suplente: {suplente['nombre']} <{suplente['email']}> — rol actual {suplente['rol']}"
        )
        self.stdout.write(f"Cubre el rol: {rol_titular} (con el rol de suplencia {rol_suplencia})")
        self.stdout.write("")

        # 1. Rol
        self.stdout.write(f"  ROL       {suplente['rol']} -> {rol_suplencia}")

        # 2. Permisos: sólo los que no tiene ya como override propio.
        existentes = {
            fila["permission_code"]
            for fila in _rows(
                "SELECT permission_code FROM user_permission_overrides WHERE user_id = %s AND effect = 'allow'",
                [suplente["id"]],
            )
        }
        a_otorgar = [codigo for codigo in PERMISOS_EXTRA if codigo not in existentes]
        for codigo in PERMISOS_EXTRA:
            if codigo in a_otorgar:
                self.stdout.write(f"  PERMISO   + {codigo}")
            else:
                self.stdout.write(f"  PERMISO   = {codigo} (ya lo tenía, no se toca ni se quitará)")

        # 3. Notificaciones
        claves = notificaciones_a_prender(rol_suplencia, rol_titular)
        preferencias_previas = {}
        for clave in claves:
            previa = self._preferencia_actual(suplente["id"], clave)
            preferencias_previas[clave] = previa
            etiqueta = CATALOG_BY_KEY[clave]["label"]
            marca = "~" if previa else "+"
            self.stdout.write(f"  AVISO     {marca} {etiqueta} ({clave})")

        snapshot = {
            "rol_original": suplente["rol"],
            "permisos_otorgados": a_otorgar,
            "preferencias": preferencias_previas,
        }

        self.stdout.write("")
        estado = "APLICADO" if aplicar else "SIMULACION, no se escribio nada"
        self.stdout.write(
            f"Rol a cambiar: 1 - Permisos a dar: {len(a_otorgar)} - "
            f"Notificaciones a prender: {len(claves)} ({estado})"
        )
        if not aplicar:
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE users SET rol = %s WHERE id = %s", [rol_suplencia, suplente["id"]])
                for codigo in a_otorgar:
                    cur.execute(
                        """
                        INSERT INTO user_permission_overrides (user_id, permission_code, effect)
                        VALUES (%s, %s, 'allow')
                        ON CONFLICT (user_id, permission_code)
                        DO UPDATE SET effect = 'allow', updated_at = CURRENT_TIMESTAMP
                        """,
                        [suplente["id"], codigo],
                    )
                for clave in claves:
                    canales = _item_channels(CATALOG_BY_KEY[clave])
                    cur.execute(
                        """
                        INSERT INTO notification_user_preferences
                               (user_id, notification_key, bell_enabled, email_enabled, push_enabled)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (user_id, notification_key) DO UPDATE
                           SET bell_enabled = EXCLUDED.bell_enabled,
                               email_enabled = EXCLUDED.email_enabled,
                               push_enabled = EXCLUDED.push_enabled,
                               updated_at = CURRENT_TIMESTAMP
                        """,
                        [
                            suplente["id"],
                            clave,
                            "bell" in canales,
                            "email" in canales,
                            "push" in canales,
                        ],
                    )
                cur.execute(
                    """
                    INSERT INTO suplencias
                           (titular_user_id, suplente_user_id, motivo, estado, snapshot)
                    VALUES (%s, %s, %s, 'activa', %s::jsonb)
                    RETURNING id
                    """,
                    [
                        (titular or {}).get("id"),
                        suplente["id"],
                        str(opts["motivo"] or ""),
                        json.dumps(snapshot, default=str),
                    ],
                )
                suplencia_id = cur.fetchone()[0]

        self.stdout.write(
            self.style.SUCCESS(
                f"Suplencia #{suplencia_id} abierta. Para cerrarla: "
                f"manage.py suplencia --cerrar --usuario {suplente['email']} --apply"
            )
        )

    def _cerrar(self, opts):
        aplicar = bool(opts["apply"])
        suplente = self._usuario(opts["usuario"], "usuario")
        fila = _uno(
            "SELECT id, snapshot FROM suplencias WHERE suplente_user_id = %s AND estado = 'activa'",
            [suplente["id"]],
        )
        if not fila:
            raise CommandError(f"{suplente['nombre']} no tiene ninguna suplencia abierta.")

        snapshot = fila["snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        rol_original = snapshot.get("rol_original")
        permisos = list(snapshot.get("permisos_otorgados") or [])
        preferencias = dict(snapshot.get("preferencias") or {})

        self.stdout.write(f"Cerrando la suplencia #{fila['id']} de {suplente['nombre']}")
        self.stdout.write(f"  ROL       {suplente['rol']} -> {rol_original}")
        for codigo in permisos:
            self.stdout.write(f"  PERMISO   - {codigo}")
        for clave, previa in preferencias.items():
            self.stdout.write(f"  AVISO     {'restaura' if previa else 'borra'} {clave}")

        self.stdout.write("")
        estado = "APLICADO" if aplicar else "SIMULACION, no se escribio nada"
        self.stdout.write(
            f"Permisos a quitar: {len(permisos)} - Notificaciones a revertir: {len(preferencias)} ({estado})"
        )
        if not aplicar:
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                if rol_original:
                    cur.execute("UPDATE users SET rol = %s WHERE id = %s", [rol_original, suplente["id"]])
                for codigo in permisos:
                    # Sólo los que otorgó la suplencia: los que ya tenía nunca entraron
                    # en la lista, así que no se los sacamos.
                    cur.execute(
                        "DELETE FROM user_permission_overrides WHERE user_id = %s AND permission_code = %s",
                        [suplente["id"], codigo],
                    )
                for clave, previa in preferencias.items():
                    if not previa:
                        cur.execute(
                            "DELETE FROM notification_user_preferences WHERE user_id = %s AND notification_key = %s",
                            [suplente["id"], clave],
                        )
                        continue
                    cur.execute(
                        """
                        UPDATE notification_user_preferences
                           SET enabled = %s, bell_enabled = %s, email_enabled = %s,
                               push_enabled = %s, updated_at = CURRENT_TIMESTAMP
                         WHERE user_id = %s AND notification_key = %s
                        """,
                        [
                            previa.get("enabled"),
                            previa.get("bell_enabled"),
                            previa.get("email_enabled"),
                            previa.get("push_enabled"),
                            suplente["id"],
                            clave,
                        ],
                    )
                cur.execute(
                    "UPDATE suplencias SET estado = 'cerrada', cerrada_at = CURRENT_TIMESTAMP WHERE id = %s",
                    [fila["id"]],
                )

        self.stdout.write(self.style.SUCCESS(f"Suplencia #{fila['id']} cerrada. {suplente['nombre']} quedó como estaba."))
