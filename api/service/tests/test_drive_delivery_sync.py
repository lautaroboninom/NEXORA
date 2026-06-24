from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from service.drive_delivery_sync import (
    DEFAULT_SHEET_NAME,
    DriveDeliverySyncError,
    _next_free_row,
    delivery_order_to_drive_row,
    sync_delivery_orders_to_drive,
)
from service.views.delivery_orders_views import DeliveryOrderDriveSyncView


class FakeSheetsClient:
    def __init__(self, rows):
        self.rows = rows
        self.updated_range = ""
        self.updated_values = []
        self.config = SimpleNamespace(sheet_id="sheet-test", sheet_name=DEFAULT_SHEET_NAME)

    def get_values(self, range_name):
        self.read_range = range_name
        return self.rows

    def update_values(self, range_name, values):
        self.updated_range = range_name
        self.updated_values = values
        return {"updatedRange": range_name}


class FakeHttpResponse:
    def __init__(self, payload, *, ok=True, status_code=200, text=""):
        self.payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


def order(**overrides):
    base = {
        "id": "do-1",
        "orderNumber": "OE-00001",
        "customerName": "OXIDOM S.R.L.",
        "deliveryType": "sale",
        "companyKey": "SEPID",
        "status": "pendiente_armado",
        "orderDate": "2026-06-23",
        "sellerCode": "EZE",
        "sellerName": "EZE Ezequiel Merino",
        "operationCompanyLabel": "",
        "rawPedido": "Pedido listo",
        "remitoNumber": "",
        "invoiceNumber": "",
        "sourceReference": "",
        "ingresoId": None,
        "items": [],
    }
    base.update(overrides)
    return base


