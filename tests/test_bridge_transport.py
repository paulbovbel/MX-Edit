"""Offline transport/import regression tests; no MIDI dependencies or hardware."""
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace
import unittest
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "editor"))
from bridge import Bridge, BridgeError, HDR, MAX_REQUEST, escape, unescape
from live import import_rig_file


class Firmware:
    """Model the existing EVAL relay and SQL protocol, with an ALSA-sized limit."""
    def __init__(self):
        self.closed = False
        self.pending = []
        self.messages = []
        self.staged = {}
        self.dispatched = []
        self.fail_chunk = False
        self.fail_dispatch = False
        self.db = sqlite3.connect(":memory:")
        weakref.finalize(self, self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""create table rigs (
            id text primary key, name text, content text, show_order integer,
            author text, is_readonly integer, color integer, created_at integer,
            prog_num integer)""")

    def iter_pending(self):
        while self.pending:
            yield self.pending.pop(0)

    def reply(self, seq, cmd, text, long):
        text = escape(text)
        parts = [text[i:i + 380] for i in range(0, len(text), 380)] or [""]
        for i, part in enumerate(parts):
            header = ([i >> 7, i & 127, len(parts) >> 7, len(parts) & 127]
                      if long else [i, len(parts)])
            self.pending.append(SimpleNamespace(type="sysex",
                data=HDR + [seq, cmd] + header + [ord(c) for c in part]))

    def send(self, message):
        data = list(message.data)
        self.messages.append(data)
        if len(data) + 2 > MAX_REQUEST:
            raise SystemError("MidiOutAlsa::sendMessage: error sending MIDI message to port")
        self.receive(data)

    def receive(self, data):
        seq, command = data[3:5]
        long = bool(command & 0x20)
        command &= 0x1F
        text = unescape("".join(map(chr, data[5:])))
        if command == 7:
            create = re.fullmatch(r'Object.defineProperty\(device.sqlPending,"(\w+)",'
                r'\{value:\[\],configurable:true,enumerable:false\}\);return "ready";', text)
            append = re.fullmatch(r'device.sqlPending.(\w+).push\("([0-9A-F ]+)"\);return "ok";', text)
            dispatch = re.fullmatch(r'var p=device.sqlPending.(\w+).join\(""\);'
                r'delete device.sqlPending.\1;device.sysEx\(p\);', text)
            delete = re.fullmatch(r'delete device.sqlPending.(\w+);', text)
            if create:
                self.staged[create[1]] = []
            elif append:
                if self.fail_chunk:
                    raise SystemError("simulated staging send failure")
                self.staged[append[1]].append(append[2])
            elif dispatch:
                if self.fail_dispatch:
                    raise SystemError("simulated dispatch send failure")
                frame = bytes.fromhex("".join(self.staged.pop(dispatch[1])))
                assert frame[0] == 0xF0 and frame[-1] == 0xF7
                inner = list(frame[1:-1])
                self.dispatched.append(inner)
                before = len(self.pending)
                self.receive(inner)
                # SQL completes asynchronously on the real firmware: the
                # relay ACK can arrive before the original request's reply.
                original_reply = self.pending[before:]
                del self.pending[before:]
                self.reply(seq, 0x47, "", long)
                self.pending.extend(original_reply)
                return
            elif delete:
                self.staged.pop(delete[1], None)
            elif text.startswith('return '):
                self.reply(seq, 0x47, text[7:].rstrip(";"), long)
                return
            else:
                raise AssertionError("Unexpected relay JS: " + text)
            self.reply(seq, 0x47, "", long)
        elif command in (8, 9):
            try:
                if command == 8:
                    rows = [dict(row) for row in self.db.execute(text)]
                else:
                    self.db.executescript("begin;\n" + text + ";\ncommit;")
                    rows = [{"changes": self.db.execute("select changes()").fetchone()[0]}]
                result = {"ok": True, "rows": rows}
            except sqlite3.Error as e:
                if self.db.in_transaction:
                    self.db.rollback()
                result = {"ok": False, "err": str(e)}
            self.reply(seq, command | 0x40, json.dumps(result), long)
        else:
            self.reply(seq, command | 0x40, text, long)


def connect():
    firmware = Firmware()
    br = Bridge.__new__(Bridge)
    br.inp = br.out = firmware
    br.mido = SimpleNamespace(Message=lambda type, data: SimpleNamespace(type=type, data=data))
    br.seq = 0
    br.v2 = br.v3 = br.v4 = br.unicode = br.long = True
    br.vnum = (0, 7)
    return br, firmware


class TransportTests(unittest.TestCase):
    def test_small_request_unchanged(self):
        br, fw = connect()
        self.assertEqual(br.request(2, "/Engine/Patch/Rig"), (0x42, "/Engine/Patch/Rig"))
        self.assertEqual(len(fw.messages), 1)
        self.assertEqual(fw.messages[0][:5], HDR + [1, 0x22])

    def test_frame_boundary(self):
        for length, staged in ((MAX_REQUEST - 7, False), (MAX_REQUEST - 6, True)):
            with self.subTest(length=length):
                br, fw = connect()
                payload = "A" * length
                self.assertEqual(br.request(2, payload)[1], payload)
                self.assertEqual(bool(fw.dispatched), staged)

    def test_unicode_and_sequence_wrap(self):
        br, fw = connect()
        br.seq = 125
        payload = "quotes '\"; \\ tabs\t\n café 🎸 " * 500
        self.assertEqual(br.request(2, payload)[1], payload)
        self.assertEqual(fw.dispatched[0][3], 126)
        self.assertEqual(len(fw.dispatched), 1)
        self.assertFalse(fw.staged)
        self.assertTrue(all(len(m) + 2 <= MAX_REQUEST for m in fw.messages))
        self.assertEqual(br.request(2, "next")[1], "next")

    def test_old_firmware_actionable_error(self):
        br, fw = connect()
        br.vnum = (0, 5)
        with self.assertRaisesRegex(BridgeError, "USB Transfer"):
            br.request(9, "A" * MAX_REQUEST)
        self.assertFalse(fw.messages)

    def test_failure_cleanup_without_dispatch_or_retry(self):
        for failure in ("fail_chunk", "fail_dispatch"):
            with self.subTest(failure=failure):
                br, fw = connect()
                setattr(fw, failure, True)
                with self.assertRaisesRegex(BridgeError, "MIDI send failed"):
                    br.request(9, "A" * MAX_REQUEST)
                self.assertFalse(fw.staged)
                self.assertFalse(fw.dispatched)
                if failure == "fail_dispatch":
                    self.assertEqual(sum(m[3] == 0 for m in fw.messages), 1)

    def test_small_send_error_wrapped(self):
        br, fw = connect()
        fw.send = lambda message: (_ for _ in ()).throw(SystemError("ALSA error"))
        with self.assertRaisesRegex(BridgeError, "MIDI send failed"):
            br.request(2, "test")

    def test_import_actual_pack(self):
        br, fw = connect()
        folder = Path(__file__).resolve().parents[1] / "presets"
        paths = [folder / (name + ".rig") for name in
                 ("NEON GLASS", "COPPER EDGE", "ORBITAL BLOOM", "VELVET RIOT", "OBSIDIAN TIGHT")]
        for path in paths:
            rid, status = import_rig_file(br, str(path))
            self.assertEqual(status, "new")
            content = fw.db.execute("select content from rigs where id=?", (rid,)).fetchone()[0]
            self.assertEqual(content, json.loads(path.read_text())["content"])
        self.assertEqual(fw.db.execute("select count(*) from rigs").fetchone()[0], 5)
        self.assertEqual(len(fw.dispatched), 5)
        self.assertTrue(all(len(m) + 2 <= MAX_REQUEST for m in fw.messages))

    def test_large_write_rollback(self):
        br, fw = connect()
        with self.assertRaisesRegex(BridgeError, "SQL:"):
            br.sql_write("insert into rigs(id, content) values('test', '%s');"
                         "insert into missing_table values(1)" % ("x" * 10000))
        self.assertEqual(fw.db.execute("select count(*) from rigs").fetchone()[0], 0)
        self.assertEqual(len(fw.dispatched), 1)


if __name__ == "__main__":
    unittest.main()
