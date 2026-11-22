"""live.py: live MX5 connection through MX5 Bridge.

All requests run in a background thread. Writes to the same parameter are
coalesced (only the latest value is sent) to avoid flooding the device when
moving controls. Results reach the GUI thread via dispatch (e.g. tk.after).
Requires bridge firmware 0.5 (database reads/writes and Unicode).

Device behavior verified on hardware:
  Rig list: db_rigs returns device order: All Rigs by rowid, setlists by
    setlist_rigs.show_order (2026-09-30, 330 rigs). IDs identify rigs; names
    are not unique.
  Loading: navigate NextBank/PrevBank until RigName1-3 matches the list, then
    press LoadRig1-3. ReceivePresetIndex = program number - 1 is faster but
    works only in the active list. Always verify LoadedID.
  Setlists: SetName1 displays a setlist; NextSetBank/PrevSetBank navigate;
    AccessSet1 enters it and loads its first rig. AccessRigView opens All Rigs
    without loading. All Rigs is not itself in the setlist list.
  Dialogs: with Confirm Dirty enabled, unsaved changes show BoardMode FreeText
    and ButtonText1-3 Cancel|Save|Discard, mapped to FS1-3. Setting Rigs/Dirty=0
    before switching avoids the prompt (drop_changes, verified 2026-09-29);
    LoadRig also reloads the current rig from storage.
  Saving: EditModeSaveRig opens cancel|save|save_new_rig, mapped to FS1|FS2|FS3.
  Buttons: state 1, hold at least 30 ms, state 0, wait at least 30 ms;
    otherwise presses may be lost or counted twice.
"""
import base64, collections, json, os, threading, time
from bridge import BridgeError, NoReply, sh_quote, sql_lit
from rigmodel import base_name


ENGINE = "/Engine/Patch"
PC = "/Engine/PresetCtrl"
RIGS = PC + "/Rigs"
RC = "/Engine/RedirCtrl"
SAVE = RC + "/EditModeSaveRig"
FS = RC + "/RawFootswitches/FS%d"
BOARD = RC + "/BoardMode"
BTN = RC + "/ButtonText%d"
ALL_RIGS = "All Rigs"
SLOTS = 11
EMPTY = "Empty Slot"
EMPTY_RIG = "[EMPTY]"
SPECIAL_MODULES = ["Rig", "Input", "Output"]
HIDDEN_PARAMS = {"PresetName", "PresetName2"}
MAX_BANKS = 200
MAX_SETLISTS = 50
HOLD = 0.05           # Button hold (reliable from 30 ms in tests)
BANK_WAIT = 0.05      # Pause after release (under 30 ms the next press counts twice)
LOAD_WAIT = 3.0
SETTLE = 0.5          # After loading, let chain/parameters settle. At 0.3 s,
                      # MX5 dropped the first subsequent set in ~20% of cases.
                      # Measured 2026-09-30: adding 0.1 s was enough, 0 failures in 30.
RELOAD_WAIT = 0.4     # LoadRig on current rig without Dirty: fresh state after ~0.2 s in tests


# ---- Device profile ----
#
# MX5/Pedalboard differences (Pedalboard diagnosis logs from
# 2026-10-11/13; editor not tested directly on that hardware, beta):
#   Banks: MX5 RigName1-3, NextBank steps by 3. Pedalboard RigName1-8 (two rows on
#     FS2-5 and FS8-11), NextBank/PrevBank step by 4; final page showed
#     slots 25-28 plus four [EMPTY]; bank up showed 21-28 (28 rigs, rowid order like MX5).
#   Switches: MX5 FootSwitch5-7 -> RawFootswitches/FS1-3. Pedalboard FootSwitch1-8 ->
#     FS2-5 (1-4), FS8-11 (5-8) in Stomp; FS1/FS7 rig up/down, FS6 looper, FS12 tempo.
#   Dialogs: ButtonText<k> maps to FS<k> (MX5: 3, Pedalboard: 12). Find buttons
#     by label (dialog_press), not fixed numbers.
#   Setlists: Pedalboard uses songs (setlist_songs/setlist_song_rigs); setlist_rigs
#     stays empty, so the editor supports All Rigs only.
#   Looper: Pedalboard has no AltLooperFS; dedicated looper switches FS1-11.
class Device:
    def __init__(self, name, bank_show, bank_step, fs, raw, buttons, layout, setlists=True, alt_looper=True,
                 new_rig=True, beta=False):
        self.name = name
        self.layout = layout         # Device switch layout: rows (top first) of FootSwitch numbers
                                     # and labels for fixed switches (display only)
        self.buttons = buttons       # Footswitches with display text (ButtonText1..n = RawFootswitches/FS1..n)
        self.bank_show = bank_show   # Rigs per bank display (RigName1..n, LoadRig1..n)
        self.bank_step = bank_step   # Slots advanced per NextBank/PrevBank
        self.fs = fs                 # Assignable footswitch numbers (FootSwitch<n>)
        self.raw = raw               # {n: RawFootswitches number in Stomp view}
        self.setlists = setlists     # Setlists from setlist_rigs (False: All Rigs only)
        self.alt_looper = alt_looper
        self.new_rig = new_rig       # blank_rig_content matches this device's rig structure
        self.beta = beta


DEVICES = {
    "MX5": Device("MX5", 3, 3, (5, 6, 7), {5: 1, 6: 2, 7: 3}, 3, [[5, 6, 7]]),
    # Pedalboard: two rows of six, FS1-6 above FS7-12 (Stomp: FS1 rig up, FS7 rig down,
    # FS6 looper, FS12 tempo; labels from diagnosis logs)
    "Pedalboard": Device("Pedalboard", 8, 4, tuple(range(1, 9)),
                         {n: n + 1 if n <= 4 else n + 3 for n in range(1, 9)}, 12,
                         [["Rig ↑", 1, 2, 3, 4, "Looper"], ["Rig ↓", 5, 6, 7, 8, "Tempo"]],
                         setlists=False, alt_looper=False, new_rig=False, beta=True),
}
DEV = DEVICES["MX5"]
FS_NUMBERS = DEV.fs


def set_device(model):
    """Set the connected device profile (None/unknown = MX5). Process-wide:
    the editor connects to only one device at a time."""
    global DEV, FS_NUMBERS
    DEV = DEVICES.get(model) or DEVICES["MX5"]
    FS_NUMBERS = DEV.fs
    return DEV


def fs_label(n):
    """Displayed number for FootSwitch<n> (MX5: 5-7 -> 1-3)."""
    return FS_NUMBERS.index(n) + 1 if n in FS_NUMBERS else n

# Live-mode node types
L_NUMBER, L_BOOL, L_ENUM, L_TEXT = "number", "bool", "enum", "text"


class LiveError(Exception):
    pass


def kind_of(info):
    if not info or not info.get("ok"):
        return None
    if info.get("valueType") == "TwoStateValue":
        return L_BOOL
    if "numEntries" in info:
        return L_ENUM
    if "maximum" in info or "unnormalized" in info:
        return L_NUMBER
    return L_TEXT


COMPACT_FIELDS = ("value", "unnormalized", "state", "index", "string")
COMPACT_GROUP = 300   # Paths per eval (~60 reply characters per value; ~48 KB limit)
_COMPACT_JS = ('var P = %s, F = %s, o = []; '
               'function f(t, k) { try { var v = t[k], y = typeof v; '
               '  return y == "number" || y == "boolean" || y == "string" ? v : null; } catch (e) { return null; } } '
               'for (var i = 0; i < P.length; i++) { var p = App.getProperty(P[i]), t = p && p.translator; '
               '  if (!t) { o.push(0); continue; } var r = []; '
               '  for (var k = 0; k < F.length; k++) r.push(f(t, F[k])); o.push(r); } '
               'return JSON.stringify(o);')


def get_compact(br, paths):
    """Like Bridge.get_many, but return only COMPACT_FIELDS (omit numEntries,
    maximum, defaultValue, valueType, editable). About one-third the reply size
    for polling (95 -> 55 ms for 86 values). Missing paths yield {'ok': False}.
    As with infoOf, absent properties omit fields; merge values, do not replace entries."""
    paths = list(dict.fromkeys(paths))
    out = {}
    for i in range(0, len(paths), COMPACT_GROUP):
        group = paths[i:i + COMPACT_GROUP]
        for p, v in zip(group, json.loads(br.eval(_COMPACT_JS % (json.dumps(group), json.dumps(COMPACT_FIELDS))))):
            if not v:
                out[p] = {"ok": False, "err": "No property: " + p}
                continue
            d = {"ok": True}
            for k, x in zip(COMPACT_FIELDS, v):
                if x is not None:
                    d[k] = x
            out[p] = d
    return out


LOST_PING = 1.0   # Seconds: ping after a transport error; no reply means connection lost


def transport_error(e):
    """True for connection failures (missing/incomplete reply or lost MIDI port),
    not device/editor error messages (LiveError)."""
    if isinstance(e, LiveError):
        return False
    if isinstance(e, BridgeError):
        return str(e).startswith(("No reply", "Incomplete reply", "MIDI port", "MIDI send failed"))
    return True


class LiveSession:
    def __init__(self, bridge, dispatch, on_lost=None):
        self.bridge = bridge
        self.dispatch = dispatch
        self.on_lost = on_lost    # Run once in GUI thread when the device stops responding
        self._cv = threading.Condition()
        self._sets = collections.OrderedDict()   # path -> (field, value, callback)
        self._jobs = collections.deque()          # (fn(bridge) -> result, callback)
        self._stop = False
        self.lost = False         # Connection lost: worker no longer accepts jobs
        self.running = False      # A job is currently using the bridge
        self.errors = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout=5)
        try:
            self.bridge.close()
        except Exception:
            pass

    def idle(self):
        with self._cv:
            return not self._sets and not self._jobs and not self.running

    def run(self, fn, callback=None):
        """Run fn(bridge) in the background; invoke callback(result) in the GUI thread.
        Exceptions from fn become callback({'ok': False, 'err': ...})."""
        with self._cv:
            self._jobs.append((fn, callback))
            self._cv.notify()

    def get(self, path, callback):
        self.run(lambda br: br.get(path), lambda r: callback(path, r))

    def get_many(self, paths, callback):
        """callback(dict mapping paths to info)"""
        self.run(lambda br: br.get_many(paths), callback)

    def set(self, path, field, value, callback=None):
        with self._cv:
            self._sets.pop(path, None)
            self._sets[path] = (field, value, callback)
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while not self._stop and not self._sets and not self._jobs:
                    self._cv.wait()
                if self._stop or self.lost:
                    return
                if self._sets:  # Writes take priority
                    path, (field, value, cb) = self._sets.popitem(last=False)
                    fn = lambda br, p=path, f=field, v=value: br.set(p, f, v)
                    cb = (lambda r, cb=cb, p=path: cb(p, r)) if cb else None
                else:
                    fn, cb = self._jobs.popleft()
                self.running = True
            broken = False
            try:
                result = fn(self.bridge)
            except LiveError as e:
                result = {"ok": False, "err": str(e)}
            except Exception as e:  # Never let the worker die
                self.errors += 1
                result = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}
                broken = transport_error(e) and (isinstance(e, NoReply) or not self._alive())
            finally:
                self.running = False
            if cb:
                self.dispatch(lambda cb=cb, r=result: cb(r))
            if broken:
                self.lost = True
                if self.on_lost:
                    self.dispatch(self.on_lost)
                return

    def _alive(self):
        """Brief ping after a transport error; one lost reply does not mean disconnection."""
        try:
            self.bridge._drain()
            self.bridge.request(0x01, timeout=LOST_PING)
            return True
        except Exception:
            return False


# ---------------- Device operations (run in worker, receive bridge) ----------------

def press(br, path, hold=HOLD, wait=BANK_WAIT):
    """Press a button: state 1, short hold, state 0, short pause."""
    br.set(path, "state", 1)
    time.sleep(hold)
    br.set(path, "state", 0)
    time.sleep(wait)


def strings(br, paths):
    """Read multiple text values; retry an incomplete reply once."""
    r = br.get_many(paths)
    if any(p not in r for p in paths):
        r = br.get_many(paths)
    return [(r.get(p) or {}).get("string", "") or "" for p in paths]


def read_bank(br):
    """Current bank's rigs (MX5: three; Pedalboard: eight): [(name, color)]."""
    k = DEV.bank_show
    v = strings(br, ["%s/RigName%d" % (PC, n) for n in range(1, k + 1)] +
                ["%s/RigColour%d" % (PC, n) for n in range(1, k + 1)])
    return [(v[i], v[i + k][:-2] if v[i + k].endswith("_D") else v[i + k]) for i in range(k)]


def _step(br, path, read, before):
    """Press and reread. If unchanged, pause and press once more (MX5 drops
    presses immediately after a rig change). Return (changed, display)."""
    press(br, path)
    cur = read(br)
    if cur != before:
        return True, cur
    time.sleep(0.2)
    cur = read(br)
    if cur != before:
        return True, cur
    press(br, path)
    cur = read(br)
    return cur != before, cur


def _to_start(br, read, prev_path, limit):
    """Press prev_path until the display stops changing; return the first entry."""
    cur = read(br)
    for _ in range(limit):
        changed, cur = _step(br, prev_path, read, cur)
        if not changed:
            break
    return cur


def read_set_cursor(br):
    return strings(br, ["%s/SetName1" % PC])[0]


def rig_status(br):
    r = br.get_many(["%s/LoadedName" % RIGS, "%s/LoadedID" % RIGS, "%s/Dirty" % RIGS, "%s/LoadedProgNum" % RIGS])
    pn = r["%s/LoadedProgNum" % RIGS].get("string", "-")
    return {"name": r["%s/LoadedName" % RIGS].get("string"), "id": r["%s/LoadedID" % RIGS].get("string"),
            "dirty": bool(r["%s/Dirty" % RIGS].get("state")), "prog": int(pn) if pn.isdigit() else None}


def dialog_buttons(br):
    """Dialog footswitch labels (ButtonText1..n; MX5: 3, Pedalboard: 12), or None
    if no dialog is open. Button k maps to RawFootswitches/FS<k>."""
    v = strings(br, [BOARD] + [BTN % n for n in range(1, DEV.buttons + 1)])
    return v[1:] if v[0] == "FreeText" else None


def dialog_key(buttons, *labels):
    """One-based index of the first button matching a label case-insensitively,
    otherwise None."""
    low = [(b or "").strip().lower() for b in buttons or []]
    for label in labels:
        if label in low:
            return low.index(label) + 1
    return None


def dialog_press(br, buttons, *labels):
    """Press a dialog button matching one of labels; raise LiveError if absent."""
    k = dialog_key(buttons, *labels)
    if not k:
        raise LiveError("A dialog is open on the %s (%s). Please answer it on the display."
                        % (DEV.name, " | ".join(b for b in buttons if b)))
    press(br, FS % k)
    return k


def _save_key(buttons):
    """Save button of cancel|save|save_new_rig dialog, or None."""
    for i, b in enumerate(buttons or []):
        t = (b or "").strip().lower()
        if t.startswith("save") and "new" not in t:
            return i + 1
    return None


def discard_dialog(br):
    """Discard the Cancel|Save|Discard unsaved-changes dialog.
    Return True if such a dialog was open."""
    b = dialog_buttons(br)
    if not b:
        return False
    dialog_press(br, b, "discard")
    return True


def drop_changes(br, st=None):
    """Mark the current rig clean (Rigs/Dirty=0) so the next switch discards without
    Cancel|Save|Discard, rather than pressing FS3. Return whether it was dirty
    (used by keep_changes)."""
    if st is None:
        st = rig_status(br)
    if not st["dirty"]:
        return False
    br.set("%s/Dirty" % RIGS, "state", 0)
    return True


def keep_changes(br):
    """Inverse of drop_changes when switching did not occur: edits are still in
    memory, so mark the rig dirty again."""
    br.set("%s/Dirty" % RIGS, "state", 1)


def wait_for(br, cond, timeout=LOAD_WAIT, discard=False):
    """Poll rig status until cond(status). With discard=True, dismiss Confirm Dirty
    with Discard as a fallback; normally drop_changes prevents the dialog."""
    end = time.time() + timeout
    while True:
        st = rig_status(br)
        if cond(st):
            time.sleep(SETTLE)
            return st
        if discard and discard_dialog(br):
            discard = False
            end = time.time() + timeout
        if time.time() >= end:
            return st
        time.sleep(0.1)


def _seek(br, items, target, read, next_path, prev_path, limit, what):
    """Navigate to target one step at a time. items suggests direction (database
    order); reread after each press and reverse at list ends, allowing device
    order to differ."""
    cur = read(br)
    if cur == target:
        return
    forward = target not in items or cur not in items or items.index(target) > items.index(cur)
    for path in ((next_path, prev_path) if forward else (prev_path, next_path)):
        for _ in range(limit):
            changed, cur = _step(br, path, read, cur)
            if cur == target:
                return
            if not changed:
                break
    raise LiveError("'%s' not found in the %s, please reread." % (target, what))


