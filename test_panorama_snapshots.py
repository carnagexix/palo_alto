"""Offline unit tests for Mode 4: no Panorama connection required."""

import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import panorama_cli as p


class XmlResponse:
    def __init__(self, text):
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        pass


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.client = p.PanoramaClient("panorama.example.invalid",
                                       "deliberately-not-a-real-key")

    def _reply(self, xml):
        response = XmlResponse(xml)
        self.client.session.get = Mock(return_value=response)
        return response

    def test_save_uses_documented_xml_op(self):
        self.client._request = Mock(
            return_value="Config saved to nightly_20261116.xml.")
        method = self.client.create_snapshot("nightly_20261116.xml")
        self.assertIn("XML operational API", method)
        self.client._request.assert_called_once_with({
            "type": "op",
            "cmd": "<save><config><to>nightly_20261116.xml</to></config></save>",
        })

    def test_save_rejects_unsafe_filename(self):
        self.client._request = Mock()
        for name in ("", "../backup.xml", "backup", "bad name.xml",
                     "backup/evil.xml", ".hidden.xml", "x" * 128 + ".xml"):
            with self.subTest(filename=name):
                with self.assertRaises(ValueError):
                    self.client.create_snapshot(name)
        self.client._request.assert_not_called()

    def test_list_parses_root_level_completions(self):
        self._reply(
            '<response status="success" code="19"><completions>'
            '<completion value="nightly_20261116.xml" help-string="saved"/>'
            '<completion value="prechange_20261115.xml"/>'
            '<completion value="nightly_20261116.xml"/>'
            '<completion value="&lt;value&gt;" help-string="placeholder"/>'
            '</completions></response>'
        )
        entries = self.client.list_snapshots()
        self.assertEqual([x["name"] for x in entries],
                         ["nightly_20261116.xml", "prechange_20261115.xml"])
        self.assertEqual(entries[0]["description"], "saved")
        params = self.client.session.get.call_args.kwargs["params"]
        self.assertEqual(params["type"], "op")
        self.assertEqual(params["action"], "complete")
        self.assertEqual(params["xpath"], "/operations/show/config/saved")
        self.assertEqual(params["key"], self.client.api_key)

    def test_list_parses_nested_completions(self):
        self._reply(
            '<response status="success"><result><completions>'
            '<completion><value>snapshot_01.xml</value></completion>'
            '</completions></result></response>'
        )
        self.assertEqual(self.client.list_snapshots(),
                         [{"name": "snapshot_01.xml", "description": ""}])

    def test_list_empty_completion_container_is_empty_list(self):
        self._reply('<response status="success"><completions/>'
                    '</response>')
        self.assertEqual(self.client.list_snapshots(), [])

    def test_list_missing_completion_container_is_error_not_empty(self):
        self._reply('<response status="success"><result/>'
                    '</response>')
        with self.assertRaisesRegex(RuntimeError, "no completion list"):
            self.client.list_snapshots()

    def test_list_propagates_api_authorization_error(self):
        self._reply('<response status="unauth" code="16">'
                    '<msg><line>Unauthorized request</line></msg></response>')
        with self.assertRaisesRegex(RuntimeError, "code 16"):
            self.client.list_snapshots()

    def test_save_menu_adds_timestamp_and_xml_suffix(self):
        self.client.create_snapshot = Mock(return_value="XML operational API")
        with patch.object(p, "choose", return_value=(
                "save configuration snapshot on Panorama")), \
                patch("builtins.input", return_value="my backups.xml"), \
                redirect_stdout(io.StringIO()):
            p.snapshot_mode(self.client)
        name = self.client.create_snapshot.call_args.args[0]
        self.assertRegex(name, r"^my_backups_\d{8}_\d{6}\.xml$")

    def test_save_error_offers_local_export(self):
        self.client.create_snapshot = Mock(
            side_effect=RuntimeError("Panorama API error (code 16)"))
        with patch.object(p, "choose", return_value=(
                "save configuration snapshot on Panorama")), \
                patch("builtins.input", side_effect=["backup", "y"]), \
                patch.object(p, "_export_config_snapshot_locally") as export, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            p.snapshot_mode(self.client)
        export.assert_called_once_with(self.client, "backup")

    def test_listing_menu_filters_builtin_entries(self):
        self.client.list_snapshots = Mock(return_value=[
            {"name": "candidate-config", "description": ""},
            {"name": "running-config.xml", "description": ""},
            {"name": "nightly.xml", "description": ""},
        ])
        output = io.StringIO()
        with patch.object(p, "choose",
                          return_value="list snapshots saved on Panorama"), \
                redirect_stdout(output):
            p.snapshot_mode(self.client)
        text = output.getvalue()
        self.assertIn("nightly.xml", text)
        self.assertNotIn("running-config.xml", text)
        self.assertNotIn("candidate-config", text)
        self.assertIn("1 named snapshot", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
