import os
import tempfile
from pathlib import Path
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase

from ..models import City, Hotel

CSV_DIR = Path(__file__).resolve().parents[4] / "Case Maykin Media - Django - CSV data"


def fake_csv_download(url, **kwargs):
    """Serve the CSV files of the case from disk instead of the network."""
    filename = url.rsplit("/", 1)[-1]
    return mock.Mock(content=(CSV_DIR / filename).read_bytes())


class DataFrameTest(TestCase):
    @mock.patch(
        "maykinbooking.base.management.commands.dataframe.requests.get",
        side_effect=fake_csv_download,
    )
    def test_dataframe_management_command(self, mock_get):
        call_command("dataframe")
        self.assertEqual(City.objects.count(), 6)
        self.assertEqual(Hotel.objects.count(), 196)
        downloaded = [
            call.args[0].rsplit("/", 1)[-1] for call in mock_get.call_args_list
        ]
        self.assertEqual(downloaded, ["city.csv", "hotel.csv"])

    def test_fileimport_management_command(self):
        call_command("fileimport")
        self.assertEqual(City.objects.count(), 6)
        self.assertEqual(Hotel.objects.count(), 196)


class EmailCSVTest(TestCase):
    def setUp(self):
        # The command writes data.csv to the working directory; keep it out of the repo.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp_dir.name)

    def test_emailcsv_management_command(self):
        call_command("email_csv")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].attachments[0][0], "data.csv")
