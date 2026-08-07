from unittest.mock import patch

from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.tests.test_bejerman_rental_remito import RentalRemitoStateTests


class IngresoAlquilerRemitoViewsTests(RentalRemitoStateTests):
    def setUp(self):
        super().setUp()
        self.jefe_user = User.objects.create(
            nombre="Jefe Alquiler Views",
            email="jefe-alquiler-views@rental-remito.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        self.jefe_token = issue_token(self.jefe_user)
        self.tecnico_user = User.objects.create(
            nombre="Tecnico Alquiler Views",
            email="tecnico-alquiler-views@rental-remito.test",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        self.tecnico_token = issue_token(self.tecnico_user)
        self.client = APIClient()

    def test_estado_view_returns_diagnostic_state(self):
        fake_state = {"ingresoId": self.ingreso_id, "status": "verified", "code": "RENTAL_REMITO_VERIFIED"}
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")
        with patch(
            "service.views.ingresos_views.get_rental_remito_state",
            return_value=fake_state,
        ) as mocked:
            resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/alquiler/remito/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "verified")
        mocked.assert_called_once()

    def test_accion_view_executes_action_with_permission(self):
        fake_result = {"status": "succeeded", "idempotent": False, "state": {"status": "verified"}}
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")
        with patch(
            "service.views.ingresos_views.execute_rental_remito_action",
            return_value=fake_result,
        ) as mocked:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/alquiler/remito/accion/",
                {"action": "correct_stl_exit"},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "succeeded")
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.args[:2], (self.ingreso_id, "correct_stl_exit"))

    def test_accion_view_denies_role_without_permission(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tecnico_token}")
        with patch("service.views.ingresos_views.execute_rental_remito_action") as mocked:
            resp = self.client.post(
                f"/api/ingresos/{self.ingreso_id}/alquiler/remito/accion/",
                {"action": "correct_stl_exit"},
                format="json",
            )

        self.assertEqual(resp.status_code, 403)
        mocked.assert_not_called()

    def test_estado_view_surfaces_rental_remito_error_for_legacy_remito(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")
        resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/alquiler/remito/?remito=23858")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "RENTAL_REMITO_INVALID")
