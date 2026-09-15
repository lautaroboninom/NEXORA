"""Alta de un equipo propio nuevo para el parque de alquiler.

El modo "propio" del alta declara un equipo nuestro que nunca estuvo alquilado ni vendido.
Tiene que dejar tres cosas coherentes entre si, porque si alguna falla el equipo queda
invisible o el stock termina en el deposito equivocado:

* el equipo a nombre de MG BIO (es el dueno que reconocen los ruteos de stock);
* la ubicacion en la estanteria de alquiler, resuelta POR NOMBRE (los ids de `locations`
  son datos de instancia: en produccion la ubicacion '-' es la 10, no la 4);
* la transferencia de stock a STL encolada, porque "disponible para alquiler" es la
  interseccion de la ubicacion con stock positivo en STL.

`SimpleTestCase` con la view instanciada a mano: no necesita Postgres. Lo que se ejercita es
la decision de la view, no el INSERT.
"""
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from service.views.devices_views import DeviceDirectCreateView


def _drf_request(factory, payload):
    """Request de DRF: la view lee `request.data`, que el WSGIRequest crudo no tiene."""
    request = Request(
        factory.post("/api/devices/alta-directa/", payload, format="json"),
        parsers=[JSONParser()],
    )
    request.user = type("U", (), {"id": 1})()
    return request


def _post_sin_transaccion(view, request):
    """Corre la view sin su `@transaction.atomic`.

    El decorador abre una conexion antes de ejecutar nada y `SimpleTestCase` las prohibe.
    Lo que se prueba aca es la decision de la view, no la atomicidad del INSERT.
    """
    return type(view).post.__wrapped__(view, request)


MG_OWNER_ID = 7
SHELF_ID = 3
OTRA_INSTITUCION_ID = 42


class AltaEquipoPropioTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = DeviceDirectCreateView()

    def _post(self, payload, *, enqueue_side_effect=None, duplicados=None, mg_consultado=None):
        """Corre la view con la capa SQL mockeada.

        `duplicados` permite simular que la serie o el MG ya existen; por defecto no hay
        conflictos y las consultas de catalogo devuelven filas validas. `mg_consultado`
        recoge el numero interno con el que se busca el duplicado.
        """
        duplicados = duplicados or {}

        def fake_q(sql, params=None, one=False):
            texto = " ".join(str(sql).split())
            if "FROM customers WHERE id=" in texto:
                return {"id": params[0]}
            if "FROM locations WHERE LOWER(nombre)" in texto:
                return {"id": SHELF_ID} if duplicados.get("shelf", True) else None
            if "FROM locations WHERE id=" in texto:
                return {"id": params[0]}
            if "FROM marcas WHERE id=" in texto:
                return {"id": params[0]}
            if "FROM models WHERE id=" in texto:
                return {"id": params[0], "marca_id": 5}
            if "REPLACE(REPLACE(UPPER(numero_serie)" in texto:
                return duplicados.get("ns")
            if "numero_interno ~*" in texto:
                if mg_consultado is not None:
                    mg_consultado.append(params[0])
                return duplicados.get("mg")
            if "FROM devices d" in texto:
                return {"id": 4412, "numero_serie": "B3CBE407167"}
            return None

        request = _drf_request(self.factory, payload)
        with (
            patch("service.views.devices_views.require_roles", return_value=None),
            patch("service.views.devices_views._set_audit_user", return_value=None),
            patch("service.views.devices_views.ensure_default_locations", return_value=None),
            patch("service.views.devices_views._mg_owner_customer_id", return_value=MG_OWNER_ID),
            patch("service.views.devices_views.q", side_effect=fake_q),
            patch("service.views.devices_views.exec_returning", return_value=4412) as insert,
            patch(
                "service.views.devices_views.enqueue_stock_val_to_stl_for_device",
                side_effect=enqueue_side_effect,
                return_value={"id": 901, "status": "pending"},
            ) as enqueue,
        ):
            response = _post_sin_transaccion(self.view, request)
        return response, insert, enqueue

    def _base(self, **cambios):
        return {
            "propio": True,
            "marca_id": 5,
            "model_id": 88,
            "tipo_equipo": "CPAP",
            "numero_serie": "B3CBE407167",
            "numero_interno": "MG 7450",
            "empresa": "SEPID",
            "stock_deposito": "VAL",
            "stock_article_code": "1104011",
            **cambios,
        }

    def _insert_params(self, insert):
        return insert.call_args[0][1]

    def test_el_equipo_queda_a_nombre_de_mg_bio_en_la_estanteria_y_sin_alquilar(self):
        response, insert, _ = self._post(self._base())

        self.assertEqual(response.status_code, 201, response.data)
        params = self._insert_params(insert)
        self.assertEqual(params[0], MG_OWNER_ID)  # customer_id
        self.assertEqual(params[7], SHELF_ID)  # ubicacion_id
        self.assertFalse(params[8])  # alquilado
        self.assertEqual(params[9], "")  # alquiler_a

    def test_ignora_la_institucion_que_venga_en_el_payload(self):
        # El cliente de un equipo propio no se elige: si el front mandara uno, mandaria el
        # equipo al parque de otro y romperia el ruteo de stock.
        _, insert, _ = self._post(self._base(customer_id=OTRA_INSTITUCION_ID))
        self.assertEqual(self._insert_params(insert)[0], MG_OWNER_ID)

    def test_encola_la_transferencia_del_deposito_elegido_a_stl(self):
        response, _, enqueue = self._post(self._base(stock_deposito="SAR"))

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args[0][0], 4412)
        self.assertEqual(enqueue.call_args[1]["company_key"], "SEPID")
        self.assertEqual(enqueue.call_args[1]["source_deposit"], "SAR")
        self.assertEqual(response.data["stock_job"]["status"], "pending")

    def test_sin_deposito_elegido_deja_decidir_al_encolador(self):
        _, _, enqueue = self._post(self._base(stock_deposito=""))
        self.assertIsNone(enqueue.call_args[1]["source_deposit"])

    def test_el_articulo_elegido_viaja_al_job(self):
        # Sin el artículo del renglón de stock, un modelo todavía sin mapear bloquea el job
        # pidiendo confirmacion manual de un dato que el operador ya tenia en pantalla.
        _, _, enqueue = self._post(self._base(stock_article_code="1104011"))
        self.assertEqual(enqueue.call_args[1]["article_code"], "1104011")

    def test_la_empresa_viaja_al_job(self):
        _, _, enqueue = self._post(self._base(empresa="MGBIO"))
        self.assertEqual(enqueue.call_args[1]["company_key"], "MGBIO")

    def test_una_empresa_inexistente_se_rechaza(self):
        response, insert, _ = self._post(self._base(empresa="NO-EXISTE"))
        self.assertEqual(response.status_code, 400)
        insert.assert_not_called()

    def test_sin_serie_no_hay_nada_que_transferir(self):
        response, insert, enqueue = self._post(self._base(numero_serie=""))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["conflict_type"], "PROPIO_SERIE_REQUERIDA")
        insert.assert_not_called()
        enqueue.assert_not_called()

    def test_sin_mg_no_se_da_de_alta(self):
        response, insert, _ = self._post(self._base(numero_interno=""))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["conflict_type"], "PROPIO_MG_REQUERIDO")
        insert.assert_not_called()

    def test_sin_estanteria_cargada_no_deja_el_equipo_invisible(self):
        response, insert, _ = self._post(self._base(), duplicados={"shelf": False})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["conflict_type"], "RENTAL_SHELF_MISSING")
        insert.assert_not_called()

    def test_guarda_el_mg_normalizado_aunque_se_escriba_pegado(self):
        # `MG1234` y `MG 1234` son el mismo equipo: si se guardan distinto, todo lo que
        # compara por texto exacto los ve como dos.
        _, insert, _ = self._post(self._base(numero_interno="mg1234"))
        self.assertEqual(self._insert_params(insert)[4], "MG 1234")

    def test_el_mg_normalizado_es_el_que_se_chequea_contra_duplicados(self):
        # Si el chequeo corriera sobre el texto crudo, `MG001678` no chocaría con la ficha
        # ya cargada como `MG 1678` y quedarían dos equipos para la misma etiqueta.
        consultados = []
        _, insert, _ = self._post(
            self._base(numero_interno="MG001678"), mg_consultado=consultados
        )
        self.assertEqual(consultados, ["MG 1678"])
        self.assertEqual(self._insert_params(insert)[4], "MG 1678")

    def test_una_serie_ya_fichada_no_se_duplica(self):
        response, insert, enqueue = self._post(self._base(), duplicados={"ns": {"id": 999}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["conflict_type"], "NS_DUPLICATE")
        insert.assert_not_called()
        enqueue.assert_not_called()

    def test_el_alta_sobrevive_a_un_encolado_bloqueado_pero_lo_informa(self):
        # El equipo ya quedo bien registrado: lo que falta es el movimiento de stock, y eso
        # se dice en la respuesta en vez de tirar abajo el alta entera.
        from service.bejerman_sync import BejermanBlockedError

        response, insert, _ = self._post(
            self._base(), enqueue_side_effect=BejermanBlockedError("Equipo vendido")
        )
        self.assertEqual(response.status_code, 201)
        insert.assert_called_once()
        self.assertEqual(response.data["stock_job"]["status"], "blocked")
        self.assertIn("Equipo vendido", response.data["stock_job"]["last_error"])


class AltaNormalSinCambiosTests(SimpleTestCase):
    """El alta de siempre no puede cambiar: sigue pidiendo institución y sin tocar stock."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = DeviceDirectCreateView()

    def _post(self, payload):
        def fake_q(sql, params=None, one=False):
            texto = " ".join(str(sql).split())
            if "FROM customers WHERE id=" in texto:
                return {"id": params[0]}
            if "FROM marcas WHERE id=" in texto:
                return {"id": params[0]}
            if "FROM models WHERE id=" in texto:
                return {"id": params[0], "marca_id": 5}
            if "FROM devices d" in texto:
                return {"id": 4413}
            return None

        request = _drf_request(self.factory, payload)
        with (
            patch("service.views.devices_views.require_roles", return_value=None),
            patch("service.views.devices_views._set_audit_user", return_value=None),
            patch("service.views.devices_views.q", side_effect=fake_q),
            patch("service.views.devices_views.exec_returning", return_value=4413) as insert,
            patch("service.views.devices_views.enqueue_stock_val_to_stl_for_device") as enqueue,
        ):
            response = _post_sin_transaccion(self.view, request)
        return response, insert, enqueue

    def test_un_alta_comun_no_encola_ningun_movimiento_de_stock(self):
        response, insert, enqueue = self._post(
            {
                "customer_id": OTRA_INSTITUCION_ID,
                "marca_id": 5,
                "model_id": 88,
                "tipo_equipo": "CPAP",
                "numero_serie": "SER-CLIENTE-1",
            }
        )
        self.assertEqual(response.status_code, 201, response.data)
        insert.assert_called_once()
        enqueue.assert_not_called()
        self.assertNotIn("stock_job", response.data)

    def test_sigue_exigiendo_la_institucion(self):
        response, insert, _ = self._post({"marca_id": 5, "model_id": 88, "numero_serie": "X"})
        self.assertEqual(response.status_code, 400)
        insert.assert_not_called()
