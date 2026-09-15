from service.tests.schema_helpers import create_fixture_device
import io
import tempfile
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from service.models import User
from service.auth import issue_token


class IngresoMediaAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_tmp = tempfile.TemporaryDirectory()
        cls._override = override_settings(
            DEFAULT_FILE_STORAGE='django.core.files.storage.FileSystemStorage',
            MEDIA_ROOT=cls._media_tmp.name,
            INGRESO_MEDIA_MAX_FILES=5,
            INGRESO_MEDIA_MAX_SIZE_MB=5,
            INGRESO_MEDIA_THUMB_MAX=128,
        )
        cls._override.enable()
        cls.addClassCleanup(cls._override.disable)
        cls.addClassCleanup(cls._media_tmp.cleanup)

        vendor = connection.vendor
        auto_inc = 'BIGSERIAL PRIMARY KEY' if vendor == 'postgresql' else ('INT AUTO_INCREMENT PRIMARY KEY' if vendor == 'mysql' else 'INTEGER PRIMARY KEY AUTOINCREMENT')
        bool_type = 'BOOLEAN' if vendor != 'sqlite' else 'INTEGER'
        bigint = 'BIGINT' if vendor != 'sqlite' else 'INTEGER'
        datetime_type = 'TIMESTAMPTZ' if vendor == 'postgresql' else 'DATETIME'
        engine_suffix = ' ENGINE=InnoDB' if vendor == 'mysql' else ''

        users_sql = f"""
            CREATE TABLE IF NOT EXISTS users (
                id {auto_inc},
                nombre TEXT,
                email VARCHAR(320) UNIQUE,
                hash_pw TEXT,
                rol TEXT,
                activo {bool_type} DEFAULT {'TRUE' if vendor == 'postgresql' else '1'}
            ){engine_suffix}
        """

        ingresos_sql = f"""
            CREATE TABLE IF NOT EXISTS ingresos (
                id {auto_inc},
                estado TEXT,
                motivo TEXT,
                fecha_ingreso {datetime_type},
                fecha_creacion {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                presupuesto_estado TEXT,
                asignado_a INT
            ){engine_suffix}
        """

        if vendor == 'sqlite':
            ingreso_media_sql = """
                CREATE TABLE IF NOT EXISTS ingreso_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ingreso_id INT NOT NULL,
                    usuario_id INT NOT NULL,
                    storage_path TEXT NOT NULL,
                    thumbnail_path TEXT NOT NULL,
                    original_name TEXT,
                    mime_type VARCHAR(80) NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    width INT NOT NULL,
                    height INT NOT NULL,
                    comentario TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
        else:
            ingreso_media_sql = f"""
                CREATE TABLE IF NOT EXISTS ingreso_media (
                    id {auto_inc},
                    ingreso_id INT NOT NULL,
                    usuario_id INT NOT NULL,
                    storage_path TEXT NOT NULL,
                    thumbnail_path TEXT NOT NULL,
                    original_name TEXT,
                    mime_type VARCHAR(80) NOT NULL,
                    size_bytes {bigint} NOT NULL,
                    width INT NOT NULL,
                    height INT NOT NULL,
                    comentario TEXT,
                    created_at {datetime_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at {datetime_type} NOT NULL DEFAULT CURRENT_TIMESTAMP {'ON UPDATE CURRENT_TIMESTAMP' if vendor == 'mysql' else ''}
                ){engine_suffix}
            """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(ingresos_sql)
            cur.execute(ingreso_media_sql)
            if vendor == 'sqlite':
                cur.execute('CREATE INDEX IF NOT EXISTS ix_ingreso_media_ingreso ON ingreso_media(ingreso_id, created_at DESC)')
                cur.execute('CREATE INDEX IF NOT EXISTS ix_ingreso_media_usuario ON ingreso_media(usuario_id)')
            else:
                cur.execute('CREATE INDEX IF NOT EXISTS idx_ingreso_media_ingreso_created ON ingreso_media(ingreso_id, created_at DESC)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_ingreso_media_usuario ON ingreso_media(usuario_id)')

    @classmethod
    def setUpTestData(cls):
        cls._seed_data()

    @classmethod
    def _seed_data(cls):
        User.objects.all().delete()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_media")
            cur.execute("DELETE FROM ingresos")

        cls.tech_user = User.objects.create(
            nombre='Tech', email='tech@example.com', hash_pw='', rol='tecnico', activo=True
        )
        cls.other_tech = User.objects.create(
            nombre='Otro', email='other@example.com', hash_pw='', rol='tecnico', activo=True
        )
        cls.admin_user = User.objects.create(
            nombre='Admin', email='admin@example.com', hash_pw='', rol='admin', activo=True
        )

        device_id = create_fixture_device()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingresos (estado, motivo, fecha_ingreso, fecha_creacion, presupuesto_estado, asignado_a, device_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                ['ingresado', 'reparación', timezone.now(), timezone.now(), 'pendiente', cls.tech_user.id, device_id],
            )
            if connection.vendor == 'sqlite':
                cur.execute('SELECT last_insert_rowid()')
            elif connection.vendor == 'postgresql':
                cur.execute("SELECT currval(pg_get_serial_sequence('ingresos', 'id'))")
            else:
                cur.execute('SELECT LAST_INSERT_ID()')
            cls.ingreso_id = cur.fetchone()[0]

        cls.tech_token = issue_token(cls.tech_user)
        cls.other_tech_token = issue_token(cls.other_tech)
        cls.admin_token = issue_token(cls.admin_user)

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_media")
        self.client = APIClient()

    # Helpers
    def _url(self, ingreso_id=None, suffix=''):
        ingreso_id = ingreso_id or self.ingreso_id
        return f"/api/ingresos/{ingreso_id}/fotos/{suffix}"

    def _make_image(self, color=(255, 0, 0), size=(64, 64)):
        buf = io.BytesIO()
        Image.new('RGB', size, color=color).save(buf, format='JPEG')
        return buf.getvalue()

    def _upload(self, token, files):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        payload = [('files', SimpleUploadedFile(name, content, content_type='image/jpeg')) for name, content in files]
        data = {}
        for idx, (field, file) in enumerate(payload):
            data[f"files_{idx}"] = file
        return self.client.post(self._url(), data, format='multipart')

    def test_assigned_technician_can_upload_and_list_photos(self):
        img1 = self._make_image(color=(255, 0, 0))
        img2 = self._make_image(color=(0, 255, 0))
        response = self._upload(self.tech_token, [("foto1.jpg", img1), ("foto2.jpg", img2)])
        self.assertEqual(response.status_code, 201)
        uploaded = response.data.get('uploaded')
        self.assertIsInstance(uploaded, list)
        self.assertEqual(len(uploaded), 2)

        list_resp = self.client.get(self._url(), HTTP_AUTHORIZATION=f"Bearer {self.tech_token}")
        self.assertEqual(list_resp.status_code, 200)
        results = list_resp.data.get('results')
        self.assertEqual(len(results), 2)
        self.assertTrue(all('thumbnail_url' in item for item in results))

    def test_unassigned_technician_cannot_upload(self):
        img = self._make_image()
        response = self._upload(self.other_tech_token, [("foto.jpg", img)])
        self.assertEqual(response.status_code, 403)

    def test_large_file_rejected_with_413(self):
        img = self._make_image(size=(400, 400)) + b'0' * (1024 * 1024)
        self.assertGreater(len(img), 1024 * 1024)
        override = override_settings(INGRESO_MEDIA_MAX_SIZE_MB=1)
        override.enable()
        try:
            response = self._upload(self.tech_token, [("big.jpg", img)])
        finally:
            override.disable()
        self.assertEqual(response.status_code, 413)
        self.assertIn('detail', response.data)

    def test_admin_can_update_comment_and_delete(self):
        img = self._make_image()
        upload_resp = self._upload(self.tech_token, [("nota.jpg", img)])
        self.assertEqual(upload_resp.status_code, 201)
        media_id = upload_resp.data['uploaded'][0]['id']

        patch_resp = self.client.patch(
            self._url(suffix=f"{media_id}/"),
            {'comentario': 'nota admin'},
            format='json',
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}"
        )
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual(patch_resp.data['comentario'], 'nota admin')

        delete_resp = self.client.delete(
            self._url(suffix=f"{media_id}/"),
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}"
        )
        self.assertEqual(delete_resp.status_code, 200)
        self.assertTrue(delete_resp.data.get('ok'))


