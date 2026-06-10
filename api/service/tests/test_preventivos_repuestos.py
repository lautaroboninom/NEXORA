import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from service.views import preventivos_views


class PreventivosRepuestosLogicTests(SimpleTestCase):
    def test_repuesto_key_normaliza_catalogo_y_texto(self):
        self.assertEqual(preventivos_views._repuesto_key(12, "Filtro"), "cat:12")
        self.assertEqual(
            preventivos_views._repuesto_key(None, "  Filtro   Hepa  "),
            "txt:filtro hepa",
        )

    @patch("service.views.preventivos_views._agenda_plan_rows")
    def test_agenda_plan_items_usa_repuesto_mas_proximo(self, mock_rows):
        mock_rows.return_value = [
            {
                "scope_type": "device",
                "plan_id": 10,
                "device_id": 55,
                "owner_customer_id": 3,
                "customer_nombre": "Clinica A",
                "customer_cod_empresa": "C1",
                "marca": "ResMed",
                "modelo": "X1",
                "numero_serie": "NS1",
                "numero_interno": "MG 0001",
                "periodicidad_valor": 12,
                "periodicidad_unidad": "meses",
                "aviso_anticipacion_dias": 30,
                "ultima_revision_fecha": None,
                "proxima_revision_fecha": None,
                "repuestos_total": 2,
                "repuesto_proximo_id": 77,
                "repuesto_proximo_nombre": "Filtro",
                "repuesto_ultima_revision_fecha": dt.date(2026, 3, 1),
                "repuesto_proxima_revision_fecha": dt.date(2026, 3, 20),
                "repuesto_aviso_anticipacion_dias": 15,
                "tipo_equipo": "CPAP",
                "variante": "",
                "borrador_revision_id": None,
            }
        ]
        out = preventivos_views._agenda_plan_items(scope="device")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["preventivo_source"], "repuesto")
        self.assertEqual(out[0]["repuestos_total"], 2)
        self.assertEqual(out[0]["repuesto_proximo_id"], 77)
        self.assertEqual(out[0]["repuesto_proximo_nombre"], "Filtro")
        self.assertEqual(out[0]["proxima_revision_fecha"], dt.date(2026, 3, 20))

    @patch("service.views.preventivos_views._agenda_plan_rows")
    def test_agenda_plan_items_fallback_plan_sin_repuestos(self, mock_rows):
        mock_rows.return_value = [
            {
                "scope_type": "device",
                "plan_id": 11,
                "device_id": 56,
                "owner_customer_id": 4,
                "customer_nombre": "Clinica B",
                "customer_cod_empresa": "C2",
                "marca": "ResMed",
                "modelo": "X2",
                "numero_serie": "NS2",
                "numero_interno": "MG 0002",
                "periodicidad_valor": 12,
                "periodicidad_unidad": "meses",
                "aviso_anticipacion_dias": 20,
                "ultima_revision_fecha": dt.date(2026, 1, 1),
                "proxima_revision_fecha": dt.date(2026, 4, 1),
                "repuestos_total": 0,
                "repuesto_proximo_id": None,
                "repuesto_proximo_nombre": "",
                "repuesto_ultima_revision_fecha": None,
                "repuesto_proxima_revision_fecha": None,
                "repuesto_aviso_anticipacion_dias": None,
                "tipo_equipo": "CPAP",
                "variante": "",
                "borrador_revision_id": None,
            }
        ]
        out = preventivos_views._agenda_plan_items(scope="device")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["preventivo_source"], "plan")
        self.assertEqual(out[0]["proxima_revision_fecha"], dt.date(2026, 4, 1))


