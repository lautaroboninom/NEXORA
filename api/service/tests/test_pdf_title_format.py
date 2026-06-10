from django.test import SimpleTestCase

from service.pdf import os_version_title


class PdfTitleFormatTest(SimpleTestCase):
    def test_os_version_title_uses_hyphen(self):
        self.assertEqual(os_version_title(29207, 1), "OS 29207-1")

    def test_os_version_title_pads_os_number(self):
        self.assertEqual(os_version_title(12, 3), "OS 00012-3")
