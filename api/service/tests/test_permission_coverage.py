"""Toda vista ruteada tiene que decir qué permiso pide.

La pantalla de Usuarios edita permisos por usuario, pero esos permisos sólo cuentan donde la
vista está en `VIEW_PERMISSION_MATRIX`. Sin entrada, una vista con `MappedPermissionGuard`
deja pasar a cualquier usuario logueado y una con `require_roles` mira sólo el rol: en los
dos casos lo que se configura en Usuarios no tiene efecto. Así entraron varias pantallas y
acciones nuevas sin que nadie pudiera darlas o quitarlas por usuario.

Estos tests recorren las rutas reales en vez de confiar en que alguien se acuerde. Si uno
falla, la vista nueva va a la matriz; o, si de verdad no corresponde, a
`PERMISSION_EXEMPT_VIEWS` con el motivo escrito.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import get_resolver
from rest_framework.views import APIView

from service.permission_catalog import PERMISSION_CODES_SET, ROLE_DEFAULTS
from service.permission_policy import PERMISSION_EXEMPT_VIEWS, VIEW_PERMISSION_MATRIX

HTTP_METHODS = ("get", "post", "put", "patch", "delete")

FRONT_PERMISSIONS = Path(__file__).resolve().parents[3] / "web" / "src" / "lib" / "permissions.js"


def _iter_patterns(patterns):
    for pattern in patterns:
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            yield from _iter_patterns(nested)
        else:
            yield pattern


def _routed_view_classes():
    classes = set()
    for pattern in _iter_patterns(get_resolver().url_patterns):
        callback = getattr(pattern, "callback", None)
        view_class = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        if isinstance(view_class, type) and issubclass(view_class, APIView):
            classes.add(view_class)
    return classes


def _exempt(view_class, method=None):
    if any(klass.__name__ in PERMISSION_EXEMPT_VIEWS for klass in view_class.__mro__):
        return True
    return method is not None and f"{view_class.__name__}.{method.upper()}" in PERMISSION_EXEMPT_VIEWS


class PermissionMatrixCoverageTests(SimpleTestCase):
    def test_toda_vista_ruteada_esta_en_la_matriz_o_exenta(self):
        faltantes = []
        for view_class in _routed_view_classes():
            if _exempt(view_class):
                continue
            mapped = VIEW_PERMISSION_MATRIX.get(view_class.__name__, {})
            for method in HTTP_METHODS:
                if not hasattr(view_class, method):
                    continue
                if method.upper() in mapped or _exempt(view_class, method):
                    continue
                faltantes.append(f"{view_class.__name__}.{method.upper()}")

        self.assertEqual(
            sorted(faltantes),
            [],
            "Vistas sin permiso en VIEW_PERMISSION_MATRIX ni exención en PERMISSION_EXEMPT_VIEWS",
        )

    def test_la_matriz_solo_usa_codigos_del_catalogo(self):
        # Un código mal escrito no da error: `user_has_permission` lo niega a todos, Jefe
        # incluido, y la pantalla queda cerrada sin que nadie sepa por qué.
        invalidos = []
        for view_name, methods in VIEW_PERMISSION_MATRIX.items():
            for method, required in methods.items():
                codes = [required] if isinstance(required, str) else list(required)
                invalidos.extend(
                    f"{view_name}.{method}: {code}" for code in codes if code not in PERMISSION_CODES_SET
                )

        self.assertEqual(invalidos, [])

    def test_las_exenciones_nombran_vistas_ruteadas(self):
        routed = set()
        for view_class in _routed_view_classes():
            routed.update(klass.__name__ for klass in view_class.__mro__)
        sobrantes = sorted(
            key for key in PERMISSION_EXEMPT_VIEWS if key.split(".", 1)[0] not in routed
        )

        self.assertEqual(sobrantes, [])


def _front_role_defaults(source):
    codes = dict(re.findall(r'^  ([A-Z0-9_]+): "([a-z0-9_.]+)",$', source, re.M))
    roles = {}
    for role, body in re.findall(r"^  ([a-z_]+): new Set\(\[(.*?)\]\),?$", source, re.M | re.S):
        roles[role] = {codes[key] for key in re.findall(r"PERMISSION_CODES\.([A-Z0-9_]+)", body)}
    supervisor = re.search(r"ROLE_DEFAULT_PERMISSIONS\.supervisor = new Set\(\[(.*?)\]\);", source, re.S)
    if supervisor:
        extra = {codes[key] for key in re.findall(r"PERMISSION_CODES\.([A-Z0-9_]+)", supervisor.group(1))}
        spread = re.findall(r"\.\.\.ROLE_DEFAULT_PERMISSIONS\.([a-z_]+)", supervisor.group(1))
        roles["supervisor"] = extra.union(*(roles[name] for name in spread))
    return codes, roles


class FrontPermissionDefaultsTests(SimpleTestCase):
    """`web/src/lib/permissions.js` repite los defaults por rol como fallback de `can()`.

    Se desincronizó sin que nadie lo notara (a recepción le faltaban tres permisos). La
    sesión trae el mapa completo y tapa la diferencia, hasta que un código nuevo llega al
    front antes que al backend o un test del front usa los defaults.
    """

    def test_los_defaults_del_front_coinciden_con_el_backend(self):
        if not FRONT_PERMISSIONS.exists():
            self.skipTest("web/ no está disponible (por ejemplo, dentro del contenedor de la API)")
        source = FRONT_PERMISSIONS.read_text(encoding="utf-8").replace("\r\n", "\n")
        codes, roles = _front_role_defaults(source)

        self.assertEqual(set(codes.values()), PERMISSION_CODES_SET)
        self.assertIn("recepcion", roles)
        self.assertIn("supervisor", roles)
        for role, front in roles.items():
            back = {code for code, allowed in ROLE_DEFAULTS[role].items() if allowed}
            self.assertEqual(sorted(back - front), [], f"{role}: permisos que faltan en el front")
            self.assertEqual(sorted(front - back), [], f"{role}: permisos que sobran en el front")