class DevicePreventivoRepuestosPlanScopeTests(SimpleTestCase):
    def test_alta_de_repuesto_no_replica_a_planes_equivalentes(self):
        plan = {"id": 91}
        payload = {
            "repuesto_key": "txt:filtro",
            "catalogo_repuesto_id": None,
            "nombre_repuesto": "Filtro",
            "periodicidad_valor": 1,
            "periodicidad_unidad": "meses",
            "aviso_anticipacion_dias": 30,
            "ultima_revision_fecha": None,
            "proxima_revision_fecha": None,
        }
        created = {**payload, "id": 101, "plan_id": 91}
        request = SimpleNamespace(data=payload, user=SimpleNamespace(id=7))

        with (
            patch("service.views.preventivos_views.require_roles"),
            patch("service.views.preventivos_views._set_audit_user"),
            patch("service.views.preventivos_views._require_existing_device", return_value=True),
            patch("service.views.preventivos_views._fetch_active_plan", return_value=plan),
            patch("service.views.preventivos_views._parse_repuesto_payload", return_value=payload),
            patch("service.views.preventivos_views._fetch_plan_repuesto_item_by_key", return_value=None),
            patch("service.views.preventivos_views._insert_plan_repuesto", return_value=101) as mock_insert,
            patch("service.views.preventivos_views._fetch_plan_repuesto_item", return_value=created),
            patch("service.views.preventivos_views._serialize_plan_repuesto", return_value=created),
            patch("service.views.preventivos_views._equivalent_plan_ids_from_plan") as mock_equivalent,
            patch("service.views.preventivos_views._upsert_template_item") as mock_template,
        ):
            view = preventivos_views.DevicePreventivoRepuestosView()
            response = preventivos_views.DevicePreventivoRepuestosView.post.__wrapped__(view, request, device_id=10)

        self.assertEqual(response.status_code, 201)
        mock_insert.assert_called_once_with(plan.get("id"), payload, 7, copy_dates=True)
        mock_equivalent.assert_not_called()
        mock_template.assert_not_called()
        self.assertEqual(response.data["replicacion"]["planes_equivalentes"], 0)
        self.assertEqual(response.data["replicacion"]["insertados"], 0)

    def test_edicion_de_repuesto_actualiza_solo_el_item_del_plan(self):
        plan = {"id": 91}
        current = {
            "id": 101,
            "plan_id": 91,
            "repuesto_key": "txt:filtro",
            "catalogo_repuesto_id": None,
            "nombre_repuesto": "Filtro",
            "periodicidad_valor": 1,
            "periodicidad_unidad": "meses",
            "aviso_anticipacion_dias": 30,
            "ultima_revision_fecha": None,
            "proxima_revision_fecha": None,
        }
        updated = {**current, "periodicidad_valor": 2}
        request = SimpleNamespace(data={"periodicidad_valor": 2}, user=SimpleNamespace(id=7))

        with (
            patch("service.views.preventivos_views.require_roles"),
            patch("service.views.preventivos_views._set_audit_user"),
            patch("service.views.preventivos_views._require_existing_device", return_value=True),
            patch("service.views.preventivos_views._fetch_active_plan", return_value=plan),
            patch("service.views.preventivos_views._fetch_plan_repuesto_item", side_effect=[current, updated]),
            patch("service.views.preventivos_views.exec_void") as mock_exec_void,
            patch("service.views.preventivos_views._serialize_plan_repuesto", return_value=updated),
            patch("service.views.preventivos_views._equivalent_plan_ids_from_plan") as mock_equivalent,
            patch("service.views.preventivos_views._fetch_plan_repuesto_item_by_key") as mock_fetch_by_key,
            patch("service.views.preventivos_views._upsert_template_item") as mock_template,
        ):
            view = preventivos_views.DevicePreventivoRepuestoDetailView()
            response = preventivos_views.DevicePreventivoRepuestoDetailView.patch.__wrapped__(
                view,
                request,
                device_id=10,
                item_id=101,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_exec_void.call_count, 1)
        sql, params = mock_exec_void.call_args.args
        self.assertIn("WHERE id=%s", sql)
        self.assertEqual(params[-1], 101)
        mock_equivalent.assert_not_called()
        mock_fetch_by_key.assert_not_called()
        mock_template.assert_not_called()
        self.assertEqual(response.data["replicacion"]["planes_equivalentes"], 0)
        self.assertEqual(response.data["replicacion"]["actualizados"], 0)

    def test_borrado_de_repuesto_elimina_solo_el_item_del_plan(self):
        plan = {"id": 91}
        current = {
            "id": 101,
            "plan_id": 91,
            "repuesto_key": "txt:filtro",
            "catalogo_repuesto_id": None,
            "nombre_repuesto": "Filtro",
        }
        request = SimpleNamespace(data={}, user=SimpleNamespace(id=7))

        with (
            patch("service.views.preventivos_views.require_roles"),
            patch("service.views.preventivos_views._set_audit_user"),
            patch("service.views.preventivos_views._require_existing_device", return_value=True),
            patch("service.views.preventivos_views._fetch_active_plan", return_value=plan),
            patch("service.views.preventivos_views._fetch_plan_repuesto_item", return_value=current),
            patch("service.views.preventivos_views.exec_void") as mock_exec_void,
            patch("service.views.preventivos_views._equivalent_plan_ids_from_plan") as mock_equivalent,
            patch("service.views.preventivos_views._delete_template_item") as mock_template_delete,
        ):
            view = preventivos_views.DevicePreventivoRepuestoDetailView()
            response = preventivos_views.DevicePreventivoRepuestoDetailView.delete.__wrapped__(
                view,
                request,
                device_id=10,
                item_id=101,
            )

        self.assertEqual(response.status_code, 200)
        mock_exec_void.assert_called_once()
        sql, params = mock_exec_void.call_args.args
        self.assertIn("WHERE id=%s", sql)
        self.assertEqual(params, [101])
        mock_equivalent.assert_not_called()
        mock_template_delete.assert_not_called()
        self.assertEqual(response.data["deleted"], 1)
        self.assertEqual(response.data["planes_equivalentes"], 0)