class DriveDeliverySyncHelpersTests(SimpleTestCase):
    def test_maps_delivery_order_to_drive_columns(self):
        row = delivery_order_to_drive_row(
            order(
                status="facturado",
                remitoNumber="RT 0002-00012345",
                invoiceNumber="FC 0001-00000123",
            )
        )

        self.assertEqual(
            row,
            [
                "Ezequiel",
                "2026-06-23",
                "OXIDOM S.R.L.",
                "SEPID",
                "Pedido listo",
                True,
                "RT 0002-00012345",
                "-",
                "FC 0001-00000123",
            ],
        )

    def test_maps_service_release_to_reparacion_and_os(self):
        row = delivery_order_to_drive_row(
            order(
                deliveryType="service_release",
                companyKey="MGBIO",
                sellerCode="",
                rawPedido="reparado",
                equipmentModel="Respironics Trilogy",
                equipmentSerial="SN-123",
                sourceReference="OS 42",
            )
        )

        self.assertEqual(row[0], "Administración")
        self.assertEqual(row[3], "REPARACION")
        self.assertEqual(row[4], "Respironics Trilogy SN-123")
        self.assertEqual(row[6], "")
        self.assertEqual(row[7], "OS-00042")

    def test_next_free_row_ignores_false_checkbox_only_rows(self):
        rows = [
            ["VENDEDOR", "FECHA", "CLIENTE", "EMPRESA", "PEDIDO", "ENTREGADO", "RT", "OS", "FC"],
            ["Ezequiel", "2026-06-23", "OXIDOM", "SEPID", "Pedido", False, "", "-", ""],
            ["", "", "", "", "", False, "", "", ""],
        ]

        self.assertEqual(_next_free_row(rows), 3)

    def test_sync_filters_cancelled_tests_and_existing_rows(self):
        orders = [
            order(id="new", rawPedido="Pedido nuevo"),
            order(id="cancelled", status="cancelado", rawPedido="Cancelado"),
            order(id="test", rawPedido="Prueba", items=[{"partida": "demo123"}]),
            order(
                id="service-duplicate",
                deliveryType="service_release",
                sourceReference="OS-00042",
                rawPedido="reparado",
            ),
        ]
        existing_rows = [
            ["VENDEDOR", "FECHA", "CLIENTE", "EMPRESA", "PEDIDO", "ENTREGADO", "RT", "OS", "FC"],
            ["Administración", "2026-06-23", "Cliente", "REPARACION", "Equipo", False, "", "OS-00042", ""],
            ["", "", "", "", "", False, "", "", ""],
        ]
        client = FakeSheetsClient(existing_rows)

        with patch("service.drive_delivery_sync._load_orders_for_sync", return_value=orders) as load_mock:
            result = sync_delivery_orders_to_drive(
                sheets_client=client,
                today=date(2026, 6, 24),
            )

        load_mock.assert_called_once_with(date(2026, 6, 23), date(2026, 6, 24))
        self.assertEqual(result["createdRows"], 1)
        self.assertEqual(result["alreadyInDrive"], 1)
        self.assertEqual(result["excludedCancelled"], 1)
        self.assertEqual(result["excludedTest"], 1)
        self.assertEqual(result["range"], "A3:I3")
        self.assertEqual(client.updated_range, f"{DEFAULT_SHEET_NAME}!A3:I3")
        self.assertEqual(client.updated_values[0][4], "Pedido nuevo")

    def test_sync_deduplicates_sales_by_visible_signature(self):
        orders = [order(id="duplicate", rawPedido="Pedido listo")]
        existing_rows = [
            ["VENDEDOR", "FECHA", "CLIENTE", "EMPRESA", "PEDIDO", "ENTREGADO", "RT", "OS", "FC"],
            ["Ezequiel", "2026-06-23", "OXIDOM S.R.L.", "SEPID", "Pedido listo", False, "", "-", ""],
        ]
        client = FakeSheetsClient(existing_rows)

        with patch("service.drive_delivery_sync._load_orders_for_sync", return_value=orders):
            result = sync_delivery_orders_to_drive(sheets_client=client, today=date(2026, 6, 23))

        self.assertEqual(result["createdRows"], 0)
        self.assertEqual(result["alreadyInDrive"], 1)
        self.assertEqual(client.updated_values, [])

    def test_sync_filters_plain_asd_dummy_orders(self):
        orders = [
            order(id="new", customerName="OXIDOM S.R.L.", rawPedido="Pedido real"),
            order(id="dummy-client", customerName="asdasd", rawPedido="Pedido real"),
            order(id="dummy-pedido", customerName="AC 24", rawPedido="asdads"),
        ]
        existing_rows = [
            ["VENDEDOR", "FECHA", "CLIENTE", "EMPRESA", "PEDIDO", "ENTREGADO", "RT", "OS", "FC"],
        ]
        client = FakeSheetsClient(existing_rows)

        with patch("service.drive_delivery_sync._load_orders_for_sync", return_value=orders):
            result = sync_delivery_orders_to_drive(sheets_client=client, today=date(2026, 6, 23))

        self.assertEqual(result["createdRows"], 1)
        self.assertEqual(result["excludedTest"], 2)
        self.assertEqual(client.updated_values[0][2], "OXIDOM S.R.L.")

    def test_apps_script_sync_posts_rows_without_service_account_credentials(self):
        orders = [
            order(id="new", rawPedido="Pedido nuevo"),
            order(id="cancelled", status="cancelado", rawPedido="Cancelado"),
            order(id="test", rawPedido="Prueba", items=[{"partida": "demo123"}]),
        ]
        response = FakeHttpResponse(
            {
                "ok": True,
                "createdRows": 1,
                "alreadyInDrive": 0,
                "range": "A5009:I5009",
                "startRow": 5009,
                "endRow": 5009,
            }
        )

        with patch.dict(
            "os.environ",
            {
                "DRIVE_DELIVERY_SYNC_WEBAPP_URL": "https://script.google.com/macros/s/test/exec",
                "DRIVE_DELIVERY_SYNC_SECRET": "secret-test",
                "DRIVE_DELIVERY_SYNC_SHEET_ID": "sheet-test",
                "DRIVE_DELIVERY_SYNC_SHEET_NAME": DEFAULT_SHEET_NAME,
            },
            clear=False,
        ), patch("service.drive_delivery_sync._load_orders_for_sync", return_value=orders), patch(
            "service.drive_delivery_sync.requests.post",
            return_value=response,
        ) as post_mock, patch(
            "service.drive_delivery_sync.load_drive_sync_config",
            side_effect=AssertionError("No debe pedir credenciales de cuenta de servicio."),
        ):
            result = sync_delivery_orders_to_drive(today=date(2026, 6, 23))

        post_mock.assert_called_once()
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["secret"], "secret-test")
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0][4], "Pedido nuevo")
        self.assertEqual(result["backend"], "apps_script")
        self.assertEqual(result["createdRows"], 1)
        self.assertEqual(result["alreadyInDrive"], 0)
        self.assertEqual(result["excludedCancelled"], 1)
        self.assertEqual(result["excludedTest"], 1)
        self.assertEqual(result["range"], "A5009:I5009")

    def test_apps_script_sync_requires_secret(self):
        with patch.dict(
            "os.environ",
            {
                "DRIVE_DELIVERY_SYNC_WEBAPP_URL": "https://script.google.com/macros/s/test/exec",
                "DRIVE_DELIVERY_SYNC_SECRET": "",
            },
            clear=False,
        ):
            with self.assertRaises(DriveDeliverySyncError) as ctx:
                sync_delivery_orders_to_drive(today=date(2026, 6, 23))

        self.assertEqual(ctx.exception.code, "DRIVE_SYNC_APPS_SCRIPT_CONFIG_MISSING")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_apps_script_sync_returns_clear_error(self):
        response = FakeHttpResponse({"ok": False, "error": "Token inválido"})

        with patch.dict(
            "os.environ",
            {
                "DRIVE_DELIVERY_SYNC_WEBAPP_URL": "https://script.google.com/macros/s/test/exec",
                "DRIVE_DELIVERY_SYNC_SECRET": "secret-test",
            },
            clear=False,
        ), patch("service.drive_delivery_sync._load_orders_for_sync", return_value=[order()]), patch(
            "service.drive_delivery_sync.requests.post",
            return_value=response,
        ):
            with self.assertRaises(DriveDeliverySyncError) as ctx:
                sync_delivery_orders_to_drive(today=date(2026, 6, 23))

        self.assertEqual(ctx.exception.code, "DRIVE_SYNC_APPS_SCRIPT_ERROR")
        self.assertEqual(ctx.exception.status_code, 502)


class DeliveryOrderDriveSyncViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = DeliveryOrderDriveSyncView.as_view()

    def _request(self, role):
        request = self.factory.post("/api/ordenes-entrega/sincronizar-drive/", {}, format="json")
        user = SimpleNamespace(id=1, rol=role, is_authenticated=True)
        force_authenticate(request, user=user)
        return request

    def test_admin_can_sync(self):
        with patch(
            "service.views.delivery_orders_views.sync_delivery_orders_to_drive",
            return_value={"ok": True, "createdRows": 1, "alreadyInDrive": 0},
        ) as sync_mock:
            response = self.view(self._request("admin"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["createdRows"], 1)
        sync_mock.assert_called_once_with()

    def test_ventas_can_sync(self):
        with patch(
            "service.views.delivery_orders_views.sync_delivery_orders_to_drive",
            return_value={"ok": True, "createdRows": 1, "alreadyInDrive": 0},
        ) as sync_mock:
            response = self.view(self._request("ventas"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["createdRows"], 1)
        sync_mock.assert_called_once_with()

    def test_jefe_can_sync(self):
        with patch(
            "service.views.delivery_orders_views.sync_delivery_orders_to_drive",
            return_value={"ok": True, "createdRows": 1, "alreadyInDrive": 0},
        ) as sync_mock:
            response = self.view(self._request("jefe"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["createdRows"], 1)
        sync_mock.assert_called_once_with()

    def test_non_admin_cannot_sync(self):
        with patch("service.views.delivery_orders_views.sync_delivery_orders_to_drive") as sync_mock:
            response = self.view(self._request("recepcion"))

        self.assertEqual(response.status_code, 403)
        sync_mock.assert_not_called()

    def test_missing_credentials_returns_clear_error(self):
        with patch(
            "service.views.delivery_orders_views.sync_delivery_orders_to_drive",
            side_effect=DriveDeliverySyncError(
                "DRIVE_SYNC_CREDENTIALS_MISSING",
                "Falta configurar GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_APPLICATION_CREDENTIALS.",
                status_code=503,
            ),
        ):
            response = self.view(self._request("ventas"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "DRIVE_SYNC_CREDENTIALS_MISSING")