def seek_setlist(br, setlists, name):
    """Navigate SetName1 to setlist name."""
    _seek(br, setlists, name, read_set_cursor, "%s/NextSetBank" % PC, "%s/PrevSetBank" % PC,
          MAX_SETLISTS, "setlist list")


def _bank_names(br):
    return [n for n, _ in read_bank(br)]


def bank_names(rows, bank):
    """Displayed names for zero-based bank in rows: omit factory number prefixes;
    unused slots in the last bank are '[EMPTY]'."""
    start = bank * DEV.bank_step
    names = [display_name(r["name"]) for r in rows[start:start + DEV.bank_show]]
    return names + [EMPTY_RIG] * (DEV.bank_show - len(names))


def bank_count(rows):
    """Number of bank positions (NextBank advances DEV.bank_step slots)."""
    return max(1, (len(rows) + DEV.bank_step - 1) // DEV.bank_step)


def goto_bank(br, rows, target):
    """Navigate to target bank in rows and verify exact displayed names. The bank
    identifies device position because database/device orders match. If ambiguous,
    first navigate to the start. A remaining mismatch means the device list
    differs; raise LiveError so it can be reread."""
    want = bank_names(rows, target)
    banks = [bank_names(rows, b) for b in range(bank_count(rows))]
    for _ in range(3):   # MX5 drops button presses immediately after changing rigs
        cur = _bank_names(br)
        if cur == want:
            return
        hits = [b for b, names in enumerate(banks) if names == cur]
        if len(hits) == 1:
            pos = hits[0]
        else:
            if _to_start(br, _bank_names, "%s/PrevBank" % PC, MAX_BANKS) != banks[0]:
                break
            pos = 0
        path = "%s/%s" % (PC, "NextBank" if target > pos else "PrevBank")
        for _ in range(abs(target - pos)):
            press(br, path)
        time.sleep(0.1)
    cur = _bank_names(br)
    if cur != want:
        raise LiveError("The rig list on the %s differs from the editor's (bank %d shows %s) - "
                        "please reread the rig list." % (DEV.name, target + 1, " | ".join(cur)))


def _prog(row):
    """Displayed program number for a rig row (DB is zero-based; -1 means none), or None."""
    p = row.get("prog_num")
    return int(p) + 1 if p is not None and int(p) >= 0 else None


JUMP = PC + "/NextPreset"   # Counter: writing advances (new - old) positions in the active list
JUMP_MAX = 16000000         # Range up to 16777216; beyond that, calculate backwards
LAST_LOAD = {}              # Last load_rig: {'via': 'jump'|'prog'|'bank', 'secs': duration} for tests/diagnostics


def jump(br, rows, index, before):
    """Load rows[index] via NextPreset: jump by the change from its previous value
    (negative goes backwards, wrapping at list ends; a full list length reloads
    the current rig). One write, ~0.06 s, no bank navigation (verified 2026-09-30,
    including 100 slots in All Rigs). Requires an unambiguous device cursor:
    after loading, the loaded position; after entering a list, position 0 even
    if the loaded rig is elsewhere (verified 2026-09-30). Store br.cursor as
    (LoadedID, position); read_rig_list initializes it. A device-side rig change
    moves it to that rig if unique. Never guess: loading the wrong rig would
    be audible. Return False so load_rig can use another method."""
    ids = [r["id"] for r in rows]
    n = len(rows)
    cur = getattr(br, "cursor", None)
    if not cur:
        return False
    if cur[0] == before["id"] and 0 <= cur[1] < n:
        pos = cur[1]
    elif before["id"] and ids.count(before["id"]) == 1:
        pos = ids.index(before["id"])
    else:
        return False   # Nothing loaded, or this rig appears multiple times in the setlist
    delta = (index - pos) % n or n
    old = int(br.get(JUMP).get("unnormalized") or 0)
    new = old + delta if old + delta <= JUMP_MAX else old - (n - delta if delta < n else n)
    br.set(JUMP, "unnormalized", new)
    return True


def load_rig(br, rows, index, discard=False, fast=True):
    """Load rows[index] in the active list (database rows in device order).
    Prefer jump, then program number, finally bank navigation. Verify LoadedID.
    discard=True discards unsaved edits without a device prompt (drop_changes).
    Reload if already current. fast=False forces bank navigation to test the
    fallback. Return the resulting rig status."""
    target = rows[index]
    rid = target["id"]
    before = rig_status(br)
    same = before["id"] == rid
    loaded = lambda s: s["id"] == rid and (not same or not s["dirty"])
    dropped = discard and drop_changes(br, before)   # Prevent a device Discard dialog
    prog = _prog(target)
    t0 = time.time()
    via = lambda how: LAST_LOAD.update(via=how, secs=time.time() - t0)
    try:
        if fast and jump(br, rows, index, before):
            if same:   # Reloading the same rig cannot be detected from status
                time.sleep(RELOAD_WAIT)
            st = wait_for(br, loaded, timeout=1.0, discard=discard and before["dirty"])
            if loaded(st):
                via("jump")
                br.cursor = (rid, index)
                return st
            br.cursor = None   # Wrong rig loaded (device cursor/list differs): continue via bank navigation
        if prog and not same and fast:   # ReceivePresetIndex targeting the current rig does nothing
            br.set("%s/ReceivePresetIndex" % RIGS, "unnormalized", prog - 1)
            st = wait_for(br, loaded, timeout=1.0, discard=discard and before["dirty"])
            if loaded(st):
                via("prog")
                br.cursor = (rid, index) if [r["id"] for r in rows].count(rid) == 1 else None
                return st
        goto_bank(br, rows, index // DEV.bank_step)
        press(br, "%s/LoadRig%d" % (PC, index % DEV.bank_step + 1))
        if same:   # Reloading the same rig is visible in status only after a delay
            time.sleep(RELOAD_WAIT)
        st = wait_for(br, loaded, discard=discard and before["dirty"])
        via("bank")
        br.cursor = (rid, index) if loaded(st) else None
    except Exception:
        if dropped and not same and rig_status(br)["id"] == before["id"]:
            keep_changes(br)
        raise
    if not loaded(st):
        if dropped and not same and st["id"] == before["id"]:
            keep_changes(br)
        if before["dirty"]:
            raise LiveError("The %s did not switch. Is it asking on the display about unsaved "
                            "changes? Then confirm there." % DEV.name)
        raise LiveError("Rig '%s' was not loaded (loaded is '%s')." % (display_name(target["name"]), st["name"]))
    return st


def reload_rig(br, rows):
    """Reload the current rig, discarding changes, at its position in rows by ID:
    jump a full list length, otherwise use its bank's LoadRig. Clear Dirty first
    to avoid the Discard prompt (verified 2026-09-29/30)."""
    ids = [r["id"] for r in rows]
    st = rig_status(br)
    if st["id"] not in ids:
        raise LiveError("The loaded rig is not in the current list - please reread the rig list.")
    return load_rig(br, rows, ids.index(st["id"]), discard=True)


# ---- Setlists ----

def enter_setlist(br, setlists, name, setlist_ids, discard=False, active=False):
    """Activate setlist name (or ALL_RIGS); return read_rig_list-style data.
    Entering a different setlist loads its first rig (discard=True suppresses
    the prompt). All Rigs loads nothing and preserves edits. Entering rereads
    names/colors/programs from the DB, including reentering the active list:
    that loads nothing and preserves rig/edits (verified 2026-09-17).
    active=True tells us not to wait for a load; used to refresh after DB writes."""
    if name != ALL_RIGS and name not in setlist_ids:
        raise LiveError("Setlist '%s' not found." % name)
    rows = db_rigs(br, None if name == ALL_RIGS else setlist_ids[name])   # Determine which rig entering the list will load
    before = rig_status(br)
    mode = br.get(BOARD)   # View (AccessSet1/AccessRigView change it)
    if name == ALL_RIGS:
        press(br, "%s/AccessRigView" % RC)
        time.sleep(SETTLE)
        st = rig_status(br)
    else:
        seek_setlist(br, setlists, name)
        first = rows[0]["id"] if rows else None
        # If another rig will load, clear Dirty first to avoid a prompt
        # (otherwise Discard via FS3). Do not clear it for the already-active list.
        dropped = discard and not active and first and first != before["id"] and drop_changes(br, before)
        press(br, "%s/AccessSet1" % RC)
        time.sleep(SETTLE)
        if active:
            st = rig_status(br)
        else:
            if first and first != before["id"]:
                # Wait for the first rig to load; LoadedID updates after a delay
                st = wait_for(br, lambda s: s["id"] == first and not s["dirty"], discard=discard and before["dirty"])
            else:
                st = wait_for(br, lambda s: not s["dirty"], discard=discard and before["dirty"])
            if dropped and st["id"] == before["id"]:   # Nothing loaded: edits remain intact
                keep_changes(br)
                st = rig_status(br)
            if st["dirty"]:
                raise LiveError("The %s did not switch the setlist. Is it asking on the display about "
                                "unsaved changes? Then confirm there." % DEV.name)
            for _ in range(10):   # Name and ID update sequentially
                st2 = rig_status(br)
                if st2 == st:
                    break
                st = st2
                time.sleep(0.1)
    _wait_stable(br, read_bank)   # Bank display updates after a delay
    if DEV.setlists or (mode or {}).get("string") not in ("Set", "SetRig"):
        restore_board(br, mode)   # Pedalboard: do not return to a setlist's song view
    br.cursor = (st["id"], 0)   # Jump cursor: first slot, even if the loaded rig is elsewhere
    return {"setlists": setlists, "setlist": name, "sure": True, "status": st, "rows": rows,
            "setlist_ids": setlist_ids}


# ---- Rig database (bridge 0.3: read-only SQL command) ----
#
# Tables: rigs(id, name, content, show_order, author, is_readonly, color, created_at,
# prog_num), setlists(id, name, ...), setlist_rigs(setlist_id, rig_id, show_order),
# settings(name, value). content is the inner JSON from a .rig file.

RIG_COLOURS = ["Off", "Green", "Dark Green", "Yellow", "Orange", "Red", "Pink", "Purple", "Blue",
               "Light Blue", "White", "Grey", "Turquoise", "Black"]   # rigs.color -> display name
RIG_FIELDS = "id, name, color, prog_num, show_order"


# Reply limit: ~48 KB/127 parts through bridge 0.5; ~6 MB with long headers on 0.6+.
# 0.6+ multiplies pages by PAGE_FACTOR (~1 MB/reply, ~7 s at ~140 KB/s).
PAGE_FACTOR = 20


def _page(br, rows):
    """Page size (rows or characters); rows assumes the old 48 KB limit."""
    return rows * PAGE_FACTOR if getattr(br, "long", False) else rows


def db_setlists(br):
    """[(id, name)] for every setlist."""
    return [(r["id"], r["name"]) for r in br.sql("select id, name from setlists order by name collate nocase")]


def db_rigs(br, setlist_id=None):
    """Setlist/all rigs in device order: [{id, name, color, prog_num, show_order}].
    Setlists use setlist_rigs.show_order. All Rigs uses rowid: the app queries
    without ORDER BY. Initial factory/USB-import rigs were inserted by name;
    newer ones append (verified 2026-09-30, all 330 rigs). Factory names like
    '003 MODERN PLEXI' lose their numeric prefix in display_name. DB prog_num
    is displayed number minus one; -1 means none."""
    if setlist_id:
        q = ("select r.id, r.name, r.color, r.prog_num, s.show_order from setlist_rigs s "
             "join rigs r on r.id = s.rig_id where s.setlist_id = %s order by s.show_order" % sql_lit(setlist_id))
    else:
        q = "select %s from rigs order by rowid" % RIG_FIELDS
    # Replies are limited to ~48 KB through 0.5; paginate
    rows, page = [], _page(br, 150)
    while True:
        part = br.sql("%s limit %d offset %d" % (q, page, len(rows)))
        rows += part
        if len(part) < page:
            return rows


def display_name(name):
    """Bank display rig name: omit leading factory number ('003 ')."""
    if len(name) > 4 and name[:3].isdigit() and name[3] == " ":
        return name[4:]
    return name


def _sql_text(br, column, where, size=15000):
    """Read a long text column in chunks (~48 KB per reply, larger on 0.6+)."""
    out, pos, size = [], 1, _page(br, size)
    while True:
        rows = br.sql("select substr(%s, %d, %d) t from %s" % (column, pos, size, where))
        piece = (rows[0]["t"] or "") if rows else ""
        out.append(piece)
        if len(piece) < size:
            return "".join(out)
        pos += size


def _exact_row(br, table, cols, key, keyval):
    """Read one row with columns cols, or None."""
    rows = br.sql("select %s from %s where %s = %s" % (", ".join(cols), table, key, sql_lit(keyval)))
    return rows[0] if rows else None


SETTINGS_INLINE = 4000   # Long text (State_Last ~34000 characters) otherwise exceeds the reply limit


def db_settings(br):
    """settings table as {name: value}. Fetch short values initially; load long
    values per row in chunks via _sql_text."""
    big = "typeof(value) = 'text' and length(value) > %d" % _page(br, SETTINGS_INLINE)
    rows = br.sql("select name, %s as big, case when %s then null else value end as value from settings" % (big, big))
    return {r["name"]: _sql_text(br, "value", "settings where name = %s" % sql_lit(r["name"])) if r["big"] else r["value"]
            for r in rows}


RIG_ROW = ["id", "name", "content", "show_order", "author", "is_readonly", "color", "created_at", "prog_num"]


def db_rig(br, rig_id):
    """Complete rig row, including content, for .rig export."""
    row = _exact_row(br, "rigs", RIG_ROW, "id", rig_id)
    if row is None:
        raise LiveError("Rig not found in the database.")
    return row


def rig_file_json(row):
    """Convert a rigs-table row to the device USB export's .rig format."""
    d = {"author": row.get("author", ""), "color": row.get("color", 0), "content": row["content"],
         "created_at": row.get("created_at", 0), "id": row["id"], "order": row.get("show_order", 0),
         "prog_num": row.get("prog_num", -1), "readonly": bool(row.get("is_readonly", 0))}
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)   # Verified byte-identical on hardware


# ---- Full backup (rigs, setlists, block presets in USB Transfer file formats) ----
#
# USB mode exposes Rigs/<name>.rig, Setlists/<name>.setlist and Blocks/<type>/<name>.block.
# Reconstruct all three byte-identically from rigs, setlists(+setlist_rigs), and blocks
# (verified against a device USB export). Backups use the same folder layout
# so they can also be copied back through USB Transfer mode.

_BAD_CHARS = '<>:"/\\|?*'
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def safe_filename(name):
    """Windows-safe filename: forbidden characters -> '_'; no trailing dot/space."""
    s = "".join("_" if c in _BAD_CHARS or ord(c) < 32 else c for c in str(name)).rstrip(" .")
    if not s or s.upper() in _RESERVED:
        s = "_" + s
    return s


def _unique_path(folder, name, ext, used):
    """Unused path in folder, accounting for Windows case-insensitivity."""
    base = safe_filename(name)
    cand, n = base, 1
    while (cand + ext).lower() in used:
        n += 1
        cand = "%s (%d)" % (base, n)
    used.add((cand + ext).lower())
    return os.path.join(folder, cand + ext)


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def db_setlist_rows(br):
    """All setlists with ordered rigs: [(setlist_row, [(rig_id, rig_name)])]."""
    out = []
    for sl in br.sql("select id from setlists order by name collate nocase"):
        sl = _exact_row(br, "setlists", ["id", "name", "author", "is_readonly", "created_at"], "id", sl["id"])
        rr = br.sql("select s.rig_id, r.name from setlist_rigs s join rigs r on r.id = s.rig_id "
                    "where s.setlist_id = %s order by s.show_order" % sql_lit(sl["id"]))
        out.append((sl, [(r["rig_id"], r["name"]) for r in rr]))
    return out


def setlist_file_json(sl, rigs):
    """Setlist row and rigs in the device USB export's .setlist format."""
    d = {"author": sl.get("author", ""), "created_at": sl.get("created_at", 0), "id": sl["id"],
         "readonly": bool(sl.get("is_readonly", 0)), "rig_names": [n for _, n in rigs],
         "rigs": [i for i, _ in rigs], "version": "1.0.0"}
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def db_blocks(br, page=25):
    """All block presets [{id, type, name, content, is_readonly}]. About 1 KB per row,
    so paginate (~48 KB reply limit, larger on 0.6+)."""
    rows, page = [], _page(br, page)
    while True:
        part = br.sql("select id, type, name, content, is_readonly from blocks "
                      "order by type, name limit %d offset %d" % (page, len(rows)))
        rows += part
        if len(part) < page:
            return rows


def block_file_json(row):
    """Convert a blocks-table row to the device USB export's .block format."""
    d = {"content": row["content"], "id": row["id"], "readonly": bool(row.get("is_readonly", 0)),
         "type": row["type"]}
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---- Block presets ----
#
# Block presets (device Block menu > Preset) reside in the blocks table:
# type is uppercase block base name ('GREEN JRC-OD', 'AMP', 'IR (1024)', 'MIX' for
# parallel paths); name is the preset name (factory presets start '+'); content is JSON
# {'data': {'<block>': {'childorder': [...], 'children': {parameter: node}}}, 'info': {...}}
# with the same nodes as .rig files (type 0: unnormalized value; 1/3: state; 4/8: string).
# Exclude On, Colour, Doubling, PresetName. Load by writing values; as with all bridge
# writes, this does not set Rigs/Dirty, so the editor marks it explicitly.
# Do NOT trigger PresetCtrl/LoadBlockPreset with SelectedBlockIndex/CurrentBlockPresetIndex:
# without the preset list on screen, it crashes the device app (restart and
# 'restore last state?' prompt). Save/rename/delete (SaveBlockPresetConfirmed, etc.)
# were untested here; this original code path only reads the database.
PRESET_SKIP = {"DoubleStates"}      # Action: state 1 copies A -> B; not a sound parameter
PRESET_MODULE = {"Chain": "Mix"}    # Parallel-path pseudo-module: /Engine/Patch/Mix mirrors Chain/Para*
                                    # and stores the preset name (Chain/PresetName does not exist)


def preset_type(module):
    """blocks.type for a module ('Green JRC-OD 2' -> 'GREEN JRC-OD')."""
    return _base_module(module).upper()


def _preset_rows(rows):
    return [{"id": r["id"], "type": r["type"], "name": r["name"], "readonly": bool(r.get("is_readonly", 0))}
            for r in rows]


def db_preset_names(br, page=150):
    """All block preset names: {TYPE: [{id, type, name, readonly}]} in device order
    (by name, factory '+...' first). ~720 rows, paginated for the ~48 KB limit."""
    rows, page = [], _page(br, page)
    while True:
        part = br.sql("select id, type, name, is_readonly from blocks "
                      "order by type, name limit %d offset %d" % (page, len(rows)))
        rows += _preset_rows(part)
        if len(part) < page:
            break
    out = {}
    for r in rows:
        out.setdefault(r["type"], []).append(r)
    return out


def db_presets_of(br, module):
    """Presets for a block type: [{id, type, name, readonly}]."""
    rows = br.sql("select id, type, name, is_readonly from blocks where type = %s "
                  "order by name" % sql_lit(preset_type(module)))
    return _preset_rows(rows)


def db_preset_content(br, preset_id):
    row = _exact_row(br, "blocks", ["id", "name", "content"], "id", preset_id)
    if row is None:
        raise LiveError("Preset not found in the database.")
    return row


def preset_name_path(module, half="A"):
    """Block preset name path (half B of a doubled block uses PresetName2)."""
    return LiveRig.path(PRESET_MODULE.get(module, module), "PresetName" + ("2" if half == "B" else ""))


def preset_writes(module, content, half="A"):
    """Writes [(path, field, value)] to load a preset into module;
    half B of a doubled block uses parameters suffixed 2."""
    data = json.loads(content)["data"] if isinstance(content, str) else content["data"]
    node = next(iter(data.values()), {})
    children = node.get("children", {})
    order = [k for k in node.get("childorder", []) if k in children]
    order += sorted(k for k in children if k not in order)
    module = PRESET_MODULE.get(module, module)
    out = []
    for p in order:
        if p in PRESET_SKIP:
            continue
        n = children[p]
        t = n.get("type")
        if t == 0:
            field, value = "unnormalized", n.get("value")
        elif t in (1, 3):
            field, value = "state", int(bool(n.get("state")))
        else:
            field, value = "string", n.get("string", "")
        if value is None:
            continue
        out.append((LiveRig.path(module, p + ("2" if half == "B" else "")), field, value))
    return out


def load_block_preset(br, module, preset, half="A"):
    """Load preset {id, name} into a block: write values and preset name.
    Return the number of parameters written."""
    row = db_preset_content(br, preset["id"])
    writes = preset_writes(module, row["content"], half)
    if not writes:
        raise LiveError("Preset '%s' contains no parameters." % preset["name"])
    done = 0
    for path, field, value in writes:
        try:
            r = br.set(path, field, value)
        except BridgeError as e:
            if "no property" in str(e):   # E.g. TremSync has no TremSync2 counterpart in half B
                continue
            raise
        if not r.get("ok"):
            raise LiveError("%s: %s" % (path.rsplit("/", 1)[1], r.get("err")))
        done += 1
    if not done:
        raise LiveError("None of the parameters of preset '%s' exists on the device." % preset["name"])
    br.set(preset_name_path(module, half), "string", preset["name"])
    return done


RIG_BATCH = 20   # Rigs per backup_all request on bridge 0.6+ (rig content ~25 KB)


def backup_all(br, folder, progress=None):
    """Back up all device rigs, setlists and block presets to folder
    (Rigs/, Setlists/, Blocks/<type>/ and backup report). progress(text)
    reports progress. Return {rigs, setlists, blocks, folder, seconds}.
    About 8 MB rig content at ~100 KB/s takes a little over a minute."""
    t0 = time.time()

    def say(txt):
        if progress:
            progress(txt)
    say("Reading rig list …")
    rigs = db_rigs(br)
    rig_dir, set_dir, blk_dir = (os.path.join(folder, d) for d in ("Rigs", "Setlists", "Blocks"))
    for d in (rig_dir, set_dir, blk_dir):
        os.makedirs(d, exist_ok=True)
    used = set()
    if getattr(br, "long", False):
        # 0.6+: RIG_BATCH rigs per request (~0.5 MB); names are exact via character escapes
        done = 0
        while True:
            part = br.sql("select %s from rigs order by rowid limit %d offset %d"
                          % (", ".join(RIG_ROW), RIG_BATCH, done), timeout=60)
            for row in part:
                done += 1
                say("Backing up rig %d of %d: %s" % (done, len(rigs), display_name(row["name"])))
                _write(_unique_path(rig_dir, row["name"], ".rig", used), rig_file_json(row))
            if len(part) < RIG_BATCH:
                break
        if done != len(rigs):
            raise LiveError("Backup: %d rigs read, but the list has %d - please try again." % (done, len(rigs)))
    else:
        for n, r in enumerate(rigs, 1):
            say("Backing up rig %d of %d: %s" % (n, len(rigs), display_name(r["name"])))
            row = db_rig(br, r["id"])   # Also returns the exact Unicode name
            _write(_unique_path(rig_dir, row["name"], ".rig", used), rig_file_json(row))
    say("Backing up setlists …")
    setlists = db_setlist_rows(br)
    used = set()
    for sl, rr in setlists:
        _write(_unique_path(set_dir, sl["name"], ".setlist", used), setlist_file_json(sl, rr))
    say("Backing up block presets …")
    blocks = db_blocks(br)
    used = {}
    for b in blocks:
        tdir = os.path.join(blk_dir, safe_filename(b["type"]))
        os.makedirs(tdir, exist_ok=True)
        _write(_unique_path(tdir, b["name"], ".block", used.setdefault(tdir, set())), block_file_json(b))
    settings = {}
    try:
        settings = db_settings(br)
        _write(os.path.join(folder, "Einstellungen.json"),
               json.dumps(settings, sort_keys=True, indent=1, ensure_ascii=False))
    except Exception:
        pass   # Ancillary metadata; the rigs themselves matter most
    secs = time.time() - t0
    info = ["MX-Edit - backup of %s" % time.strftime("%d.%m.%Y %H:%M"),
            "Bridge: %s" % getattr(br, "version", "?"),
            "Database: db_version %s, RigVersion %s" % (settings.get("db_version", "?"), settings.get("RigVersion", "?")),
            "",
            "%d rigs in Rigs\\, %d setlists in Setlists\\, %d block presets in Blocks\\<type>\\" % (
                len(rigs), len(setlists), len(blocks)),
            "Einstellungen.json: table settings (GlobalSettings, State_Last, ...)",
            "",
            "The files have the same form as the MX5's export in USB transfer mode",
            "(Rigs\\, Setlists\\, Blocks\\ on the MX5 drive). Restore: in the editor with",
            "'Restore everything' (bridge 0.4), or put the MX5 into USB transfer mode and copy the folders.",
            "Whether the MX5 takes over copied-back files on sync (especially overwritten,",
            "already existing ones) is untested - try it with a single rig first.",
            "Backup duration: %.0f s" % secs]
    _write(os.path.join(folder, "Backup.txt"), "\r\n".join(info) + "\r\n")
    return {"rigs": len(rigs), "setlists": len(setlists), "blocks": len(blocks), "folder": folder, "seconds": secs}


def read_rig_list(br):
    """Read setlists and the active rig list from the DB: {setlists, setlist, sure,
    rows, setlist_ids, status}. The device does not report its active list:
    settings.State_Last contains it but updates only on app exit (2026-09-30).
    Infer it by matching the displayed bank against all lists (sure=False means
    ambiguous/guessed). Do not navigate the set display: its order matches DB
    name order and seek_setlist can find it regardless. Reenter the detected
    active list (enter_setlist active=True) to establish the jump cursor."""
    if not DEV.setlists:   # Pedalboard setlists contain songs; All Rigs only, without loading
        return enter_setlist(br, [], ALL_RIGS, {}, active=True)
    cursor = read_set_cursor(br)
    sets = db_setlists(br)
    names = [n for _, n in sets]
    ids = {n: i for i, n in sets}
    lists = [(ALL_RIGS, db_rigs(br))] + [(n, db_rigs(br, i)) for i, n in sets]
    shown = _bank_names(br)
    exact, close = [], []
    for name, rows in lists:
        banks = [bank_names(rows, b) for b in range(bank_count(rows))]
        if shown in banks:
            exact.append(name)
        elif any(len(set(shown) & set(b) - {EMPTY_RIG}) >= 2 for b in banks):
            close.append(name)
    if len(exact) == 1:
        active, sure = exact[0], True
    elif not exact and len(close) == 1:
        active, sure = close[0], True
    else:
        active = cursor if cursor in names else ALL_RIGS
        sure = not names
    if sure:
        # Reenter the active list once (~1 s, no load, edits preserved):
        # the jump cursor is then known to be at slot 0
        return enter_setlist(br, names, active, ids, active=True)
    br.cursor = None
    return {"setlists": names, "setlist": active, "sure": sure, "rows": dict(lists)[active],
            "status": rig_status(br), "setlist_ids": ids}


def _wait_stable(br, read, gap=0.2, tries=15):
    """Wait for two equal readings separated by gap."""
    prev = read(br)
    for _ in range(tries):
        time.sleep(gap)
        cur = read(br)
        if cur == prev:
            return cur
        prev = cur
    return prev


def open_save_dialog(br, timeout=2.0):
    """Open cancel|save|save_new_rig and wait for it. If absent halfway through,
    request once more only if no dialog is open (observed twice on 2026-09-30
    immediately after an app restart, not reliably reproducible). If still
    absent, briefly check for a late dialog and Cancel it so future actions
    are not blocked by a lingering prompt."""
    br.set(SAVE, "state", 1)
    end = time.time() + timeout
    retry = time.time() + timeout / 2
    while True:
        b = dialog_buttons(br)
        if _save_key(b):
            return b
        if retry and time.time() >= retry and not b:
            retry = None
            br.set(SAVE, "state", 1)
        if time.time() >= end:
            break
        time.sleep(0.05)
    for _ in range(20):   # Check for another 2 seconds
        time.sleep(0.1)
        late = dialog_buttons(br)
        if _save_key(late):
            cancel_dialog(br, late)
            break
    raise LiveError("The save dialog did not appear on the %s in time%s - nothing was saved."
                    % (DEV.name, " (open: %s)" % " | ".join(x for x in b if x) if b else ""))


def save_rig(br):
    """Open the Save dialog and press save (MX5: FS2); return rig status."""
    press(br, FS % _save_key(open_save_dialog(br)))
    return wait_for(br, lambda s: not s["dirty"])


def cancel_dialog(br, buttons=None):
    """Cancel an open dialog (MX5: FS1); do nothing if no Cancel button exists."""
    k = dialog_key(buttons if buttons is not None else dialog_buttons(br), "cancel")
    if k:
        press(br, FS % k)


# ---- Rig database writes (bridge 0.4) ----
#
# Write rigs, setlists and program numbers directly to evil.db: one transaction
# per call, rollback on first error; the service backs up before the first
# write after boot. The device app rereads lists, colors, names and programs
# only on entering a list (AccessRigView / AccessSet1).
# Call refresh_lists after every write to reenter the active list
# (no rig load, ~1 s). The current rig retains its in-memory state;
# if its row changes, reload it using
# refresh_lists reload_row=True. Inactive lists need no refresh:
# the device rereads them the next time they are entered.
# Verified on hardware 2026-09-17: create/copy, rename, color,
# program number, setlist entry, delete (cascades to setlist_rigs),
# and Save as new rig (save_rig_as).

RIG_AUTHOR = "UserName"     # Author value used by rigs saved on the device
MAX_PROG = 128


def new_id():
    import uuid
    return str(uuid.uuid4())


def check_rig_name(name):
    """Apply device rig-name limits: uppercase ASCII, at most 32 characters."""
    name = " ".join(name.split())
    if not name:
        raise LiveError("The name must not be empty.")
    if len(name) > 32:
        raise LiveError("The name may be at most 32 characters long.")
    if any(not 0x20 <= ord(c) <= 0x7E for c in name):
        raise LiveError("ASCII characters only (no umlauts) - like names entered on the MX5.")
    return name.upper()


def db_name_taken(br, table, name, except_id=None):
    """True if table already has a row with this name, case-insensitively."""
    where = "name = %s collate nocase" % sql_lit(name)
    if except_id:
        where += " and id <> %s" % sql_lit(except_id)
    return bool(br.sql("select 1 from %s where %s limit 1" % (table, where)))


def db_prognum_owner(br, prog, except_id=None):
    """Name of the rig owning displayed program number 1..128, otherwise None."""
    where = "prog_num = %d" % (prog - 1)
    if except_id:
        where += " and id <> %s" % sql_lit(except_id)
    rows = br.sql("select name from rigs where %s limit 1" % where)
    return rows[0]["name"] if rows else None


def content_with_name(content, name):
    """Rig content JSON (DB/.rig format) with a new Rig/PresetName.
    Compact JSON with sorted keys, verified on hardware, leaves everything
    else byte-identical."""
    try:
        c = json.loads(content)
        c["data"]["Patch"]["children"]["Rig"]["children"]["PresetName"]["string"] = name
    except (ValueError, KeyError, TypeError) as e:
        raise LiveError("Rig content not readable (%s)." % e)
    return json.dumps(c, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def blank_rig_content(name):
    """Empty rig content: all slots Empty Slot, serial, no gate, unassigned Toggle
    footswitches, Classic pedals. Reconstructed from device-created PASS THROUGH.
    The device's apiRigs.makeNewRig is inaccessible through the bridge.
    Return database-format JSON text."""
    def num(v, t=0):
        return {"type": t, "value": v}

    def txt(s, t=8):
        return {"string": s, "type": t}

    def flag(b):
        return {"state": b, "type": 1}

    def node(order, children, typed=True):
        n = {"childorder": order, "children": children}
        if typed:
            n["type"] = "Node"
        return n
    info = {"version": "4.0.1"}
    scene = base64.b64encode(b"".join(b"\0\0\0\0" + EMPTY.encode().ljust(32, b"\0") for _ in range(SLOTS))).decode()
    fs_order, fs = [], {}
    for n in (5, 6, 7):
        s = str(n)
        items = [("Module" + s, txt("Unassigned")), ("Operation" + s, txt("")),
                 ("SceneNumberOfStates" + s, num(1, 10)), ("Scene" + s, {"state": scene, "type": 24}),
                 ("MacroColour" + s, txt("Green", 4))]
        items += [("Scene%sSlot%dPreset" % (s, k), txt("")) for k in range(1, SLOTS + 1)]
        items += [("State2Scene" + s, {"state": scene, "type": 24}), ("State2MacroColour" + s, txt("Green", 4))]
        items += [("State2Scene%sSlot%dPreset" % (s, k), txt("")) for k in range(1, SLOTS + 1)]
        items += [("UserFootSwitchText" + s, txt("")), ("ModeNew" + s, txt("Toggle", 4))]
        for k, v in items:
            fs_order.append(k)
            fs[k] = v
    fs_order += ["TapTempoColour", "LastScene", "LastSceneState"]
    fs.update(TapTempoColour=txt("Green", 4), LastScene=num(-1, 10), LastSceneState=num(0, 10))

    def pedal(mode):
        order, ch = [], {}
        for i in range(1, 5):
            for k, v in (("Module", txt("Unassigned")), ("Param", txt("")), ("Min", num(0)), ("Max", num(100))):
                order.append(k + str(i))
                ch[k + str(i)] = v
        if mode:
            order.append("PedalMode")
            ch["PedalMode"] = txt("Classic", 4)
        return order, ch
    para = [("Para1Level", num(-6)), ("Para2Level", num(-6)), ("Para1Pan", num(0)), ("Para2Pan", num(0)),
            ("ParaDelay", num(0))]
    chain = [("Routing", txt("S", 4)), ("Tails", flag(True))]
    chain += [("ModuleType%d" % k, txt(EMPTY, 4)) for k in range(1, SLOTS + 1)] + para
    blocks = {
        "Chain": chain,
        "Rig": [("PresetName", txt(name)), ("Tempo", num(120)), ("TempoFromMaster", flag(True)),
                ("RigCurPedalAB", flag(False))],
        "Input": [("PresetName", txt("")), ("InputGain", num(0)), ("GateThresh", num(-120)), ("GateRelease", num(11)),
                  ("FilterThreshold", num(-100)), ("RigInput", flag(False))],
        "Output": [("PresetName", txt("")), ("RigVolume", num(0)), ("RigWidth", num(100))],
        "Mix": para + [("PresetName", txt(""))],
    }
    patch = node(list(blocks), {b: node([k for k, _ in kv], dict(kv)) for b, kv in blocks.items()}, False)
    c = {"FootSwitch": {"data": {"FootSwitch": node(fs_order, fs, False)}, "info": info},
         "Pedal1": {"data": {"Pedal1": node(*pedal(True), typed=False)}, "info": info},
         "Pedal2": {"data": {"Pedal2": node(*pedal(False), typed=False)}, "info": info},
         "data": {"Patch": patch}, "info": info}
    return json.dumps(c, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def create_rig(br, name, src_id=None, content=None, color=0, prog=None, setlist_id=None):
    """Create a rig from src_id's DB content or supplied .rig content; set inner
    Rig/PresetName to name. prog is the displayed program number or None.
    setlist_id also appends it to that setlist. Return the new ID.
    Requests up to 100 KB are supported; rigs are under 30 KB."""
    name = check_rig_name(name)
    if db_name_taken(br, "rigs", name):
        raise LiveError("There is already a rig called '%s'." % name)
    if prog is not None:
        owner = db_prognum_owner(br, prog)
        if owner:
            raise LiveError("Program number %d already belongs to rig '%s'." % (prog, owner))
    if src_id:
        content = db_rig(br, src_id)["content"]
    elif content is None:
        raise LiveError("create_rig needs src_id or content.")
    content = content_with_name(content, name)
    rid = new_id()
    pn = -1 if prog is None else prog - 1
    sql = ("insert into rigs (id, name, content, show_order, author, is_readonly, color, created_at, prog_num) "
           "values (%s, %s, %s, 0, %s, 0, %d, strftime('%%s','now'), %d)"
           % (sql_lit(rid), sql_lit(name), sql_lit(content), sql_lit(RIG_AUTHOR), color, pn))
    if setlist_id:
        sql += "; " + _setlist_append_sql(setlist_id, rid)
    if br.sql_write(sql) < 1:
        raise LiveError("The rig was not created (source not found?).")
    return rid


def rename_rig(br, rig_id, name):
    name = check_rig_name(name)
    if db_name_taken(br, "rigs", name, rig_id):
        raise LiveError("There is already a rig called '%s'." % name)
    content = content_with_name(db_rig(br, rig_id)["content"], name)   # Also update the inner PresetName
    br.sql_write("update rigs set name = %s, content = %s where id = %s"
                 % (sql_lit(name), sql_lit(content), sql_lit(rig_id)))
    return name


def set_rig_color(br, rig_id, color):
    br.sql_write("update rigs set color = %d where id = %s" % (int(color), sql_lit(rig_id)))


def set_rig_prognum(br, rig_id, prog):
    """Set displayed program number 1..128, or None. Remove it from any current
    owner, matching device behavior."""
    if prog is None:
        pn = -1
    else:
        if not 1 <= int(prog) <= MAX_PROG:
            raise LiveError("Program numbers go from 1 to %d." % MAX_PROG)
        pn = int(prog) - 1
    sql = "update rigs set prog_num = %d where id = %s" % (pn, sql_lit(rig_id))
    if pn >= 0:
        sql = "update rigs set prog_num = -1 where prog_num = %d and id <> %s; " % (pn, sql_lit(rig_id)) + sql
    br.sql_write(sql)


def delete_rig(br, rig_id):
    """Delete a rig and its setlist entries. Cannot delete the loaded rig."""
    if rig_status(br)["id"] == rig_id:
        raise LiveError("The loaded rig cannot be deleted - load another one first.")
    return br.sql_write("delete from rigs where id = %s" % sql_lit(rig_id))


def save_rig_as(br, name, setlist_id=None, prog=None):
    """Create a new rig from the loaded live state, including unsaved edits
    (bridge 0.4), replacing save_new_rig's touchscreen-only name entry.
    Preserve the old row, save on device to capture live state, copy that row
    under a new name and restore the old row in one transaction. The old rig
    remains loaded and clean, with live state in memory and original state
    in the DB; caller loads the new rig via refresh_lists. setlist_id also
    appends to that setlist. Return the new ID."""
    name = check_rig_name(name)
    if db_name_taken(br, "rigs", name):
        raise LiveError("There is already a rig called '%s'." % name)
    if prog is not None:
        owner = db_prognum_owner(br, prog)
        if owner:
            raise LiveError("Program number %d already belongs to rig '%s'." % (prog, owner))
    st = rig_status(br)
    if not st["id"]:
        raise LiveError("No rig is loaded on the %s." % DEV.name)
    old = db_rig(br, st["id"])
    st = save_rig(br)
    if st["dirty"]:
        raise LiveError("The %s did not save. Is a dialog open on the display?" % DEV.name)
    content = content_with_name(db_rig(br, old["id"])["content"], name)
    rid = new_id()
    sql = ("insert into rigs (id, name, content, show_order, author, is_readonly, color, created_at, prog_num) "
           "values (%s, %s, %s, 0, %s, 0, %d, strftime('%%s','now'), %d); "
           "update rigs set content = %s where id = %s"
           % (sql_lit(rid), sql_lit(name), sql_lit(content), sql_lit(RIG_AUTHOR), int(old.get("color") or 0),
              -1 if prog is None else prog - 1, sql_lit(old["content"]), sql_lit(old["id"])))
    if setlist_id:
        sql += "; " + _setlist_append_sql(setlist_id, rid)
    try:
        br.sql_write(sql)
    except BridgeError as e:
        raise LiveError("'%s' was saved under its old name, the copy '%s' was not created: %s"
                        % (old["name"], name, e))
    return rid


def _setlist_append_sql(setlist_id, rig_id):
    return ("insert into setlist_rigs (id, setlist_id, rig_id, show_order) "
            "select %s, %s, %s, coalesce(max(show_order), -1) + 1 from setlist_rigs where setlist_id = %s"
            % (sql_lit(new_id()), sql_lit(setlist_id), sql_lit(rig_id), sql_lit(setlist_id)))


def create_setlist(br, name, rig_ids=()):
    name = check_rig_name(name)
    if db_name_taken(br, "setlists", name):
        raise LiveError("There is already a setlist called '%s'." % name)
    sid = new_id()
    sql = ("insert into setlists (id, name, author, is_readonly, created_at) values (%s, %s, %s, 0, strftime('%%s','now'))"
           % (sql_lit(sid), sql_lit(name), sql_lit(RIG_AUTHOR)))
    for n, rid in enumerate(rig_ids):
        sql += "; insert into setlist_rigs (id, setlist_id, rig_id, show_order) values (%s, %s, %s, %d)" % (
            sql_lit(new_id()), sql_lit(sid), sql_lit(rid), n)
    br.sql_write(sql)
    return sid


def rename_setlist(br, setlist_id, name):
    name = check_rig_name(name)
    if db_name_taken(br, "setlists", name, setlist_id):
        raise LiveError("There is already a setlist called '%s'." % name)
    br.sql_write("update setlists set name = %s where id = %s" % (sql_lit(name), sql_lit(setlist_id)))
    return name


def delete_setlist(br, setlist_id):
    """Delete a setlist without deleting its rigs."""
    return br.sql_write("delete from setlists where id = %s" % sql_lit(setlist_id))


def setlist_add(br, setlist_id, rig_id):
    """Append a rig to a setlist; duplicates are allowed, as on the device."""
    br.sql_write(_setlist_append_sql(setlist_id, rig_id))


def setlist_set_order(br, setlist_id, rig_ids):
    """Replace a setlist's contents with rig_ids in order: remove/reorder/insert in one operation."""
    sql = "delete from setlist_rigs where setlist_id = %s" % sql_lit(setlist_id)
    for n, rid in enumerate(rig_ids):   # None represents a missing rig slot (NULL rig_id), as on the device
        sql += "; insert into setlist_rigs (id, setlist_id, rig_id, show_order) values (%s, %s, %s, %d)" % (
            sql_lit(new_id()), sql_lit(setlist_id), "null" if rid is None else sql_lit(rid), n)
    br.sql_write(sql)


def db_setlist_rig_ids(br, setlist_id):
    return [r["rig_id"] for r in br.sql("select rig_id from setlist_rigs where setlist_id = %s order by show_order"
                                        % sql_lit(setlist_id))]


def db_setlist_members(br):
    """All ordered setlist rig IDs: {setlist_id: [rig_id or None]}.
    Paginate to avoid oversized replies for large lists."""
    q = "select setlist_id, rig_id from setlist_rigs order by setlist_id, show_order"
    out, rows, page = {}, [], _page(br, 400)
    while True:
        part = br.sql("%s limit %d offset %d" % (q, page, len(rows)))
        rows += part
        if len(part) < page:
            break
    for r in rows:
        out.setdefault(r["setlist_id"], []).append(r.get("rig_id"))
    return out


def set_rig_prognums(br, pairs):
    """Set program numbers in one transaction: pairs=[(rig_id, displayed_number)].
    Remove any existing ownership, as in set_rig_prognum."""
    sql = []
    for rid, prog in pairs:
        if not 1 <= int(prog) <= MAX_PROG:
            raise LiveError("Program numbers go from 1 to %d." % MAX_PROG)
        pn = int(prog) - 1
        sql.append("update rigs set prog_num = -1 where prog_num = %d and id <> %s" % (pn, sql_lit(rid)))
        sql.append("update rigs set prog_num = %d where id = %s" % (pn, sql_lit(rid)))
    if sql:
        br.sql_write("; ".join(sql))


# ---- Save block preset (bridge 0.4) ----
#
# A user preset is a rig's block subtree (data.Patch.children.<block>) excluding
# PresetName/PresetName2/Doubling/On/Colour and half-B parameters (suffix 2),
# in rig parameter order, under the block base name, with info.version 1.0.9
# (derived from 10 device user presets). Read live values so
# unsaved edits are included.

PRESET_OMIT = {"PresetName", "PresetName2", "Doubling", "On", "Colour"}
PRESET_VERSION = "1.0.9"


def _base_module(module):
    return base_name(PRESET_MODULE.get(module, module))


def preset_content_from_rig(content, module, half="A"):
    """Block preset content JSON from rig content; half='B' uses parameters
    suffixed 2, stored without the suffix."""
    try:
        patch = json.loads(content)["data"]["Patch"]["children"]
    except (ValueError, KeyError, TypeError) as e:
        raise LiveError("Rig content not readable (%s)." % e)
    key = PRESET_MODULE.get(module, module)
    if key not in patch:
        raise LiveError("Block '%s' is not in the saved rig - please save first." % module)
    node = patch[key]
    children = node.get("children", {})
    order, out = [], {}
    for p in node.get("childorder", []) + sorted(k for k in children if k not in node.get("childorder", [])):
        if p in PRESET_OMIT or p not in children:
            continue
        is_b = len(p) > 1 and p.endswith("2") and p[:-1] in children and p[:-1] not in PRESET_OMIT
        if half == "B":
            if not is_b:
                continue
            name = p[:-1]
        else:
            if is_b:
                continue
            name = p
        if name in out:
            continue
        order.append(name)
        out[name] = dict(children[p])
    if not out:
        raise LiveError("Block '%s' has no parameters for a preset." % module)
    return json.dumps({"data": {_base_module(module): {"childorder": order, "children": out}},
                       "info": {"version": PRESET_VERSION}}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def preset_content_live(br, content, module, half="A"):
    """Like preset_content_from_rig, but with current device values."""
    text = preset_content_from_rig(content, module, half)
    d = json.loads(text)
    blk = next(iter(d["data"].values()))
    mod = PRESET_MODULE.get(module, module)
    suffix = "2" if half == "B" else ""
    paths = {LiveRig.path(mod, p + suffix): p for p in blk["childorder"] if p not in PRESET_SKIP}
    info = br.get_many(list(paths))
    for path, p in paths.items():
        i = info.get(path) or {}
        if not i.get("ok"):
            continue
        n = blk["children"][p]
        t = n.get("type")
        if t == 0 and i.get("unnormalized") is not None:
            v = i["unnormalized"]
            n["value"] = int(v) if float(v).is_integer() else v
        elif t in (1, 3) and i.get("state") is not None:
            n["state"] = bool(i["state"])
        elif t in (4, 8) and i.get("string") is not None:
            n["string"] = i["string"]
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def save_block_preset(br, rig_id, module, name, half="A", live=True, replace=False):
    """Create a named module preset for rig_id in the DB (bridge 0.4).
    live=True reads device values, otherwise saved rig values. replace=True
    overwrites a user preset with the same name. Set the device block's preset
    name afterwards. Return (preset_id, 'new'|'replaced'), legacy internal status keys."""
    name = " ".join(name.split())   # Preset names may contain lowercase letters, matching the device
    if not name:
        raise LiveError("The preset name must not be empty.")
    if name.startswith("+"):
        raise LiveError("Names starting with '+' are reserved for factory presets.")
    typ = preset_type(module)
    content = db_rig(br, rig_id)["content"]
    text = preset_content_live(br, content, module, half) if live else preset_content_from_rig(content, module, half)
    rows = br.sql("select id, is_readonly from blocks where type = %s and name = %s collate nocase"
                  % (sql_lit(typ), sql_lit(name)))
    if rows:
        if rows[0].get("is_readonly") or not replace:
            raise LiveError("There is already a preset '%s' for %s." % (name, typ))
        pid = rows[0]["id"]
        br.sql_write("update blocks set content = %s where id = %s" % (sql_lit(text), sql_lit(pid)))
        what = "replaced"
    else:
        pid = new_id()
        br.sql_write("insert into blocks (id, type, name, content, is_readonly) values (%s, %s, %s, %s, 0)"
                     % (sql_lit(pid), sql_lit(typ), sql_lit(name), sql_lit(text)))
        what = "new"
    br.set(preset_name_path(module, half), "string", name)
    return pid, what


def delete_block_preset(br, preset_id):
    rows = br.sql("select is_readonly from blocks where id = %s" % sql_lit(preset_id))
    if rows and rows[0].get("is_readonly"):
        raise LiveError("Factory presets cannot be deleted.")
    return br.sql_write("delete from blocks where id = %s" % sql_lit(preset_id))


# ---- Upload files (inverse of backup_all) ----

def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise LiveError("%s: not readable (%s)" % (os.path.basename(path), e))


def rig_name_of_file(d, path):
    """Rig name from .rig: inner PresetName, otherwise filename."""
    try:
        return json.loads(d["content"])["data"]["Patch"]["children"]["Rig"]["children"]["PresetName"]["string"]
    except (KeyError, ValueError, TypeError):
        return os.path.splitext(os.path.basename(path))[0]


def db_ids(br, table, page=500):
    ids, page = set(), _page(br, page)
    while True:
        part = br.sql("select id from %s limit %d offset %d" % (table, page, len(ids)))
        ids.update(r["id"] for r in part)
        if len(part) < page:
            return ids


def import_rig_file(br, path, name=None, mode="new", setlist_id=None, keep_prog=False):
    """Import a .rig file. mode='new': new rig/new ID, named from file or name;
    'replace': overwrite existing file ID's content/color; 'restore': create
    with file ID only if absent. keep_prog preserves the file's program number
    if free. Return (rig_id, 'new'|'replaced'|'existing'), legacy internal status keys."""
    d = _read_json_file(path)
    if "content" not in d:
        raise LiveError("%s is not a .rig file." % os.path.basename(path))
    name = name or rig_name_of_file(d, path)
    # Preserve device-written file names, including Unicode (sql_lit uses hex);
    # only user-supplied names undergo device-style validation
    name = check_rig_name(name) if mode == "new" else (" ".join(name.split()) or os.path.basename(path))
    color = int(d.get("color") or 0)
    prog = int(d.get("prog_num", -1)) + 1 if keep_prog and int(d.get("prog_num", -1)) >= 0 else None
    if prog and db_prognum_owner(br, prog):
        prog = None
    fid = d.get("id") or new_id()
    exists = bool(br.sql("select 1 from rigs where id = %s" % sql_lit(fid)))
    if mode == "replace" and exists:
        content = content_with_name(d["content"], name)
        sql = "update rigs set content = %s, color = %d" % (sql_lit(content), color)
        if prog is not None:
            sql += ", prog_num = %d" % (prog - 1)
        br.sql_write(sql + " where id = %s" % sql_lit(fid))
        return fid, "replaced"
    if mode == "restore":
        if exists:
            return fid, "existing"
        if db_name_taken(br, "rigs", name):
            name = _free_name(br, "rigs", name)
        content = content_with_name(d["content"], name)
        pn = -1 if prog is None else prog - 1
        sql = ("insert into rigs (id, name, content, show_order, author, is_readonly, color, created_at, prog_num) "
               "values (%s, %s, %s, %d, %s, 0, %d, %d, %d)"
               % (sql_lit(fid), sql_lit(name), sql_lit(content), int(d.get("order") or 0),
                  sql_lit(d.get("author") or RIG_AUTHOR), color, int(d.get("created_at") or time.time()), pn))
        if setlist_id:
            sql += "; " + _setlist_append_sql(setlist_id, fid)
        br.sql_write(sql)
        return fid, "new"
    if db_name_taken(br, "rigs", name):
        name = _free_name(br, "rigs", name)
    rid = create_rig(br, name, content=d["content"], color=color, prog=prog, setlist_id=setlist_id)
    return rid, "new"


def _free_name(br, table, name):
    """'NAME' -> 'NAME 2', 'NAME 3', ... until unused."""
    base = name[:29]
    for n in range(2, 100):
        cand = "%s %d" % (base, n)
        if not db_name_taken(br, table, cand):
            return cand
    raise LiveError("No free name for '%s'." % name)


def import_setlist_file(br, path, rig_ids_present):
    """Import a .setlist if its ID is absent. Missing rigs become empty slots
    (NULL rig_id), as on device. Return legacy internal status 'new'|'existing'."""
    d = _read_json_file(path)
    if "rigs" not in d:
        raise LiveError("%s is not a .setlist file." % os.path.basename(path))
    sid = d.get("id") or new_id()
    if br.sql("select 1 from setlists where id = %s" % sql_lit(sid)):
        return "existing"
    name = os.path.splitext(os.path.basename(path))[0]
    if db_name_taken(br, "setlists", name):
        name = _free_name(br, "setlists", name)
    sql = ("insert into setlists (id, name, author, is_readonly, created_at) values (%s, %s, %s, 0, %d)"
           % (sql_lit(sid), sql_lit(name), sql_lit(d.get("author") or RIG_AUTHOR), int(d.get("created_at") or time.time())))
    for n, rid in enumerate(d["rigs"]):
        sql += "; insert into setlist_rigs (id, setlist_id, rig_id, show_order) values (%s, %s, %s, %d)" % (
            sql_lit(new_id()), sql_lit(sid), sql_lit(rid) if rid in rig_ids_present else "NULL", n)
    br.sql_write(sql)
    return "new"


def import_block_file(br, path):
    """Import a .block preset if its ID and (type, name) are unused.
    Use filename as preset name. Return legacy internal status 'new'|'existing'."""
    d = _read_json_file(path)
    if "content" not in d or "type" not in d:
        raise LiveError("%s is not a .block file." % os.path.basename(path))
    bid = d.get("id") or new_id()
    name = os.path.splitext(os.path.basename(path))[0]
    if br.sql("select 1 from blocks where id = %s or (type = %s and name = %s)"
              % (sql_lit(bid), sql_lit(d["type"]), sql_lit(name))):
        return "existing"
    br.sql_write("insert into blocks (id, type, name, content, is_readonly) values (%s, %s, %s, %s, 0)"
                 % (sql_lit(bid), sql_lit(d["type"]), sql_lit(name), sql_lit(d["content"])))
    return "new"


def restore_all(br, folder, progress=None, replace=False):
    """Inverse of backup_all: restore Rigs/, Setlists/, Blocks/ from folder.
    Preserve existing rig/preset IDs unless replace=True overwrites rigs.
    Create setlists only for absent IDs. Call restart_app afterwards so the
    device sees new setlists. Return counters and an error list."""
    say = progress or (lambda t: None)
    counts = {"rigs": 0, "rigs_replaced": 0, "rigs_existing": 0, "setlists": 0, "blocks": 0, "errors": []}
    rig_dir, set_dir, blk_dir = (os.path.join(folder, d) for d in ("Rigs", "Setlists", "Blocks"))
    rig_files = sorted(f for f in os.listdir(rig_dir) if f.lower().endswith(".rig")) if os.path.isdir(rig_dir) else []
    present = db_ids(br, "rigs")
    for n, f in enumerate(rig_files, 1):
        say("Rig %d of %d: %s" % (n, len(rig_files), f[:-4]))
        try:
            d = _read_json_file(os.path.join(rig_dir, f))
            mode = "replace" if replace and d.get("id") in present else "restore"
            rid, what = import_rig_file(br, os.path.join(rig_dir, f), mode=mode, keep_prog=True)
            counts["rigs" if what == "new" else "rigs_" + what] += 1
            present.add(rid)
        except LiveError as e:
            counts["errors"].append(str(e))
    if os.path.isdir(set_dir):
        for f in sorted(os.listdir(set_dir)):
            if not f.lower().endswith(".setlist"):
                continue
            say("Setlist %s" % f[:-8])
            try:
                if import_setlist_file(br, os.path.join(set_dir, f), present) == "new":
                    counts["setlists"] += 1
            except LiveError as e:
                counts["errors"].append(str(e))
    if os.path.isdir(blk_dir):
        files = [os.path.join(r, f) for r, _, fs in os.walk(blk_dir) for f in fs if f.lower().endswith(".block")]
        for n, p in enumerate(sorted(files), 1):
            if n % 10 == 1:
                say("Block preset %d of %d" % (n, len(files)))
            try:
                if import_block_file(br, p) == "new":
                    counts["blocks"] += 1
            except LiveError as e:
                counts["errors"].append(str(e))
    return counts


def _set_rig_content(br, rig_id, content):
    br.sql_write("update rigs set content = %s where id = %s" % (sql_lit(content), sql_lit(rig_id)))


def stash_edits(br, rig_id):
    """Before restart, preserve loaded rig_id's unsaved edits without permanently
    saving it: remember its row, save on device to capture live state, retain
    that state and immediately restore the old row. Return {id, saved, live}
    or None (clean/different loaded rig). Read-only rigs cannot be saved and
    raise LiveError."""
    st = rig_status(br)
    if not rig_id or st["id"] != rig_id or not st["dirty"]:
        return None
    old = db_rig(br, rig_id)
    if int(old.get("is_readonly") or 0):
        raise LiveError("“%s” is read-only on the %s, so its unsaved edits could not be kept." % (old.get("name"), DEV.name))
    if save_rig(br)["dirty"]:
        raise LiveError("The %s did not save its unsaved edits. Was a dialog open on the display?" % DEV.name)
    live = db_rig(br, rig_id)["content"]
    if live != old["content"]:
        _set_rig_content(br, rig_id, old["content"])
    return {"id": rig_id, "saved": old["content"], "live": live}


def restart_app(br, rig_id=None, setlist=None, progress=None, timeout=90, action="restart", keep_edits=False):
    """Restart the device app (bridge 0.4), needed after setlist changes because
    SetName1 reads lists only at startup. Takes ~10 s; unsaved edits are lost
    unless preserved. Answer 'restore last state?' with No (FS2), giving a
    defined state: nothing loaded, All Rigs active. Yes restores an older,
    possibly different dirty rig that prompts on the next switch. Then enter
    setlist and load rig_id. Return read_rig_list-style data. action defaults
    to restart; nam-on/nam-off toggle NAM first (bridge 0.7). keep_edits uses
    stash_edits: load live state, restore the saved DB row, mark the rig dirty.
    res['edits_kept'] reports success; res['edits_lost'] explains failure.
    progress(text, fraction 0..1 or None)."""
    say = progress or (lambda t, f=None: None)
    stash, lost = None, None
    if keep_edits and rig_id:
        say("Keeping your unsaved edits ...", 0.02)
        try:
            stash = stash_edits(br, rig_id)
        except LiveError as e:
            lost = str(e)
    say("Stopping the device app ...", 0.04)
    board = br.get(BOARD)   # Device view; otherwise restart leaves it in SetRig (2026-09-30)
    start = time.time()
    br.action(action)
    end = start + timeout
    waiting = lambda: say("Waiting for the device app to start ...",
                          0.06 + 0.54 * min(1.0, (time.time() - start) / 11.0))
    while time.time() < start + 3:
        waiting()
        time.sleep(0.5)
    while True:   # The bridge returns with the new app, usually after ~9 s
        try:
            br.ping()
            break
        except BridgeError:
            if time.time() > end:
                raise LiveError("The device app is not reachable after the restart.")
            waiting()
            time.sleep(1)
    say("App is running again, waiting for the dialog ...", 0.62)
    while time.time() < end:
        b = br.get(BOARD).get("string") == "FreeText" and dialog_buttons(br)
        if b and dialog_key(b, "yes") and dialog_key(b, "no"):
            press(br, FS % dialog_key(b, "no"))   # No means restore nothing (MX5: FS2)
            for _ in range(20):
                if br.get(BOARD).get("string") != "FreeText":
                    break
                time.sleep(0.1)
            break
        time.sleep(0.5)
    time.sleep(SETTLE)
    if stash:   # Put live state in the row before reading lists/loading the rig
        _set_rig_content(br, rig_id, stash["live"])
    try:
        say("Reading the rig list ...", 0.72)
        res = read_rig_list(br)
        if setlist and setlist != res["setlist"] and (setlist == ALL_RIGS or setlist in res["setlist_ids"]):
            say("Entering setlist %s ..." % setlist, 0.8)
            res = enter_setlist(br, res["setlists"], setlist, res["setlist_ids"], discard=True)
        ids = [r["id"] for r in res["rows"]]
        if rig_id in ids and rig_status(br)["id"] != rig_id:
            say("Loading the rig again ...", 0.88)
            res["status"] = load_rig(br, res["rows"], ids.index(rig_id), discard=True)
    finally:
        if stash:   # Restore saved state in the DB
            _set_rig_content(br, rig_id, stash["saved"])
    if stash:
        if rig_status(br)["id"] == rig_id:
            say("Restoring your unsaved edits ...", 0.95)
            keep_changes(br)   # Live state in memory differs from saved DB state: mark dirty
            res["status"] = rig_status(br)
            res["edits_kept"] = True
        else:
            lost = "The rig was not loaded again after the restart."
    if lost:
        res["edits_lost"] = lost
    restore_board(br, board)
    return res


VIEW_MODES = ("Stomp", "Rig", "Hybrid", "Set", "SetRig", "Looper")   # BoardMode views excluding dialogs/tuner


def restore_board(br, before):
    """Restore device BoardMode from a get reply after an action changes it;
    only ordinary views, never dialogs or tuner."""
    if not before or before.get("string") not in VIEW_MODES or "index" not in before:
        return
    for _ in range(20):   # A dialog or tuner is still closing
        now = br.get(BOARD).get("string")
        if now in VIEW_MODES:
            break
        time.sleep(0.1)
    else:
        return
    if now != before["string"]:
        br.set(BOARD, "index", before["index"])


def refresh_lists(br, setlists, setlist, setlist_ids, reload_id=None, reload_row=False):
    """After DB changes, reenter the active setlist/All Rigs to reread it (~1 s,
    no rig load). Other lists reread on their next entry. reload_id loads a
    different rig (save_rig_as), or with reload_row=True reloads the current
    rig whose row changed. Names/colors/programs live in device memory; use
    bank navigation because ReceivePresetIndex does nothing for the current rig.
    Return enter_setlist-style data."""
    if setlist == ALL_RIGS or setlist not in (setlist_ids or {}):
        setlist = ALL_RIGS
    res = enter_setlist(br, setlists, setlist, setlist_ids, active=True)
    ids = [r["id"] for r in res["rows"]]
    st = res["status"]
    if reload_id in ids and (st["id"] != reload_id or reload_row):
        res["status"] = load_rig(br, res["rows"], ids.index(reload_id), discard=st["id"] == reload_id or st["dirty"])
    return res


# ---- Impulse responses (IR files) ----
#
# IR files persist in the device's USB image (usb.img, mounted at IR_DIR,
# vfat/UTF-8): <folder>/<name>.wav, one folder level.
# Factory IRs from /usr/Evil/Content are copied there. The block stores
# '[directory](<folder>)[name](<name without extension>)' in IR/IR (half B:
# IR2; block types IR and IR (1024)). Writing string loads immediately
# (PresetCtrl/Resources/IRNotFound[Lite][2][_B] state 1 means missing file)
# without setting Dirty. '[IR ROOT]' searches all folders, taking the first
# matching name; '[USER]' means USER folder. The device scans only at app startup
# and after USB synchronization: restart_app is required after any file change
# before new files can load, including in existing folders
# (verified 2026-09-17). File access uses Bridge.read_file/write_file/list_dir/shell
# (bridge 0.3).

IR_DIR = "/media/az01-internal/Evil/usb_mnt/Impulse Responses"
IR_ROOT, IR_USER = "[IR ROOT]", "[USER]"
IR_EXT = (".wav", ".mp3")         # Matches the device app's filter: .(wav|mp3)$
IR_MODULES = ("IR", "IR (1024)")
IR_MAX_KB = 2048                  # Reject larger files; the device only uses the beginning anyway


def ir_string(folder, name):
    return "[directory](%s)[name](%s)" % (folder, name)


def parse_ir(s):
    """'[directory](folder)[name](name)' -> (folder, name); other text -> (None, text)."""
    s = s or ""
    if s.startswith("[directory](") and "[name](" in s and s.endswith(")"):
        d, _, n = s[len("[directory]("):-1].partition(")[name](")
        return d, n
    return None, s


def ir_label(s, folder=True):
    """Display like IRInfoString: 'folder / name' (folder=False: name only)."""
    d, n = parse_ir(s)
    if d is None:
        return n
    return "%s / %s" % (d, n) if folder and d else n


def ir_folder_path(folder):
    if folder == IR_ROOT or not folder:
        return IR_DIR
    return IR_DIR + "/" + ("USER" if folder == IR_USER else folder)


def ir_file_path(folder, filename):
    return ir_folder_path(folder) + "/" + filename


def _ir_folder_name(dirname):
    return IR_USER if dirname == "USER" else dirname


def check_ir_name(name, what="Name"):
    """Error text if name is invalid as a device file/folder name, otherwise None."""
    if not name or name != name.strip() or name.endswith("."):
        return "%s must not be empty and must not start/end with a space or dot." % what
    bad = [c for c in name if c in _BAD_CHARS or ord(c) < 32]
    if bad:
        return "%s contains forbidden characters: %s" % (what, " ".join(sorted(set(bad))))
    if name.startswith("[") and name.endswith("]"):
        return "%s must not be in square brackets (reserved)." % what
    if len(name.encode("utf-8")) > 120:
        return "%s is too long." % what
    return None


def db_irs(br):
    """All device IRs: {'folders': [folder], 'files': {folder: [(name, filename)]}}.
    Folders follow device order; include [IR ROOT] only if root files exist.
    Files sort case-insensitively. One query, ~0.3 s."""
    return _ir_index((rel, is_dir) for rel, is_dir, _ in br.list_dir(IR_DIR, recursive=True))


def _ir_index(entries):
    """Convert [(relative/path, is_directory)] to db_irs layout:
    one folder level, .wav files only."""
    files, folders = {}, set()
    for rel, is_dir in entries:
        parts = rel.split("/")
        if is_dir:
            if len(parts) == 1:
                folders.add(_ir_folder_name(parts[0]))
            continue
        if len(parts) > 2 or not rel.lower().endswith(IR_EXT):
            continue
        folder = _ir_folder_name(parts[0]) if len(parts) == 2 else IR_ROOT
        fn = parts[-1]
        files.setdefault(folder, []).append((fn[:fn.rfind(".")], fn))
    for lst in files.values():
        lst.sort(key=lambda t: t[0].lower())
    order = sorted(folders | set(files), key=lambda f: (f not in (IR_ROOT, IR_USER), f))
    return {"folders": order, "files": {f: files.get(f, []) for f in order}}


def ir_filename(irs, folder, name):
    """Filename with extension for db_irs folder/name, or None."""
    for n, fn in irs["files"].get(folder, []):
        if n == name:
            return fn
    return None


def wav_info(data):
    """WAV (channels, sample rate, bits, length in frames), or None."""
    if len(data) < 36 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt, size = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        ln = int.from_bytes(data[pos + 4:pos + 8], "little")
        if cid == b"fmt " and ln >= 16:
            ch = int.from_bytes(data[pos + 10:pos + 12], "little")
            rate = int.from_bytes(data[pos + 12:pos + 16], "little")
            bits = int.from_bytes(data[pos + 22:pos + 24], "little")
            fmt = (ch, rate, bits)
        elif cid == b"data":
            size = min(ln, len(data) - pos - 8)
        pos += 8 + ln + (ln & 1)
    if not fmt:
        return None
    ch, rate, bits = fmt
    frames = size // max(1, ch * max(bits, 8) // 8) if size is not None else 0
    return ch, rate, bits, frames


def ir_create_folder(br, folder):
    err = check_ir_name(folder, "Folder name")
    if err:
        raise LiveError(err)
    if folder in (IR_ROOT, IR_USER):
        raise LiveError("This folder already exists.")
    br.make_dir(ir_folder_path(folder))


def ir_upload(br, folder, path, name=None, replace=False, progress=None):
    """Upload local .wav/.mp3 IR to device <folder>/<name>.<extension>.
    Return (name, filename). Existing files fail unless replace=True.
    progress(written, total), as in Bridge.write_file."""
    base, ext = os.path.splitext(os.path.basename(path))
    ext = ext.lower()
    if ext not in IR_EXT:
        raise LiveError("Only .wav or .mp3 files: %s" % os.path.basename(path))
    name = (name or base).strip()
    err = check_ir_name(name)
    if err:
        raise LiveError(err)
    data = open(path, "rb").read()
    if len(data) > IR_MAX_KB * 1024:
        raise LiveError("%s is too large (%d KB, at most %d KB)." % (os.path.basename(path), len(data) // 1024, IR_MAX_KB))
    if ext == ".wav" and wav_info(data) is None:
        raise LiveError("%s is not a WAV file." % os.path.basename(path))
    target = ir_file_path(folder, name + ext)
    if not replace:
        taken = {os.path.splitext(n)[0].lower() for n, d, _ in br.list_dir(ir_folder_path(folder)) if not d}
        if name.lower() in taken:
            raise LiveError("Folder %s already has an IR called %s." % (folder, name))
    br.write_file(target, data, progress=progress)
    return name, name + ext


def ir_download(br, folder, filename, local_path):
    data = br.read_file(ir_file_path(folder, filename))
    with open(local_path, "wb") as f:
        f.write(data)
    return len(data)


def _sh(br, cmd):
    rc, out = br.shell(cmd)
    if rc != 0:
        raise LiveError("Device command failed: %s" % (out.strip() or cmd))


def ir_delete(br, folder, filename):
    _sh(br, "rm -f %s" % sh_quote(ir_file_path(folder, filename)))


def ir_rename(br, folder, filename, new_name):
    """Rename a file preserving its extension; return the new filename."""
    err = check_ir_name(new_name)
    if err:
        raise LiveError(err)
    new_fn = new_name + os.path.splitext(filename)[1]
    _sh(br, "mv -n %s %s" % (sh_quote(ir_file_path(folder, filename)), sh_quote(ir_file_path(folder, new_fn))))
    return new_fn


def ir_delete_folder(br, folder):
    if folder in (IR_ROOT, IR_USER):
        raise LiveError("This folder cannot be deleted.")
    _sh(br, "rm -r %s" % sh_quote(ir_folder_path(folder)))


def ir_rename_folder(br, folder, new_name):
    if folder in (IR_ROOT, IR_USER):
        raise LiveError("This folder cannot be renamed.")
    err = check_ir_name(new_name, "Folder name")
    if err:
        raise LiveError(err)
    _sh(br, "mv -n %s %s" % (sh_quote(ir_folder_path(folder)), sh_quote(ir_folder_path(new_name))))


def db_rigs_using_ir(br, folder, name):
    """Names of rigs referencing this IR, including half B."""
    needle = ir_string(folder, name)
    rows = br.sql("select name from rigs where instr(content, %s) > 0 order by name" % sql_lit(needle))
    return [r["name"] for r in rows]


def read_ir_folder(folder):
    """Offline USB export Impulse Responses -> db_irs format, empty if absent."""
    entries = []
    if os.path.isdir(folder):
        for entry in os.listdir(folder):
            p = os.path.join(folder, entry)
            entries.append((entry, os.path.isdir(p)))
            if os.path.isdir(p):
                entries += [(entry + "/" + fn, os.path.isdir(os.path.join(p, fn))) for fn in os.listdir(p)]
    return _ir_index(entries)


# ---- Device model ----
#
# Bridge also builds for Pedalboard/Gigboard (same firmware 2.7 family,
# Andere_Geraete/BEFUND.txt). Original MX5 hardware assumptions: three switches
# FootSwitch5-7, banks of three rigs, dialogs on FS1-3. Model comes from the device tree.
DEVICE_MODELS = {"inmusic,hg04": "MX5", "inmusic,mg01": "Pedalboard", "inmusic,hg02": "Gigboard"}


def device_model(br):
    """Return 'MX5', 'Pedalboard', 'Gigboard', or another compatible value;
    None if unreadable (retain original behavior). Cache in br.model."""
    if not hasattr(br, "model"):
        try:
            rc, out = br.shell("tr '\\0' ' ' < /proc/device-tree/compatible")
            first = out.split()[0] if rc == 0 and out.split() else ""
            br.model = DEVICE_MODELS.get(first, first) or None
        except Exception:
            br.model = None
    return br.model


# ---- NAM mod (bridge 0.7, built with NAM) ----
#
# lolgab's NAM mod (github.com/lolgab/headrush-nam-mod) replaces Anxiety OD's
# audio processing (Anxiety OD and Anxiety OD 2; with four instances also
# Anxiety OD V2 [2]) with Neural Amp Modeler. Controls (verified against v0.1.6
# patch/nam_hook.cpp): Drive selects a model; unnormalized 0..100 is the slot
# in NAM_DIR's .nam list sorted by filename bytes; absent slots
# are silent. Tone/Level control input/output: below 50% they attenuate linearly
# to silence; above 50% they add 0..+12 dB. Hi-Lo does nothing. Scan once at app startup,
# skipping unloadable files, which shifts all subsequent slots.
# Restart after every file change. NAM convention:
# 'NNN - Name.nam', contiguous number = slot. Rigs store Drive as a slot;
# moving/deleting models requires updating rigs with nam_remap.
# Bridge launches with NAM only when enabled (nam-on/nam-off actions).

NAM_DIR = "/media/az01-internal/Evil/usb_mnt/NAM"
NAM_INFO = "/usr/Evil/Scripts/mx5bridge-nam.txt"
NAM_EXT = ".nam"
NAM_STEPS = 101                  # Drive 0..100%; one model per percent
NAM_SILENT = NAM_STEPS - 1       # No-model slot, used only when a 101st model exists
NAM_V1 = ("Anxiety OD", "Anxiety OD 2")
NAM_V2 = ("Anxiety OD V2", "Anxiety OD V2 2")
NAM_PRESET_TYPES = {"Anxiety OD": "ANXIETY OD", "Anxiety OD V2": "ANXIETY OD V2"}
NAM_MAX_MB = 30                  # Reject larger files (A1/A2 models are 0.1..5 MB)
NAM_TRIM_DB = 12.0               # Tone/Level above 50%: up to +12 dB (NAM_TRIM_MAX_DB)


def nam_status(br):
    """Device NAM state: {installed, on, active, auto_off, ref, instances}.
    installed=False without NAM or before bridge 0.7. on applies to next launch;
    active means the running app launched with NAM."""
    off = {"installed": False, "on": False, "active": False, "auto_off": False, "ref": "", "instances": 0}
    if br.vnum < (0, 7):
        return off
    st = br.action("status")
    if not st.get("nam_installed"):
        return off
    rc, out = br.shell("cat %s 2>/dev/null" % sh_quote(NAM_INFO))
    info = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    try:
        inst = int(info.get("nam_instanzen", 2))
    except ValueError:
        inst = 2
    return {"installed": True, "on": bool(st.get("nam_on")), "active": bool(st.get("nam_active")),
            "auto_off": bool(st.get("nam_auto_off")), "ref": st.get("nam_ref") or info.get("nam_mod_ref", ""),
            "instances": inst}


def nam_modules(nam):
    """Block names playing NAM, empty if inactive in the running app."""
    if not nam or not nam.get("active"):
        return ()
    return NAM_V1 + (NAM_V2 if nam.get("instances", 2) >= 4 else ())


NAM_NAMES = {"Anxiety OD": "NAM", "Anxiety OD V2": "NAM V2"}   # Editor display names: base name -> name


def nam_display(m):
    """NAM block display name: 'Anxiety OD 2' -> 'NAM 2', 'Anxiety OD V2' -> 'NAM V2'."""
    base = base_name(m)
    return NAM_NAMES.get(base, base) + m[len(base):]


def nam_base(fn):
    """Model display filename without 'NNN - ' prefix or .nam extension."""
    n = fn[:-len(NAM_EXT)] if fn.lower().endswith(NAM_EXT) else fn
    if len(n) > 6 and n[:3].isdigit() and n[3:6] == " - ":
        n = n[6:]
    return n


def nam_filename(index, name):
    return "%03d - %s%s" % (index, name, NAM_EXT)


def _nam_index(files):
    """[(filename, bytes)] -> device-ordered models: *.nam only, excluding macOS
    '._' companions; sort filename bytes like NAM's std::sort.
    Each model: {file, name, size, index}; index is its selecting Drive value."""
    ok = [(fn, size) for fn, size in files
          if fn.lower().endswith(NAM_EXT) and not fn.startswith("._")]
    ok.sort(key=lambda t: t[0].encode("utf-8"))
    return [{"file": fn, "name": nam_base(fn), "size": size, "index": i} for i, (fn, size) in enumerate(ok)]


def db_nam_models(br):
    """Device NAM models: {'exists': folder_exists, 'models': [...] like _nam_index}."""
    q = sh_quote(NAM_DIR)
    rc, out = br.shell("[ -d %s ] || { echo NODIR; exit 0; }\ncd %s || exit 1\n"
                       "for f in * .[!.]*; do [ -f \"$f\" ] && printf '%%s\\t%%s\\n' \"$(wc -c < \"$f\")\" \"$f\"; done; true"
                       % (q, q))
    if rc != 0:
        raise LiveError("Could not read the NAM folder: %s" % out.strip())
    if out.strip() == "NODIR":
        return {"exists": False, "models": []}
    files = []
    for line in out.splitlines():
        size, _, fn = line.partition("\t")
        if fn and size.strip().isdigit():
            files.append((fn, int(size)))
    return {"exists": True, "models": _nam_index(files)}


def check_nam_name(name):
    """Error text for a model name without number/extension, otherwise None."""
    err = check_ir_name(name)
    if err:
        return err
    if len(name.encode("utf-8")) > 200:
        return "Name is too long."
    return None


def check_nam_data(data, what="The file"):
    """Validate .nam like NAM's loader (JSON architecture/config/weights).
    Return a short description ('WaveNet, 13 KB weights') or raise LiveError:
    unloadable files would shift all following model slots."""
    try:
        j = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise LiveError("%s is not a NAM model (no JSON: %s)." % (what, e))
    if not isinstance(j, dict) or "architecture" not in j or "weights" not in j and "config" not in j:
        raise LiveError("%s is not a NAM model (architecture/weights missing)." % what)
    arch = str(j.get("architecture"))
    meta = j.get("metadata") if isinstance(j.get("metadata"), dict) else {}
    extra = meta.get("name") or meta.get("gear_make") or ""
    return arch + (" · " + str(extra) if extra else "")


def nam_download(br, fn, local_path):
    data = br.read_file(NAM_DIR + "/" + fn)
    with open(local_path, "wb") as f:
        f.write(data)
    return len(data)


def nam_rename_files(br, renames, delete=()):
    """Delete/rename files in one shell script using temporary names, since a new
    name may be another file's old name. First verify targets are not owned
    by files that remain."""
    if not renames and not delete:
        return
    d = sh_quote(NAM_DIR)
    lines = ["cd %s || exit 1" % d]
    lines += ["rm -f -- %s || exit 1" % sh_quote(fn) for fn in delete]
    lines += ["mv -- %s %s || exit 1" % (sh_quote(old), sh_quote(".mx5b_tmp_%d" % i)) for i, (old, _) in enumerate(renames)]
    lines += ["[ -e %s ] && { echo exists: %s; exit 1; }; mv -- %s %s || exit 1"
              % (sh_quote(new), sh_quote(new), sh_quote(".mx5b_tmp_%d" % i), sh_quote(new))
              for i, (_, new) in enumerate(renames)]
    rc, out = br.shell("\n".join(lines))
    if rc != 0:
        raise LiveError("Renaming the NAM models failed: %s\n(Check the NAM folder; temporary files are "
                        "called .mx5b_tmp_*.)" % out.strip())


def _nam_value_sql(json_path):
    return "cast(round(json_extract(content, %s)) as integer)" % sql_lit(json_path)


def _nam_paths(nam):
    """(table, Drive JSON path, display label, extra condition) for NAM rigs/presets.
    Only presets named 'NNN - ...' (NAM/editor convention); Anxiety OD factory
    presets such as BRIGHT/SMOOTH are overdrive settings, not DB read-only entries."""
    out = []
    for m in nam_modules(nam):
        out.append(("rigs", '$.data.Patch.children."%s".children.Drive.value' % m, m, ""))
    for base in sorted({base_name(m) for m in nam_modules(nam)}):
        t = NAM_PRESET_TYPES[base]
        out.append(("blocks", '$.data."%s".children.Drive.value' % base, t,
                    " and type = %s and name glob '[0-9][0-9][0-9] - *'" % sql_lit(t)))
    return out


def nam_usage(br, nam):
    """Model usage across rigs/NAM presets: {slot: ['Rig NAME (block)', 'Preset NAME', ...]}.
    Inspect content, ~0.5 s."""
    usage = {}
    for table, path, what, cond in _nam_paths(nam):
        rows = br.sql("select name, %s as v from %s where json_extract(content, %s) is not null%s"
                      % (_nam_value_sql(path), table, sql_lit(path), cond))
        for r in rows:
            label = "%s (%s)" % (r["name"], what) if table == "rigs" else "Preset %s (%s)" % (r["name"], what)
            usage.setdefault(int(r["v"]), []).append(label)
    return usage


def nam_remap(br, nam, mapping):
    """Remap NAM Drive values in rigs/presets using {old: new}. One transaction;
    json_set preserves remaining content. Return changed row count.
    The device rereads the loaded rig only on its next load."""
    if not mapping:
        return 0
    stmts = []
    for table, path, what, cond in _nam_paths(nam):
        v = _nam_value_sql(path)
        case = "case %s %s end" % (v, " ".join("when %d then %d" % (a, b) for a, b in sorted(mapping.items())))
        stmts.append("update %s set content = json_set(content, %s, %s) where json_extract(content, %s) is not null "
                     "and %s in (%s)%s;" % (table, sql_lit(path), case, sql_lit(path), v,
                                            ",".join(str(a) for a in sorted(mapping)), cond))
    return br.sql_write("\n".join(stmts))


def nam_apply(br, nam, old, final, delete=(), replace=None, progress=None):
    """Rebuild the NAM folder in one operation. old: previous device-ordered filenames.
    final: [(source, display_name)] in new order; source is an old filename or
    local upload path. delete: old files to remove. replace: {old_file: local_path}
    replaces content while keeping name/slot. Upload new files under temporary
    names without .nam so partial models stay invisible. Rename contiguously
    'NNN - Name.nam', delete, and update rig/preset Drive via nam_remap to retain
    each rig's model. progress(text, fraction 0..1 or None).
    Return {'files': count, 'remapped': changed_rigs_or_presets}."""
    say = progress or (lambda t, f=None: None)
    if len(final) > NAM_STEPS:
        raise LiveError("At most %d NAM models are reachable (Drive 0–100 %%)." % NAM_STEPS)
    seen = set()
    for _, name in final:
        err = check_nam_name(name)
        if err:
            raise LiveError("%s: %s" % (name, err))
        if name.lower() in seen:
            raise LiveError("The name “%s” is used twice." % name)
        seen.add(name.lower())
    oldset = set(old)
    replace = {fn: p for fn, p in (replace or {}).items() if fn in oldset}
    ups = [(i, src) for i, (src, _) in enumerate(final) if src not in oldset] + [(None, p) for p in replace.values()]
    for _, src in ups:   # Validate everything before writing
        if not os.path.isfile(src):
            raise LiveError("File not found: %s" % src)
        if os.path.getsize(src) > NAM_MAX_MB * 1024 * 1024:
            raise LiveError("%s is too large (at most %d MB)." % (os.path.basename(src), NAM_MAX_MB))
        with open(src, "rb") as f:
            check_nam_data(f.read(), os.path.basename(src))
    _sh(br, "mkdir -p %s" % sh_quote(NAM_DIR))
    cur = {}
    total = sum(os.path.getsize(src) for _, src in ups) or 1
    sent = 0
    for k, (i, src) in enumerate(ups):   # Upload: 0..80%; rename and update rigs afterwards
        text = "Uploading %s (%d/%d) …" % (os.path.basename(src), k + 1, len(ups))
        say(text, 0.8 * sent / total)
        tmp = ".mx5b_up_%d" % k
        with open(src, "rb") as f:
            data = f.read()
        br.write_file(NAM_DIR + "/" + tmp, data,
                      progress=lambda n, _, t=text, s=sent: say(t, 0.8 * (s + n) / total))
        sent += len(data)
        cur[src] = tmp
    if replace:
        _sh(br, "\n".join("mv -f %s %s" % (sh_quote(NAM_DIR + "/" + cur[p]), sh_quote(NAM_DIR + "/" + fn))
                          for fn, p in replace.items()))
    renames = []
    for i, (src, name) in enumerate(final):
        now, new = cur.get(src, src), nam_filename(i, name)
        if now != new:
            renames.append((now, new))
    say("Renaming models …", 0.85 if ups else 0.2)
    nam_rename_files(br, renames, [fn for fn in delete if fn in oldset])
    pos = {src: i for i, (src, _) in enumerate(final)}
    mapping = {i: pos.get(fn, NAM_SILENT) for i, fn in enumerate(old) if pos.get(fn, NAM_SILENT) != i}
    n = 0
    if mapping:
        say("Updating rigs and presets …", 0.9 if ups else 0.5)
        n = nam_remap(br, nam, mapping)
    return {"files": len(final), "remapped": n}


def nam_trim_text(v):
    """Convert normalized Tone/Level (0..1) to NAM's applied level."""
    import math
    v = float(v)
    if v <= 0:
        return "mute"
    if v < 0.5:
        return "%.1f dB" % (20 * math.log10(v / 0.5))
    return "+%.1f dB" % ((v - 0.5) * 2 * NAM_TRIM_DB) if v > 0.5 else "0.0 dB"


def nam_index_of(info):
    """Drive info -> slot 0..100."""
    try:
        return int(round(float(info.get("unnormalized", 0))))
    except (TypeError, ValueError):
        return 0


# ---- Tuner (verified 2026-09-17) ----
# Press RedirCtrl/AccessTuner to open (BoardMode Tuner,
# AudioCtrl/Input/TunerOn On); ExitTuner or a physical footswitch closes it.
# FFTCtrl/TunerString is the note (empty means no signal); TunerCents unnormalized
# is deviation (-100..100 cents, device display -50..+50), updated ~35 ms.
# get_many takes ~5 ms; editor polls about 30 times/s.
# TunerRef: 410..480 Hz reference (IntegerValue); TunerMuting: mute output while tuning.
FFT = "/Engine/FFTCtrl"
TUNER_NOTE = FFT + "/TunerString"
TUNER_CENTS = FFT + "/TunerCents"
TUNER_REF = FFT + "/TunerRef"
TUNER_MUTE = "/Engine/AudioCtrl/Input/TunerMuting"
TUNER_ON = "/Engine/AudioCtrl/Input/TunerOn"
TUNER_PATHS = [TUNER_NOTE, TUNER_CENTS, TUNER_REF, TUNER_MUTE, BOARD]
TUNER_REF_MIN, TUNER_REF_MAX = 410, 480
TUNER_OK = 6.0        # Absolute cents below this are in tune, matching device threshold


def tuner_open(br, before=None):
    """Open the device tuner and return tuner_parse's first reading. Save the
    previous view on the connection for tuner_close (ExitTuner otherwise returns
    to Hybrid). If already open on device, before is the last normal BoardMode
    reply from editor polling."""
    board = br.get(BOARD) or {}
    if board.get("string") == "Tuner":
        br.tuner_board = before
    else:
        br.tuner_board = board
        press(br, RC + "/AccessTuner")
        for _ in range(20):
            if (br.get(BOARD) or {}).get("string") == "Tuner":
                break
            time.sleep(0.05)
        else:
            raise LiveError("Tuner not opened on the device (BoardMode stays %s)" % (br.get(BOARD) or {}).get("string"))
    return tuner_parse(br.get_many(TUNER_PATHS))


def tuner_close(br):
    """Exit the tuner unless already exited, and restore the previous view."""
    if (br.get(BOARD) or {}).get("string") == "Tuner":
        press(br, RC + "/ExitTuner")
        restore_board(br, getattr(br, "tuner_board", None))
    br.tuner_board = None
    return True


def tuner_parse(res):
    """TUNER_PATHS get_many reply -> {note, cents, ref, mute, active}."""
    note = (res.get(TUNER_NOTE) or {}).get("string") or ""
    cents = (res.get(TUNER_CENTS) or {}).get("unnormalized")
    ref = (res.get(TUNER_REF) or {}).get("unnormalized")
    mute = (res.get(TUNER_MUTE) or {}).get("state")
    board = (res.get(BOARD) or {}).get("string")
    return {"note": note, "cents": float(cents) if cents is not None else 0.0,
            "ref": int(ref) if ref is not None else None, "mute": None if mute is None else bool(mute),
            "active": board == "Tuner", "ok": bool(res.get("ok", True)) and TUNER_CENTS in res}


# ---- Level meters (verified 2026-09-30) ----
# /Engine/InputMeter/Current|Peak (unnormalized dB, -120..0) and OutputMeterL|R/Current|Peak
# (-60..+6) match device input/output meters. Continuously updated; Peak
# holds briefly, then decays. get_many takes ~5 ms.
METERS = {"Input": [("IN", "/Engine/InputMeter")],
          "Output": [("L", "/Engine/OutputMeterL"), ("R", "/Engine/OutputMeterR")]}
METER_RANGE = {"Input": (-60.0, 0.0), "Output": (-60.0, 6.0)}   # Displayed dB range


def meter_paths(module):
    return [p + s for _, p in METERS.get(module, []) for s in ("/Current", "/Peak")]


def meter_parse(module, res):
    """get_many reply -> [(name, current_db, peak_db)]; None for missing values."""
    out = []
    for name, p in METERS.get(module, []):
        cur = (res.get(p + "/Current") or {}).get("unnormalized")
        peak = (res.get(p + "/Peak") or {}).get("unnormalized")
        out.append((name, None if cur is None else float(cur), None if peak is None else float(peak)))
    return out


# ---- Looper (verified 2026-10-11, bridge 0.7) ----
# All under /Engine/Looper (names found by get_many over Evil binary strings;
# the looper page itself is C++). Actions are buttons (state 1 -> 0, press):
#   Record: RecState RecReady -> Rec; second press closes/plays the loop
#     (OverReady), then toggles overdub (Rec <-> OverReady); each recording adds a layer
#   Play: start/stop (StartState Empty|Stopped|Playing); also stops active overdub
#   Stop: stop only
#   Undo: PEEL removes the last layer; removing the sole layer empties the loop (no redo)
#   Clear: delete loop and reset speed/length
#   DirectionChange: forward/reverse (Reverse Normal|Reverse, PlaybackText NORMAL|REVERSE)
#   HalfSpeed/DoubleSpeed  SpeedText x 1/4 .. x 4;  HalfLen/DoubleLen  MultText x 1 .. x 8
#     Length changes apply at the loop end; LengthText follows
# Display: StateText EMPTY|RECORD|PLAY|OVERDUB|READY, RecordText RECORD|OVERDUB, PlayText START|STOP;
#   LengthText mm:ss.z grows while recording; RelativPos unnormalized 0..100
#   is loop position; NumLayers, Modified Y|N (changed since load/save), RemainingRecordingTimeString.
# Settings: Looper/Mix = Settings/LooperMix (global loop level 0..100%); Looper/Feedback
#   (0..100%, overdub decay); AudioCtrl/OtherSettings/LooperLoc Pre|Post (before/after
#   chain, global); RedirCtrl/AltLooperFS Main|Alt (looper-view footswitch layout:
#   Main FS1 start/stop, FS2 record/overdub, FS3 -> Alt; Alt FS2 undo, FS3 -> Main).
# View: RedirCtrl/SelectLooperMode opens BoardMode Looper; ExitLooper returns to Stomp.
# Looper works in any view; playback continues after leaving.
# Mute (TwoStateValue) had no observable effect; Insert/InsertState unclear and unused.
# Load/save loops (USB drive Loops folder) only through device GUI (LooperPage, C++).
# Loop data is app memory only, with no file/waveform property. The editor draws
# each layer's envelope from InputMeter/Current sampled while recording.
# RelativPos updates every ~30-40 ms in 1% steps and continues increasing in reverse,
# without a jump when toggled. LengthText incorporates speed/length
# (double speed halves time); progress is 100% / LengthText.
LOOPER = "/Engine/Looper"
LOOPER_MIX = LOOPER + "/Mix"
LOOPER_FEEDBACK = LOOPER + "/Feedback"
LOOPER_LOC = "/Engine/AudioCtrl/OtherSettings/LooperLoc"
LOOPER_ALT_FS = RC + "/AltLooperFS"
LOOPER_LEVEL = "/Engine/InputMeter/Current"     # Input level in dB for layer envelopes
LOOPER_ACTIONS = {"record": "Record", "play": "Play", "stop": "Stop", "undo": "Undo", "clear": "Clear",
                  "reverse": "DirectionChange", "half_speed": "HalfSpeed", "double_speed": "DoubleSpeed",
                  "half_len": "HalfLen", "double_len": "DoubleLen"}
LOOPER_PATHS = [LOOPER + "/" + k for k in ("StartState", "RecState", "StateText", "RecordText", "PlayText",
                                           "UndoText", "LengthText", "RelativPos", "NumLayers", "SpeedText",
                                           "MultText", "Reverse", "Modified", "RemainingRecordingTimeString")] + [
    LOOPER_MIX, LOOPER_FEEDBACK, LOOPER_LOC, LOOPER_ALT_FS, BOARD, LOOPER_LEVEL]


def looper_open(br, before=None, show=True):
    """Show device looper (show=False leaves view unchanged); return first looper_parse
    reading. Save prior view on the connection for looper_close (ExitLooper
    otherwise returns to Stomp). If already open, before is the last normal
    non-looper BoardMode reply."""
    board = br.get(BOARD) or {}
    if board.get("string") == "Looper" or not show:
        br.looper_board = before if (before or {}).get("string") not in (None, "Looper") else None
        br.looper_shown = board.get("string") == "Looper"
    else:
        br.looper_board = board
        press(br, RC + "/SelectLooperMode")
        for _ in range(20):
            if (br.get(BOARD) or {}).get("string") == "Looper":
                break
            time.sleep(0.05)
        else:
            raise LiveError("Looper not opened on the device (BoardMode stays %s)" % (br.get(BOARD) or {}).get("string"))
        br.looper_shown = True
    return looper_parse(br.get_many(LOOPER_PATHS))


def looper_close(br):
    """Exit device looper if this editor opened it and it is still visible,
    restoring the previous view. Loop playback continues."""
    if getattr(br, "looper_shown", False) and (br.get(BOARD) or {}).get("string") == "Looper":
        press(br, RC + "/ExitLooper")
        restore_board(br, getattr(br, "looper_board", None))
    br.looper_board = None
    br.looper_shown = False
    return True


def looper_action(br, name):
    """Press a LOOPER_ACTIONS button and return the new reading."""
    press(br, "%s/%s" % (LOOPER, LOOPER_ACTIONS[name]), hold=0.06, wait=0.03)
    return looper_parse(br.get_many(LOOPER_PATHS))


def looper_seconds(text):
    """LengthText mm:ss.z -> seconds, or None if unreadable."""
    try:
        m, sec = (text or "").split(":")
        return int(m) * 60 + float(sec)
    except ValueError:
        return None


def looper_parse(res):
    """LOOPER_PATHS get_many reply -> state, text and settings dictionary."""
    def g(k, field="string"):
        return (res.get(k if k.startswith("/") else LOOPER + "/" + k) or {}).get(field)
    pos = g("RelativPos", "unnormalized")
    mix, fb = g(LOOPER_MIX, "unnormalized"), g(LOOPER_FEEDBACK, "unnormalized")
    level = g(LOOPER_LEVEL, "unnormalized")
    try:
        layers = int(g("NumLayers") or 0)
    except ValueError:
        layers = 0
    return {"start": g("StartState") or "Empty", "rec": g("RecState") or "RecReady",
            "state": g("StateText") or "", "record_text": g("RecordText") or "RECORD",
            "play_text": g("PlayText") or "START", "undo_text": g("UndoText") or "PEEL",
            "length": g("LengthText") or "", "pos": float(pos) if pos is not None else 0.0, "layers": layers,
            "speed": g("SpeedText") or "x 1", "mult": g("MultText") or "x 1",
            "reverse": g("Reverse") == "Reverse", "modified": g("Modified") == "Y",
            "remaining": g("RemainingRecordingTimeString") or "",
            "mix": float(mix) if mix is not None else None, "feedback": float(fb) if fb is not None else None,
            "loc": g(LOOPER_LOC), "alt": g(LOOPER_ALT_FS) == "Alt",
            "board": g(BOARD), "level": float(level) if level is not None else None,
            "seconds": looper_seconds(g("LengthText")), "ok": bool(res.get("ok", True)) and LOOPER + "/StartState" in res}


# ---- Hardware assignments (footswitches, scenes, expression pedals) ----
#
# MX5 switches 1-3 map to /Engine/FootSwitch/...5,6,7. Per switch:
#   ModeNew Toggle|Scene, UserFootSwitchText label; Toggle uses
#   ModuleList/OperationList choices, setting Module/Operation;
#   Scene uses SceneNumberOfStates 1-2, MacroColour/State2MacroColour,
#   Scene/State2Scene slot(0..10)={effect, mode}; 0 unchanged,
#   1 on, 2 off. Set only occupied chain slots: assigning a mode to
#   an empty slot crashed the device when triggered.
#   Each slot also has Scene<n>Slot<k>Preset (second state:
#   State2Scene<n>Slot<k>Preset): block preset name from the blocks table
#   for that block type, loaded when the scene triggers.
#   'No Preset' means none; empty slots return ''. The device does not
#   validate names (2026-09-17: WARM loaded by footswitch,
#   updating the block's PresetName and parameters).
# Pedal1 internal/Pedal2 external: four ModuleList/ParamList assignments
#   setting Module/Param and percentage Min/Max; Pedal1 has PedalMode Classic|Advanced.
# Assignment changes set Rigs/Dirty on the device.
# Switch display (verified 2026-09-17): FootSwitchText<n>
#   is label or block name; FootSwitchColour<n> is displayed color
#   and follows active state in two-state scenes (SceneState<n>=0/1); FootSwitch<n> state
#   indicates lit status (last scene triggered or block on). Press in Stomp via
#   RedirCtrl/RawFootswitches/FS1-3 (press_footswitch). Pedalboard: FootSwitch1-8 -> FS2-5/FS8-11
#   according to the device profile (FS_NUMBERS/DEV.raw).

HW_FS, HW_PEDAL = "Footswitches", "Pedals"
HW_MODULES = [HW_FS, HW_PEDAL]
FSW = "/Engine/FootSwitch"
PEDAL = "/Engine/Pedal%d"
SCENE_UNCHANGED, SCENE_ON, SCENE_OFF = 0, 1, 2
NO_SCENE_PRESET = "No Preset"
FS_DISPLAY = ("FootSwitchText%d", "FootSwitchColour%d", "FootSwitch%d")


def fs_display_paths():
    """Footswitch display properties: text, color, lit state (MX5: 3 x 3)."""
    return ["%s/%s" % (FSW, k % n) for n in FS_NUMBERS for k in FS_DISPLAY]


def fs_display(values):
    """Read switch values -> {n: {text, colour, on}}, or None until read."""
    out = {}
    for n in FS_NUMBERS:
        t, c, o = ((values.get("%s/%s" % (FSW, k % n)) or {}) for k in FS_DISPLAY)
        if not t and not c and not o:
            continue
        out[n] = {"text": t.get("string") or "", "colour": c.get("string") or "", "on": bool(o.get("state"))}
    return out or None


def press_footswitch(br, n):
    """Press FootSwitch<n> (MX5 5-7) like a physical press via DEV.raw.
    Stomp view only, to avoid accidentally pressing a dialog button.
    Return the new display state."""
    board = (br.get(BOARD) or {}).get("string")
    if board != "Stomp":
        raise LiveError("The device currently shows '%s', not the stomp view." % board)
    press(br, FS % DEV.raw[n], hold=0.06, wait=0.2)
    return br.get_many(fs_display_paths())


def scene_path(n, state):
    return "%s/%sScene%d" % (FSW, "State2" if state == 2 else "", n)


def scene_preset_path(n, state, slot):
    """Preset property for a scene's zero-based chain slot."""
    return "%s/%sScene%dSlot%dPreset" % (FSW, "State2" if state == 2 else "", n, slot + 1)


def read_scene_slots(br, n, state):
    """[(blockname, mode, preset)] for 11 scene slots; empty preset means none."""
    js = ('var t = App.getProperty("%s").translator; var o = []; for (var i = 0; i < %d; i++) '
          '{ var s = t.slot(i); o.push([s.effect, s.mode]); } return JSON.stringify(o);' % (scene_path(n, state), SLOTS))
    slots = [(e, int(m)) for e, m in json.loads(br.eval(js))]
    paths = [scene_preset_path(n, state, i) for i in range(SLOTS)]
    r = br.get_many(paths)
    out = []
    for i, (e, m) in enumerate(slots):
        name = (r.get(paths[i]) or {}).get("string") or ""
        out.append((e, m, "" if name == NO_SCENE_PRESET else name))
    return out


def set_scene_slot(br, n, state, slot, mode):
    """Set a zero-based chain slot's mode; reject empty slots."""
    js = ('var s = App.getProperty("%s").translator.slot(%d); if (s.effect === "%s") return "leer"; '
          's.mode = %d; return String(s.mode);' % (scene_path(n, state), slot, EMPTY, mode))
    r = br.eval(js)
    if r == "leer":
        raise LiveError("Slot %d is empty." % (slot + 1))
    return int(r)


def set_scene_preset(br, n, state, slot, name):
    """Set the preset loaded by a scene at zero-based slot. Empty name means none.
    Return the stored name, empty for none."""
    r = br.set(scene_preset_path(n, state, slot), "string", name or NO_SCENE_PRESET)
    if not r.get("ok"):
        raise LiveError("Preset not set: %s" % r.get("err"))
    got = r.get("string") or ""
    return "" if got == NO_SCENE_PRESET else got


def _entries(br, path):
    try:
        return br.entries(path)
    except Exception:
        return []


FS_KEYS = {"mode": "ModeNew%d", "text": "UserFootSwitchText%d", "module": "ModuleList%d",
           "operation": "OperationList%d", "states": "SceneNumberOfStates%d",
           "colour1": "MacroColour%d", "colour2": "State2MacroColour%d"}
FS_LISTS = {"modes": "ModeNew%d", "modules": "ModuleList%d", "operations": "OperationList%d",
            "colours": "MacroColour%d"}
PEDAL_KEYS = {"module": "ModuleList%d", "param": "ParamList%d", "min": "Min%d", "max": "Max%d"}
PEDAL_LISTS = {"modules": "ModuleList%d", "params": "ParamList%d"}

# Read all hardware in one eval (previously ~55 requests, 250 ms). V: infoOf paths;
# E: entriesOf paths (missing list -> []); S: scene paths (slots 0..10);
# P: scene preset paths (text only).
_HW_JS = ('var V = %s, E = %s, S = %s, P = %s, o = {v: {}, e: {}, s: {}, p: {}}; '
          'for (var i = 0; i < V.length; i++) o.v[V[i]] = device.infoOf(V[i]); '
          'for (var i = 0; i < E.length; i++) { try { o.e[E[i]] = device.entriesOf(E[i]); } catch (x) { o.e[E[i]] = []; } } '
          'for (var i = 0; i < S.length; i++) { var t = device.translatorOf(S[i]), l = []; '
          '  for (var k = 0; k < %d; k++) { var x = t.slot(k); l.push([x.effect, x.mode]); } o.s[S[i]] = l; } '
          'for (var i = 0; i < P.length; i++) { try { o.p[P[i]] = device.translatorOf(P[i]).string; } catch (x) { } } '
          'return JSON.stringify(o);')


def read_hardware(br):
    """Read all active-rig hardware assignments (values, choices, scenes)
    in one eval instead of ~55 requests."""
    vals, lists, scenes = [], [], []
    for n in FS_NUMBERS:
        vals += ["%s/%s" % (FSW, k % n) for k in FS_KEYS.values()]
        lists += ["%s/%s" % (FSW, k % n) for k in FS_LISTS.values()]
        scenes += [scene_path(n, 1), scene_path(n, 2)]
    for p in (1, 2):
        base = PEDAL % p
        for i in (1, 2, 3, 4):
            vals += ["%s/%s" % (base, k % i) for k in PEDAL_KEYS.values()]
            lists += ["%s/%s" % (base, k % i) for k in PEDAL_LISTS.values()]
        vals.append(base + "/PedalMode")
        lists.append(base + "/PedalMode")
    presets = [scene_preset_path(n, st, i) for n in FS_NUMBERS for st in (1, 2) for i in range(SLOTS)]
    r = json.loads(br.eval(_HW_JS % (json.dumps(vals), json.dumps(lists), json.dumps(scenes), json.dumps(presets), SLOTS)))
    v, e, s, pr = r["v"], r["e"], r["s"], r["p"]

    def scene(n, st):
        out = []
        for i, (eff, mode) in enumerate(s[scene_path(n, st)]):
            name = pr.get(scene_preset_path(n, st, i)) or ""
            out.append((eff, int(mode), "" if name == NO_SCENE_PRESET else name))
        return out

    hw = {"fs": {}, "pedal": {}}
    for n in FS_NUMBERS:
        d = {name: v.get("%s/%s" % (FSW, k % n), {}) for name, k in FS_KEYS.items()}
        d.update({name: e.get("%s/%s" % (FSW, k % n), []) for name, k in FS_LISTS.items()})
        d["scenes"] = [scene(n, 1), scene(n, 2)]
        hw["fs"][n] = d
    for p in (1, 2):
        base = PEDAL % p
        rows = []
        for i in (1, 2, 3, 4):
            row = {name: v.get("%s/%s" % (base, k % i), {}) for name, k in PEDAL_KEYS.items()}
            row.update({name: e.get("%s/%s" % (base, k % i), []) for name, k in PEDAL_LISTS.items()})
            rows.append(row)
        mode = v.get(base + "/PedalMode", {})
        hw["pedal"][p] = {"rows": rows, "mode": mode if mode.get("ok") else None,
                          "pedal_modes": e.get(base + "/PedalMode", []) if mode.get("ok") else []}
    return hw


# ---- Move blocks (verified 2026-09-29) ----
#
# Original UI BlockMove.js only sets ModuleType<k>.index:
# an already-present type moves to the target, retaining parameters.
# An occupied target is replaced, but each block type is a fixed instance:
# reinsert it later and parameters/on/color/pedal assignments remain.
# Only scene entries (mode/preset per footswitch) are lost,
# so move_chain saves them first and restores by block name afterwards.
# Changing index does not set Rigs/Dirty; the UI sets it explicitly.

def plan_block_move(chain, src, dst):
    """Chain after dragging one-based src to dst: move directly if empty;
    otherwise shift from dst toward the next gap (toward src or the end).
    If no gap exists, swap both blocks, matching device behavior."""
    c = list(chain)
    i, j = src - 1, dst - 1
    m = c[i]
    if i == j or m is None:
        return c
    if c[j] is None:
        c[i], c[j] = None, m
        return c
    rng = range(j + 1, i) if i > j else range(j + 1, len(c))
    gap = next((k for k in rng if c[k] is None), None)
    if gap is None:
        c[i], c[j] = c[j], m
    else:
        c[j + 1:gap + 1] = c[j:gap]
        c[i], c[j] = None, m
    return c


def chain_writes(cur, desired):
    """Writes [(slot, name)] from cur to desired with the same blocks reordered.
    Prefer empty targets; only displace/reinsert a block if no empty slot exists."""
    work, out, writes = list(cur), set(), []
    assert sorted(m for m in cur if m) == sorted(m for m in desired if m), "andere Bloecke"
    while work != desired:
        progressed = False
        for k, name in enumerate(desired):
            if name and work[k] is None and (name in work or name in out):
                if name in work:
                    work[work.index(name)] = None
                out.discard(name)
                work[k] = name
                writes.append((k + 1, name))
                progressed = True
        if progressed:
            continue
        k0 = next(k for k in range(len(work)) if desired[k] and work[k] != desired[k])
        free = [k for k in range(len(work)) if work[k] is None]
        if free:   # Park block k0 in an empty slot
            work[free[0]], work[k0] = work[k0], None
            writes.append((free[0] + 1, work[free[0]]))
        else:      # Full chain: overwrite target, then reinsert the displaced block
            name = desired[k0]
            if name in work:
                work[work.index(name)] = None
            out.add(work[k0])
            work[k0] = name
            writes.append((k0 + 1, name))
    return writes


_JS_TR = ('function tr(p) { return App.getProperty(p).translator; } '
          'function sc(n, s) { return tr("%(F)s/" + (s == 2 ? "State2" : "") + "Scene" + n); } '
          'function pp(n, s, i) { return tr("%(F)s/" + (s == 2 ? "State2" : "") + "Scene" + n + "Slot" + (i + 1) + "Preset"); } '
          'function chain() { var o = []; for (var i = 1; i <= %(N)d; i++) o.push(tr("%(C)s" + i).string); return o; } '
          'var FS = %(FS)s; ')


def _move_js(body, **kw):
    kw.update(F=FSW, N=SLOTS, C=ENGINE + "/Chain/ModuleType", FS=json.dumps(list(FS_NUMBERS)))
    return (_JS_TR % kw) + body


def move_chain(br, desired, types):
    """Reorder the device chain to desired (block names, None=empty) without losing
    parameters/scenes. types is the ModuleType choice list.
    Return {'chain': [...], 'writes': n, 'fixed': restored_scene_entries}."""
    snap = json.loads(br.eval(_move_js(
        'var c = chain(), k = {}; '
        'for (var a = 0; a < FS.length; a++) for (var s = 1; s <= 2; s++) { var t = sc(FS[a], s); '
        '  for (var i = 0; i < c.length; i++) if (c[i] != "%s") { var x = t.slot(i); '
        '    k[FS[a] + "/" + s + "/" + c[i]] = [x.mode, pp(FS[a], s, i).string]; } } '
        'return JSON.stringify({chain: c, keep: k});' % EMPTY)))
    cur = [None if m == EMPTY else m for m in snap["chain"]]
    writes = chain_writes(cur, list(desired))
    if not writes:
        return {"chain": cur, "writes": 0, "fixed": 0}
    idx = [[k, types.index(name)] for k, name in writes]
    br.eval(_move_js('var W = %s; for (var i = 0; i < W.length; i++) tr("%s/Chain/ModuleType" + W[i][0]).index = W[i][1]; '
                     'tr("%s/Dirty").state = true; return "";' % (json.dumps(idx), ENGINE, RIGS)))
    time.sleep(0.1)   # The engine updates moved blocks' scene entries itself
    # Restore scene entries by block name: modes only on occupied slots;
    # empty slots get mode 0 because a mode on an empty slot may crash the app
    res = json.loads(br.eval(_move_js(
        'var K = %s, c = chain(), fixed = 0; '
        'for (var a = 0; a < FS.length; a++) for (var s = 1; s <= 2; s++) { var t = sc(FS[a], s); '
        '  for (var i = 0; i < c.length; i++) { var x = t.slot(i); '
        '    if (c[i] == "%s") { if (x.mode != 0) { x.mode = 0; fixed++; } continue; } '
        '    var w = K[FS[a] + "/" + s + "/" + c[i]] || [0, "%s"]; '
        '    if (x.mode != w[0]) { x.mode = w[0]; fixed++; } '
        '    var p = pp(FS[a], s, i), ps = p.string || "%s", ws = w[1] || "%s"; '
        '    if (ps != ws) { p.string = ws; fixed++; } } } '
        'return JSON.stringify({chain: c, fixed: fixed});'
        % (json.dumps(snap["keep"]), EMPTY, NO_SCENE_PRESET, NO_SCENE_PRESET, NO_SCENE_PRESET))))
    got = [None if m == EMPTY else m for m in res["chain"]]
    if got != list(desired):
        raise LiveError("Chain on the device: %s" % [g or "-" for g in got])
    return {"chain": got, "writes": len(writes), "fixed": res["fixed"]}


class LiveRig:
    """Cached view of the device's active rig."""

    def __init__(self, session, catalog):
        self.session = session
        self.catalog = catalog
        self.values = {}          # Full path -> latest reply
        self.slots = [None] * SLOTS
        self.name = "?"
        self.rig_id = None
        self.routing = "?"
        self.dirty = False
        self.marking_dirty = False   # Rigs/Dirty write in progress
        self.loaded_modules = set()
        self.setlists = []        # Device setlist names excluding ALL_RIGS
        self.setlist = None       # Active setlist or ALL_RIGS
        self.setlist_sure = False # False means inferred/uncertain; device does not report active setlist
        self.setlist_ids = None   # Setlist name -> database ID
        self.rows = []            # Active rig-list database rows in device order
        self.block_types = None   # ModuleType choice list, read once
        self.presets = None       # Block presets {TYPE: [{id,type,name,readonly}]} from db_preset_names; None until read
        self.hw = None            # Hardware assignments from read_hardware; None until read
        self.hw_loading = False
        self.nam = None           # NAM status from nam_status; None until read
        self.nam_models = None    # Device-ordered NAM models from db_nam_models()['models']; None until read

    def apply_rig_list(self, res):
        """Apply read_rig_list/enter_setlist/refresh_lists result."""
        self.setlists = res["setlists"]
        self.setlist = res["setlist"]
        self.setlist_sure = res["sure"]
        self.setlist_ids = res["setlist_ids"]
        self.rows = res["rows"]

    def setlist_choices(self):
        return [ALL_RIGS] + self.setlists

    # ---- Paths ----
    @staticmethod
    def path(module, param):
        return "%s/%s/%s" % (ENGINE, module, param)

    @staticmethod
    def slot_path(slot):
        return "%s/Chain/ModuleType%d" % (ENGINE, slot)

    # ---- Header data ----
    def overview_paths(self):
        paths = ["%s/Rig/PresetName" % ENGINE, "%s/Chain/Routing" % ENGINE, "%s/Dirty" % RIGS, "%s/LoadedID" % RIGS]
        paths += [self.slot_path(i) for i in range(1, SLOTS + 1)]
        return paths

    def apply_overview(self, path, info):
        """Return True if rig or chain changed."""
        if not info.get("ok"):
            return False
        s = info.get("string", "")
        if path.endswith("/Rig/PresetName"):
            changed = s != self.name
            self.name = s
            return changed
        if path.endswith("/Rigs/LoadedID"):
            changed = self.rig_id is not None and s != self.rig_id
            self.rig_id = s
            return changed
        if path.endswith("/Rigs/Dirty"):
            self.dirty = bool(info.get("state"))
            return False
        if path.endswith("/Chain/Routing"):
            self.routing = s
            return False
        n = int(path.rsplit("ModuleType", 1)[1])
        new = None if s == EMPTY else s
        changed = self.slots[n - 1] != new
        self.slots[n - 1] = new
        return changed

    def chain(self):
        return [(i + 1, m) for i, m in enumerate(self.slots)]

    def modules(self):
        """Chain blocks plus Rig/Input/Output, excluding hardware pages."""
        return [m for m in self.slots if m] + SPECIAL_MODULES

    def has_module(self, module):
        return module in self.slots or module in SPECIAL_MODULES or module in HW_MODULES

    # ---- Rig list ----
    def rigs(self):
        """[(index, display_name, color_name, ID)] for the active list."""
        return [(i, display_name(r["name"]), RIG_COLOURS[int(r.get("color") or 0)]
                 if 0 <= int(r.get("color") or 0) < len(RIG_COLOURS) else "Off", r["id"])
                for i, r in enumerate(self.rows)]

    def row(self, rig_id):
        """First matching rig row in the active list, or None."""
        return next((r for r in self.rows if r["id"] == rig_id), None)

    def prog_of(self, rig_id):
        r = self.row(rig_id)
        return _prog(r) if r else None

    # ---- Parameters ----
    def params(self, module):
        """Catalog parameter names in device order where known."""
        entry = self.catalog.modules.get(module) or self.catalog.modules.get(self.catalog.base(module)) or {}
        order = list(entry.get("order", []))
        order += sorted(k for k in entry.get("params", {}) if k not in order)
        if module in SPECIAL_MODULES or entry:
            return [p for p in order if p not in HIDDEN_PARAMS]
        return ["On", "Colour"]  # Unknown block type: provide at least the basic controls

    def info(self, module, param):
        return self.values.get(self.path(module, param))

    def store(self, path, info):
        if info.get("ok"):
            self.values[path] = info

    def forget_module(self, module):
        prefix = "%s/%s/" % (ENGINE, module)
        for k in [k for k in self.values if k.startswith(prefix)]:
            del self.values[k]
        self.loaded_modules.discard(module)

    def is_on(self, module):
        i = self.info(module, "On")
        return None if i is None else bool(i.get("state"))

    def fs_display(self):
        """Three footswitches' fs_display state, or None until read."""
        return fs_display(self.values)

    def colour(self, module):
        i = self.info(module, "Colour")
        return None if i is None else i.get("string")