class DevicePreventivoRevisionChecklistTests(SimpleTestCase):
    @patch("service.views.preventivos_views._serialize_plan")
    @patch("service.views.preventivos_views._fetch_revision_items")
    @patch("service.views.preventivos_views._fetch_revision")
    @patch("service.views.preventivos_views.exec_void")
    @patch("service.views.preventivos_views.exec_returning")
    @patch("service.views.preventivos_views._fetch_plan_repuestos_by_ids")
    @patch("service.views.preventivos_views._fetch_active_plan")
    @patch("service.views.preventivos_views._set_audit_user")
    @patch("service.views.preventivos_views.require_roles")
    def test_revision_por_checklist_no_reinicia_recuento_por_defecto(
        self,
        mock_require_roles,
        mock_set_audit_user,
        mock_fetch_plan,
        mock_fetch_repuestos_ids,
        mock_exec_returning,
        mock_exec_void,
        mock_fetch_revision,
        mock_fetch_revision_items,
        mock_serialize_plan,
    ):
        mock_fetch_plan.return_value = {
            "id": 91,
            "periodicidad_valor": 6,
            "periodicidad_unidad": "meses",
            "proxima_revision_fecha": None,
        }
        mock_fetch_repuestos_ids.return_value = [
            {"id": 101, "nombre_repuesto": "Filtro", "periodicidad_valor": 30, "periodicidad_unidad": "dias"},
            {"id": 102, "nombre_repuesto": "Cooler", "periodicidad_valor": 45, "periodicidad_unidad": "dias"},
        ]
        mock_exec_returning.return_value = 777
        mock_fetch_revision.return_value = {"id": 777}
        mock_fetch_revision_items.return_value = []
        mock_serialize_plan.return_value = {"id": 91}

        request = SimpleNamespace(
            data={"fecha_realizada": "2026-03-10", "repuesto_ids": [101, 102], "resumen": "ok"},
            user=SimpleNamespace(id=7),
        )
        view = preventivos_views.DevicePreventivoRevisionCreateView()
        response = preventivos_views.DevicePreventivoRevisionCreateView.post.__wrapped__(view, request, device_id=10)
        self.assertEqual(response.status_code, 201)

        sql_calls = [c.args[0] for c in mock_exec_void.call_args_list]
        updates = [sql for sql in sql_calls if "UPDATE preventivo_plan_repuestos" in sql]
        inserts = [sql for sql in sql_calls if "INSERT INTO preventivo_revision_items" in sql]
        self.assertEqual(len(updates), 0)
        self.assertEqual(len(inserts), 2)

    @patch("service.views.preventivos_views._serialize_plan")
    @patch("service.views.preventivos_views._fetch_revision_items")
    @patch("service.views.preventivos_views._fetch_revision")
    @patch("service.views.preventivos_views.exec_void")
    @patch("service.views.preventivos_views.exec_returning")
    @patch("service.views.preventivos_views._fetch_plan_repuestos_by_ids")
    @patch("service.views.preventivos_views._fetch_active_plan")
    @patch("service.views.preventivos_views._set_audit_user")
    @patch("service.views.preventivos_views.require_roles")
    def test_revision_por_checklist_reinicia_solo_repuestos_indicados(
        self,
        mock_require_roles,
        mock_set_audit_user,
        mock_fetch_plan,
        mock_fetch_repuestos_ids,
        mock_exec_returning,
        mock_exec_void,
        mock_fetch_revision,
        mock_fetch_revision_items,
        mock_serialize_plan,
    ):
        mock_fetch_plan.return_value = {
            "id": 91,
            "periodicidad_valor": 6,
            "periodicidad_unidad": "meses",
            "proxima_revision_fecha": None,
        }
        mock_fetch_repuestos_ids.return_value = [
            {"id": 101, "nombre_repuesto": "Filtro", "periodicidad_valor": 30, "periodicidad_unidad": "dias"},
            {"id": 102, "nombre_repuesto": "Cooler", "periodicidad_valor": 45, "periodicidad_unidad": "dias"},
        ]
        mock_exec_returning.return_value = 777
        mock_fetch_revision.return_value = {"id": 777}
        mock_fetch_revision_items.return_value = []
        mock_serialize_plan.return_value = {"id": 91}

        request = SimpleNamespace(
            data={
                "fecha_realizada": "2026-03-10",
                "repuesto_ids": [101, 102],
                "reset_recuento_repuesto_ids": [101],
                "resumen": "ok",
            },
            user=SimpleNamespace(id=7),
        )
        view = preventivos_views.DevicePreventivoRevisionCreateView()
        response = preventivos_views.DevicePreventivoRevisionCreateView.post.__wrapped__(view, request, device_id=10)
        self.assertEqual(response.status_code, 201)

        sql_calls = [c.args[0] for c in mock_exec_void.call_args_list]
        updates = [sql for sql in sql_calls if "UPDATE preventivo_plan_repuestos" in sql]
        plan_updates = [sql for sql in sql_calls if "UPDATE preventivo_planes" in sql]
        inserts = [sql for sql in sql_calls if "INSERT INTO preventivo_revision_items" in sql]
        self.assertEqual(len(updates), 1)
        self.assertEqual(mock_exec_void.call_args_list[1].args[1][-1], 101)
        self.assertEqual(len(plan_updates), 0)
        self.assertEqual(len(inserts), 2)

    @patch("service.views.preventivos_views._serialize_plan")
    @patch("service.views.preventivos_views._fetch_revision_items")
    @patch("service.views.preventivos_views._fetch_revision")
    @patch("service.views.preventivos_views.exec_void")
    @patch("service.views.preventivos_views.exec_returning")
    @patch("service.views.preventivos_views._normalize_item_payload")
    @patch("service.views.preventivos_views._fetch_active_plan")
    @patch("service.views.preventivos_views._set_audit_user")
    @patch("service.views.preventivos_views.require_roles")
    def test_revision_legacy_se_mantiene_sin_repuesto_ids(
        self,
        mock_require_roles,
        mock_set_audit_user,
        mock_fetch_plan,
        mock_normalize,
        mock_exec_returning,
        mock_exec_void,
        mock_fetch_revision,
        mock_fetch_revision_items,
        mock_serialize_plan,
    ):
        mock_fetch_plan.return_value = {
            "id": 93,
            "periodicidad_valor": 1,
            "periodicidad_unidad": "meses",
            "proxima_revision_fecha": None,
        }
        mock_normalize.return_value = {
            "device_id": 10,
            "equipo_snapshot": "Equipo 10",
            "serie_snapshot": "NS",
            "interno_snapshot": "MG",
            "estado_item": "ok",
            "motivo_no_control": None,
            "ubicacion_detalle": None,
            "accesorios_cambiados": False,
            "accesorios_detalle": None,
            "notas": None,
            "arrastrar_proxima": True,
        }
        mock_exec_returning.return_value = 778
        mock_fetch_revision.return_value = {"id": 778}
        mock_fetch_revision_items.return_value = []
        mock_serialize_plan.return_value = {"id": 93}

        request = SimpleNamespace(
            data={"fecha_realizada": "2026-03-10", "estado_item": "ok"},
            user=SimpleNamespace(id=7),
        )
        view = preventivos_views.DevicePreventivoRevisionCreateView()
        response = preventivos_views.DevicePreventivoRevisionCreateView.post.__wrapped__(view, request, device_id=10)
        self.assertEqual(response.status_code, 201)
        mock_normalize.assert_called_once()
