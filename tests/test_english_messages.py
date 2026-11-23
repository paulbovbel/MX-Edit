"""Offline regressions for behavior that depends on translated message/status text."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "editor"))
from bridge import BridgeError, NoReply
from live import LiveError, import_rig_file, restore_all, transport_error
from test_bridge_transport import connect


class EnglishMessageTests(unittest.TestCase):
    def connection(self):
        br, firmware = connect()
        self.addCleanup(firmware.db.close)
        return br, firmware

    def test_retry_incomplete_english_reply(self):
        br, _ = self.connection()
        request = Mock(side_effect=[BridgeError("Incomplete reply (1 of 2 parts)"), "ok"])
        self.assertEqual(br._retry(request), "ok")
        self.assertEqual(request.call_count, 2)

    def test_missing_reply_pings_before_retry(self):
        br, _ = self.connection()
        br.request = Mock(return_value=(0x41, "MX5Bridge-0.7"))
        request = Mock(side_effect=[BridgeError("No reply from device"), "ok"])
        self.assertEqual(br._retry(request), "ok")
        br.request.assert_called_once_with(0x01, timeout=br.RETRY_PING)

    def test_missing_ping_raises_no_reply(self):
        br, _ = self.connection()
        br.request = Mock(side_effect=BridgeError("No reply from device"))
        request = Mock(side_effect=BridgeError("No reply from device"))
        with self.assertRaisesRegex(NoReply, "including ping"):
            br._retry(request)
        self.assertEqual(request.call_count, 1)

    def test_device_errors_are_not_retried(self):
        br, _ = self.connection()
        request = Mock(side_effect=BridgeError("SQL: unknown table"))
        with self.assertRaisesRegex(BridgeError, "unknown table"):
            br._retry(request)
        self.assertEqual(request.call_count, 1)

    def test_batch_split_accepts_english_and_legacy_firmware(self):
        for message in ("Reply too large (50000 characters)", "Antwort zu gross (50000 Zeichen)"):
            with self.subTest(message=message):
                br, _ = self.connection()
                br.request = Mock(side_effect=[
                    BridgeError(message), (0x45, '{"a": 1}'), (0x45, '{"b": 2}')
                ])
                self.assertEqual(br._get_group(["a", "b"]), {"a": 1, "b": 2})
                self.assertEqual(br.request.call_count, 3)

    def test_transport_classification(self):
        for message in ("No reply from device", "Incomplete reply (1 of 2 parts)",
                        "MIDI port closed", "MIDI send failed (100 bytes): ALSA"):
            with self.subTest(message=message):
                self.assertTrue(transport_error(BridgeError(message)))
        self.assertFalse(transport_error(BridgeError("SQL: constraint failed")))
        self.assertFalse(transport_error(LiveError("No reply is not a transport error here")))

    def test_restore_report_uses_english_status_keys(self):
        br, _ = self.connection()
        source = Path(__file__).resolve().parents[1] / "editor" / "werkzeuge" / "regtest_rig.rig"
        with tempfile.TemporaryDirectory(prefix="mx-edit-english-") as folder:
            rigs = Path(folder) / "Rigs"
            rigs.mkdir()
            target = rigs / source.name
            shutil.copyfile(source, target)
            _, status = import_rig_file(br, str(target), mode="restore")
            self.assertEqual(status, "new")
            result = restore_all(br, folder)
            self.assertEqual(result["rigs_existing"], 1)
            self.assertEqual(result["rigs_replaced"], 0)
            self.assertEqual(result["errors"], [])
            result = restore_all(br, folder, replace=True)
            self.assertEqual(result["rigs_existing"], 0)
            self.assertEqual(result["rigs_replaced"], 1)
            self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
