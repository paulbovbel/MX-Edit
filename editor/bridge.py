"""bridge.py: MX5 Bridge client (SysEx over USB-MIDI).

Supports bridge 0.1 (single-part replies), 0.2 (multipart replies:
<cmd> <part> <total> <text>), 0.3 (read-only rig database SQL),
0.4 (SQL write transactions and database actions: status, backup, restore,
restart), 0.5 (non-ASCII characters escaped as 0x7F plus four hex digits
instead of '?'; encoding/decoding is transparent to callers), and 0.6
(long reply headers with 14-bit part counters, requested by command bit
0x20: about 6 MB replies instead of 48 KB; SHELL command 0x0B;
the SQL read channel is strictly read-only).

Device files (read_file, write_file, make_dir, list_dir, shell): bridge
0.6+ uses SHELL with base64 data; 0.3-0.5 use the SQL read channel's
sqlite3 readfile()/writefile()/fsdir() and .system (one shell line as root).
"""
import base64, json, re, time, uuid

HDR = [0x7D, 0x48, 0x52]
_ESCAPED = re.compile("\x7f([0-9A-Fa-f]{4})")
KEYWORDS = ["mx5", "headrush", "gadget", "f_midi", "midi function"]
MAX_REPLY = 40000   # Estimated reply size per batch (bridge limit: 127 x 380 characters)
PER_VALUE = 220     # Reply characters per value in addition to its path (measured ~130-160)
# Bound complete outgoing messages, including F0/F7. ALSA's sequencer output
# pool can reject a whole rig (SQL hex literals make it roughly twice as big).
MAX_REQUEST = 4096
STAGE_CHUNK = 3000


class BridgeError(Exception):
    pass


class NoReply(BridgeError):
    """No reply, including to a subsequent short ping: the device is disconnected."""


class Bridge:
    def __init__(self, in_name, out_name):
        import mido
        self.mido = mido
        self.in_name, self.out_name = in_name, out_name
        self.inp = mido.open_input(in_name)
        self.out = mido.open_output(out_name)
        self.seq = 0
        self.v2 = None      # None = unknown, determined by ping()
        self.v3 = False     # Database service (SQL) available
        self.v4 = False     # Database writes (SQL transactions and actions) available
        self.unicode = False  # Bridge 0.5: non-ASCII characters survive transport unchanged
        self.long = False     # Bridge 0.6: long reply header (14-bit part counter), SHELL command
        self.version = ""
        self.vnum = (0, 0)  # Version from ping, e.g. (0, 5)
        self._drain()

    @staticmethod
    def find_ports():
        import mido
        def pick(names):
            hits = [n for n in names if any(k in n.lower() for k in KEYWORDS)]
            if len(hits) != 1:
                raise BridgeError("Could not identify a unique MX5 MIDI port: %s" % names)
            return hits[0]
        try:
            ins, outs = mido.get_input_names(), mido.get_output_names()
        except Exception as e:   # E.g. Linux without ALSA sequencer (snd-seq), or missing backend
            raise BridgeError("MIDI unavailable: %s" % e)
        return pick(ins), pick(outs)

    @classmethod
    def auto(cls):
        return cls(*cls.find_ports())

    def close(self):
        for p in (self.inp, self.out):
            try:
                p.close()
            except Exception:
                pass

    def _drain(self):
        for _ in self.inp.iter_pending():
            pass

    # ---------- Transport ----------
    def _is_chunk(self, d):
        if self.v2 is False:
            return False
        return len(d) >= 7 and d[5] < d[6] <= 127 and (self.v2 or (d[5] == 0 and d[6] >= 1))

    def request(self, cmd, text="", timeout=3.0):
        if self.inp.closed or self.out.closed:   # send() on a closed rtmidi port crashes
            raise BridgeError("MIDI port closed")
        self.seq = self.seq % 127 + 1
        seq = self.seq
        long_hdr = self.long and cmd != 0x01   # Ping always uses a short reply (version not yet known)
        data = HDR + [seq, cmd | (0x20 if long_hdr else 0)] + \
            [ord(c) & 0x7F for c in (escape(text) if self.unicode else text)]
        if len(data) + 2 > MAX_REQUEST:
            self._send_staged(data)
        else:
            self._send_message(data)
        parts, total = {}, None
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.inp.iter_pending():
                if m.type != "sysex":
                    continue
                d = list(m.data)
                if d[:3] != HDR or len(d) < 5 or d[3] != seq:
                    continue
                code = d[4]
                if long_hdr and len(d) >= 9:   # <part hi> <part lo> <total hi> <total lo>
                    total = d[7] << 7 | d[8]
                    parts[d[5] << 7 | d[6]] = "".join(chr(b) for b in d[9:])
                    deadline = time.time() + timeout
                    if len(parts) < total:
                        continue
                    body = "".join(parts[i] for i in range(total))
                elif not long_hdr and self._is_chunk(d):
                    total = d[6]
                    parts[d[5]] = "".join(chr(b) for b in d[7:])
                    deadline = time.time() + timeout
                    if len(parts) < total:
                        continue
                    body = "".join(parts[i] for i in range(total))
                else:
                    body = "".join(chr(b) for b in d[5:])
                if self.unicode:
                    body = unescape(body)
                if code == 0x7F:
                    raise BridgeError(body)
                return code, body
            time.sleep(0.002)
        if parts:
            raise BridgeError("Incomplete reply (%d of %d parts)" % (len(parts), total))
        raise BridgeError("No reply from device")

    def _send_message(self, data):
        try:
            self.out.send(self.mido.Message("sysex", data=data))
        except (SystemError, RuntimeError) as e:
            raise BridgeError("MIDI send failed (%d bytes): %s" % (len(data) + 2, e)) from e

    def _send_staged(self, data):
        """Relay one original request through bounded, acknowledged EVALs.

        Bridge >= 0.6 exposes its JS sqlPending object and sysEx dispatcher.
        A non-enumerable temporary property keeps staging out of pollSql's
        for-in loop. Only the final EVAL dispatches the original request:
        no partial SQL, no new transaction, no firmware change.
        """
        if self.vnum < (0, 6):
            raise BridgeError("Large MIDI requests require bridge firmware 0.6 or newer. "
                              "Use USB Transfer to import this rig.")
        key = "_mxedit_" + uuid.uuid4().hex
        ref = "device.sqlPending." + key
        # Stage the exact MIDI frame as ASCII hex, so Unicode escapes and
        # protocol fields survive unchanged and chunks cannot inject JS.
        frame = " ".join("%02X" % b for b in [0xF0] + data + [0xF7])
        try:
            self.eval('Object.defineProperty(device.sqlPending,"%s",'
                      '{value:[],configurable:true,enumerable:false});return "ready";' % key)
            for off in range(0, len(frame), STAGE_CHUNK):
                self.eval('%s.push("%s");return "ok";' % (ref, frame[off:off + STAGE_CHUNK]))
            js = 'var p=%s.join("");delete %s;device.sysEx(p);' % (ref, ref)
            # Seq 0 is reserved for the relay's own acknowledgement. The
            # caller waits for the original seq, including asynchronous SQL.
            self._send_message(HDR + [0, 0x07 | (0x20 if self.long else 0)] +
                               [ord(c) for c in js])
        except Exception:
            # Never retry dispatch: a write might already have taken effect.
            # Best-effort cleanup must not hide the original failure.
            try:
                self.request(0x07, "delete %s;" % ref, timeout=1.0)
            except Exception:
                pass
            raise

    RETRY_PING = 1.0   # Seconds: ping before retrying a missing reply

    def _retry(self, fn, *a):
        """Retry once if a reply is missing or incomplete. If entirely missing,
        ping briefly first; if that also fails, raise NoReply immediately.
        Detects disconnection in ~4 s instead of two full timeouts."""
        try:
            return fn(*a)
        except BridgeError as e:
            if "Incomplete reply" not in str(e) and "No reply" not in str(e):
                raise
            self._drain()
            if "No reply" in str(e):
                try:
                    self.request(0x01, timeout=self.RETRY_PING)
                except BridgeError:
                    raise NoReply("No reply from device (including ping)")
                self._drain()
            return fn(*a)

    # ---------- Commands ----------
    def ping(self):
        self.v2 = None
        code, body = self.request(0x01)   # Reply 'MX5Bridge-0.5 f_midi' is plain ASCII
        self.version = body
        m = re.match(r"MX5Bridge-(\d+)\.(\d+)", body)
        self.vnum = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        self.v2 = self.vnum >= (0, 2)
        self.v3 = self.vnum >= (0, 3)
        self.v4 = self.vnum >= (0, 4)
        self.unicode = self.vnum >= (0, 5)
        self.long = self.vnum >= (0, 6)
        return body

    def get(self, path):
        code, body = self._retry(self.request, 0x02, path)
        return json.loads(body.split("\t", 1)[1])

    def set(self, path, field, value):
        if isinstance(value, bool):
            value = int(value)
        code, body = self.request(0x03, "%s\t%s\t%s" % (path, field, value))
        return json.loads(body.split("\t", 1)[1])

    def entries(self, path):
        self._need_v2("Choice lists")
        code, body = self._retry(self.request, 0x04, path)
        return json.loads(body.split("\t", 1)[1])

    def get_many(self, paths):
        """Read multiple values (individually on bridge 0.1). Split by estimated
        reply size; halve a batch if its reply still exceeds the limit."""
        if not self.v2:
            return {p: self.get(p) for p in paths}
        result, group, size = {}, [], 0
        for p in list(paths) + [None]:
            if p is None or (group and size + len(p) + PER_VALUE > MAX_REPLY):
                if group:
                    result.update(self._get_group(group))
                group, size = [], 0
            if p is not None:
                group.append(p)
                size += len(p) + PER_VALUE
        return result

    def _get_group(self, group):
        try:
            code, body = self._retry(self.request, 0x05, "\n".join(group))
        except BridgeError as e:
            # Accept both English replies and older, German-language firmware.
            if not any(s in str(e) for s in ("too large", "zu gross")) or len(group) < 2:
                raise
            half = len(group) // 2
            return dict(self._get_group(group[:half]), **self._get_group(group[half:]))
        return json.loads(body)

    def eval(self, js):
        self._need_v2("EVAL")
        return self.request(0x07, js)[1]

    def sql(self, query, timeout=20.0):
        """Read-only rig database query (bridge 0.3). Return rows as a list of
        dictionaries; database failures raise BridgeError."""
        if not self.v3:
            raise BridgeError("SQL queries require bridge firmware 0.3")
        return self._db(0x08, query, timeout)

    def sql_write(self, statements, timeout=30.0):
        """Execute SQL writes (bridge 0.4) as one transaction with foreign keys enabled.
        The first error rolls back all writes and raises BridgeError. Before
        the first write after boot, the service backs up the device database.
        Return the number of changed rows."""
        if not self.v4:
            raise BridgeError("Database writes require bridge firmware 0.4")
        rows = self._db(0x09, statements, timeout)
        return rows[0].get("changes", 0) if rows else 0

    def action(self, name, timeout=60.0):
        """Database action (bridge 0.4): status, backup, restore, restart.
        restore/restart stop the device app; it restarts after ~15 s.
        The bridge is unavailable meanwhile; reconnect afterwards."""
        if not self.v4:
            raise BridgeError("Actions require bridge firmware 0.4")
        rows = self._db(0x0A, name, timeout)
        return rows[0] if rows else {}

    def _db(self, cmd, text, timeout):
        code, body = self.request(cmd, text, timeout=timeout)
        r = json.loads(body)
        if not r.get("ok"):
            raise BridgeError("SQL: %s" % (r.get("err") or "unknown error"))
        return r.get("rows") or []

    # ---------- Device files (bridge 0.3, SQL read channel) ----------
    def _need_v3(self, what):
        if not self.v3:
            raise BridgeError("%s require bridge firmware 0.3" % what)

    # ---------- Files and shell on bridge 0.6+ (SHELL, base64 data) ----------
    SQLITE = "/usr/Evil/Scripts/mx5bridge-sqlite3"
    FILE_PAGE = 192 * 1024      # Bytes per read request (base64 reply ~256 KB, ~1.5 s)
    FILE_CHUNK = 48 * 1024      # Bytes per write request (base64 request ~64 KB)

    def _shell6(self, script, timeout=70.0):
        """Run a shell script (including multiline) via SHELL: (exit code, output including stderr)."""
        rows = self._db(0x0B, script, timeout)
        r = rows[0] if rows else {}
        return int(r.get("rc", -1)), r.get("out") or ""

    def _check6(self, rc, out, what):
        if rc != 0:
            raise BridgeError("%s (exit code %d): %s" % (what, rc, out.strip()[:300]))
        return out

    def _read_file6(self, path):
        q = sh_quote(path)
        size = int(self._check6(*self._shell6("wc -c < %s" % q), what="Cannot read file: %s" % path).strip())
        out = bytearray()
        for k in range((size + self.FILE_PAGE - 1) // self.FILE_PAGE):
            rc, b64 = self._shell6("dd if=%s bs=%d skip=%d count=1 2>/dev/null | base64 -w 0"
                                   % (q, self.FILE_PAGE, k))
            out += base64.b64decode(self._check6(rc, b64, "Read failed: %s" % path).strip())
        if len(out) != size:
            raise BridgeError("Incomplete file read (%d instead of %d bytes): %s" % (len(out), size, path))
        return bytes(out)

    def _write_file6(self, path, data, progress=None):
        q = sh_quote(path)
        for off in range(0, max(len(data), 1), self.FILE_CHUNK):
            part = base64.b64encode(data[off:off + self.FILE_CHUNK]).decode("ascii")
            self._check6(*self._shell6("printf '%%s' '%s' | base64 -d %s %s" % (part, ">" if off == 0 else ">>", q)),
                         what="Write failed: %s" % path)
            if progress:
                progress(min(len(data), off + self.FILE_CHUNK), len(data))
        n = int(self._check6(*self._shell6("wc -c < %s" % q), what="Cannot read file: %s" % path).strip())
        if n != len(data):
            raise BridgeError("Incomplete file write (%s instead of %d bytes): %s" % (n, len(data), path))
        return n

    def _list_dir6(self, path, recursive):
        # sqlite3 fsdir runs in a separate process, not the read channel. Character escapes
        # (0.5) preserve filenames; a long-header reply fits all entries.
        cond = "" if recursive else " and name not glob %s" % sql_lit(path + "/*/*")
        sql = ("select name, (mode & 61440) = 16384 as d, mtime from fsdir(%s) where name <> %s%s order by name"
               % (sql_lit(path), sql_lit(path), cond))
        out = self._check6(*self._shell6("%s -json :memory: %s" % (self.SQLITE, sh_quote(sql))),
                           what="Cannot read folder: %s" % path)
        rows = json.loads(out) if out.strip() else []
        return [(r["name"][len(path) + 1:], bool(r.get("d")), r.get("mtime")) for r in rows]

    def read_file(self, path, page=20000):
        """Read device file bytes, paginated as hex because replies are limited to
        about 48 KB (bridge 0.6+ uses SHELL and base64)."""
        if self.long:
            return self._read_file6(path)
        self._need_v3("File operations")
        rows = self.sql("select length(readfile(%s)) as n" % sql_lit(path))
        size = rows[0].get("n") if rows else None
        if size is None:
            raise BridgeError("Cannot read file: %s" % path)
        out = bytearray()
        for off in range(0, size, page):
            rows = self.sql("select hex(substr(readfile(%s), %d, %d)) as h" % (sql_lit(path), off + 1, page))
            out += bytes.fromhex(rows[0]["h"])
        return bytes(out)

    def write_file(self, path, data, chunk=30000, progress=None):
        """Write/overwrite a device file in appended chunks. Call progress(written,
        total) after each chunk. Return the number of bytes written."""
        if self.long:
            return self._write_file6(path, data, progress)
        self._need_v3("File operations")
        lit = sql_lit(path)
        first = True
        for off in range(0, max(len(data), 1), chunk):
            part = data[off:off + chunk].hex()
            if first:
                q = "select writefile(%s, unhex('%s')) as n" % (lit, part)
            else:
                q = "select writefile(%s, readfile(%s) || unhex('%s')) as n" % (lit, lit, part)
            rows = self.sql(q, timeout=30.0)
            if not rows or rows[0].get("n") is None:
                raise BridgeError("Write failed: %s" % path)
            first = False
            if progress:
                progress(min(len(data), off + chunk), len(data))
        rows = self.sql("select length(readfile(%s)) as n" % lit)
        n = rows[0].get("n") if rows else None
        if n != len(data):
            raise BridgeError("Incomplete file write (%s instead of %d bytes): %s" % (n, len(data), path))
        return n

    def make_dir(self, path):
        """Create a directory; an existing directory is not an error."""
        if self.long:
            self._check6(*self._shell6("mkdir -p %s" % sh_quote(path)), what="Cannot create folder: %s" % path)
            return
        self._need_v3("File operations")
        self.sql("select writefile(%s, null, 16877) as n" % sql_lit(path))   # 040755

    def list_dir(self, path, recursive=False):
        """List [(name, is_directory, mtime)], with names relative to path (including
        subpaths when recursive). Non-ASCII names use hex because SysEx is
        limited to 7 bits through bridge 0.4."""
        if self.long:
            return self._list_dir6(path, recursive)
        self._need_v3("File operations")
        lit = sql_lit(path)
        cond = "" if recursive else " and name not glob %s" % sql_lit(path + "/*/*")
        out, page, off = [], 300, 0
        while True:
            rows = self.sql("select case when name glob '*[^ -~]*' then hex(name) else name end as n, "
                            "name glob '*[^ -~]*' as h, (mode & 61440) = 16384 as d, mtime from fsdir(%s) "
                            "where name <> %s%s order by name limit %d offset %d"
                            % (lit, lit, cond, page, off))
            for r in rows:
                name = bytes.fromhex(r["n"]).decode("utf-8", "replace") if r.get("h") else r["n"]
                out.append((name[len(path) + 1:], bool(r.get("d")), r.get("mtime")))
            if len(rows) < page:
                return out
            off += page

    def shell(self, cmd, timeout=30.0):
        """Run a shell command on the device as root; return (exit code, output).
        No newlines in cmd before bridge 0.6 (SHELL). Use sh_quote() for
        non-ASCII paths."""
        if self.long:
            return self._shell6(cmd, timeout=max(timeout, 70.0))
        self._need_v3("Shell commands")
        if "\n" in cmd or "\r" in cmd:
            raise BridgeError("Shell command must be a single line")
        out, rc = "/tmp/mx5bridge/sh.out", "/tmp/mx5bridge/sh.rc"
        rows = self.sql(".system { %s ; } > %s 2>&1; echo $? > %s\n"
                        "select cast(readfile('%s') as text) as out, cast(readfile('%s') as text) as rc"
                        % (cmd, out, rc, out, rc), timeout=timeout)
        r = rows[0] if rows else {}
        try:
            code = int((r.get("rc") or "").strip() or -1)
        except ValueError:
            code = -1
        return code, r.get("out") or ""

    def _need_v2(self, what):
        if self.v2 is False:
            raise BridgeError("%s require bridge firmware 0.2" % what)


def escape(text):
    """Wire encoding (bridge 0.5): escape everything except TAB, LF and 0x20..0x7E
    as 0x7F plus four hex digits per UTF-16 unit (non-BMP characters use surrogate pairs)."""
    if all(c in "\t\n" or " " <= c <= "~" for c in text):
        return text
    out = []
    for c in text:
        if c in "\t\n" or " " <= c <= "~":
            out.append(c)
        else:
            units = c.encode("utf-16-be")
            out += ["\x7f%04X" % int.from_bytes(units[i:i + 2], "big") for i in range(0, len(units), 2)]
    return "".join(out)


def unescape(text):
    """Inverse of escape: decode escapes and combine surrogate pairs."""
    if "\x7f" not in text:
        return text
    s = _ESCAPED.sub(lambda m: chr(int(m.group(1), 16)), text)
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")


def sql_lit(s):
    """SQL string literal. Encode non-ASCII text as hex (cast(x'..' as text))
    for 7-bit SysEx through bridge 0.4; bridge 0.5+ also accepts plain text."""
    s = str(s)
    if all(" " <= c <= "~" for c in s):
        return "'" + s.replace("'", "''") + "'"
    return "cast(x'%s' as text)" % s.encode("utf-8").hex()


def sh_quote(s):
    """Bridge.shell() argument: single-quoted, with non-ASCII encoded via printf octal escapes."""
    if all(" " <= c <= "~" for c in s):
        return "'" + s.replace("'", "'\\''") + "'"
    return '"$(printf \'%s\')"' % "".join(chr(c) if 32 <= c <= 126 and chr(c) not in "'\\%" else "\\%03o" % c
                                          for c in s.encode("utf-8"))
