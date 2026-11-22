#!/usr/bin/env python3
"""regressionstest.py: live regression tests against a real MX5.

Run from editor folder: python werkzeuge/regressionstest.py
                       python werkzeuge/regressionstest.py --quick

Without prompts, test connect -> DB rig list -> create/load own test rig ->
parameter writes/reads -> discard -> save -> DB color/refresh/save-as/delete ->
hardware assignments and scene presets -> scenes/switch presses -> block moves ->
tuner -> looper -> meters -> compact poll -> export -> EVAL -> Unicode ->
DB setlists -> import/restore -> IR selection -> file operations -> NAM (0.7
with NAM, otherwise skip) -> LiveSession connection loss -> app restart -> cleanup.
Full run ~60 s; --quick ~30 s, skipping save/save-as/All Rigs/restart.
Restart briefly prompts restore last state; the test answers No automatically.
Do not press device controls.

Since 2026-10-04, no existing rig/setlist is required. Create ZZ MXB REGTEST
from adjacent regtest_rig.rig (chain gap, IR, scenes) and temporarily insert
first in the active setlist, or append in All Rigs. A dedicated setlist would
require two app restarts because names load only at startup. Only the test rig
is modified/saved; others are only loaded. Always restore previous rig/view
and delete test rigs and cascading setlist entries, even after failure.
Next run removes leftovers from interrupted runs.

Requires USB MX5 with bridge 0.5, no device dialog or unsaved edits (abort rather
than discard them), and no live editor competing for the MIDI port.
Console output also appends to regressionstest_log.txt in current directory.
Exit 0 means all passed; 1 means at least one failure. Legacy --schnell is supported.
"""
import json, os, sys, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # Editor folder containing bridge.py/live.py/rigmodel.py
from bridge import Bridge, BridgeError
import live as L
from rigmodel import Catalog

TEST_RIG = "ZZ MXB REGTEST"        # Created for the test, deleted at end
RIG_FILE = os.path.join(HERE, "regtest_rig.rig")
LOG = "regressionstest_log.txt"


class Test:
    def __init__(self, quick=False):
        self.quick = quick
        self.br = None
        self.ok = self.bad = 0
        self.failed = []
        self.res = {}          # Intermediate results for later steps
        self.log = open(LOG, "a", encoding="utf-8")
        self.say("##### Regression test %s #####" % time.strftime("%Y-%m-%d %H:%M:%S"))

    def say(self, txt):
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(txt.encode(enc, "replace").decode(enc), flush=True)   # Console cannot print emoji
        self.log.write(txt + "\n")
        self.log.flush()

    def step(self, name, fn):
        """Run one step; count exceptions as failures and continue."""
        t = time.time()
        try:
            detail = fn()
            self.ok += 1
            self.say("  OK      %-34s %5.1f s  %s" % (name, time.time() - t, detail or ""))
        except Exception as e:
            self.bad += 1
            self.failed.append(name)
            self.say("  FAIL    %-34s %5.1f s  %s: %s" % (name, time.time() - t, type(e).__name__, e))
            if not isinstance(e, (AssertionError, L.LiveError, BridgeError)):
                self.say("          " + traceback.format_exc().strip().replace("\n", "\n          "))

    def need(self, key):
        if key not in self.res:
            raise AssertionError("Missing prerequisite (step '%s' did not pass)" % key)
        return self.res[key]

    # ---------------- Steps ----------------
    def t_verbinden(self):
        self.br = Bridge.auto()
        v = self.br.ping()
        assert self.br.vnum >= (0, 5), "Bridge 0.5 required; found: %s" % v
        b = L.dialog_buttons(self.br)
        assert not b, "MX5 dialog open (%s); please answer on the device" % " | ".join(b)
        self.res["version"] = v
        return v

    def t_rigliste(self):
        res = L.read_rig_list(self.br)
        assert res["sure"], "active setlist is only inferred"
        n = len(res["rows"])
        assert n > 0, "no rigs"
        self.res["liste"] = res
        return "%d Rigs in '%s', Setlists %s" % (n, res["setlist"], res["setlists"])

    # ---- Dedicated test rig ----
    @property
    def set(self):
        """Test list: initially active setlist or All Rigs."""
        return self.need("start")["setlist"]

    def _set_id(self):
        return self.need("liste")["setlist_ids"].get(self.set) if self.set != L.ALL_RIGS else None

    def _refresh(self, reload_id=None):
        res = self.need("liste")
        res2 = L.refresh_lists(self.br, res["setlists"], self.set, res["setlist_ids"], reload_id=reload_id)
        self.res["liste"] = dict(res, rows=res2["rows"], status=res2["status"])
        return res2

    def _leftovers(self):
        return self.br.sql("select id, name from rigs where name like 'ZZ MXB REGTEST%'")

    def t_vorbereiten(self):
        """Remember loaded rig/view, remove old leftovers, create regtest_rig.rig,
        insert first in active setlist, then refresh."""
        res = self.need("liste")
        st = L.rig_status(self.br)
        assert not st["dirty"], ("Loaded rig '%s' has unsaved edits; please use the device to "
                                 "save or discard first; the test would otherwise discard them" % st["name"])
        left = self._leftovers()
        if any(r["id"] == st["id"] for r in left):    # Interrupted run left its test rig loaded
            other = next((i for i, r in enumerate(res["rows"]) if not r["name"].startswith(TEST_RIG)), None)
            assert other is not None, "only test rigs in active list"
            st = L.load_rig(self.br, res["rows"], other, discard=True)
        self.res["start"] = {"setlist": res["setlist"], "rig": st["id"], "name": st["name"],
                             "board": self.br.get(L.BOARD)}
        for r in left:
            L.delete_rig(self.br, r["id"])
        d = json.load(open(RIG_FILE, encoding="utf-8"))
        rid = L.create_rig(self.br, TEST_RIG, content=d["content"], color=d.get("color", 9))
        self.res["rig_id"] = rid
        sid = self._set_id()
        if sid:   # Before other entries without changing their show_order
            self.br.sql_write(
                "insert into setlist_rigs (id, setlist_id, rig_id, show_order) select %s, %s, %s, "
                "coalesce(min(show_order), 0) - 1 from setlist_rigs where setlist_id = %s"
                % (L.sql_lit(L.new_id()), L.sql_lit(sid), L.sql_lit(rid), L.sql_lit(sid)))
        res2 = self._refresh()
        ids = [r["id"] for r in res2["rows"]]
        assert rid in ids, "Test rig missing from list '%s'" % self.set
        self.res["index"] = ids.index(rid)
        assert not sid or self.res["index"] == 0, "Test rig at position %d instead of 1" % (self.res["index"] + 1)
        assert res2["status"]["id"] == st["id"], "Refresh loaded another rig"
        return "%s created at position %d in '%s'%s; original rig %s" % (
            TEST_RIG, self.res["index"] + 1, self.set, ", %d leftover(s) deleted" % len(left) if left else "", st["name"])

    def t_anderes_rig(self):
        """Load another rig first so loading the test rig actually changes state."""
        res = self.need("liste")
        if L.rig_status(self.br)["id"] != self.need("rig_id"):
            return "another rig already loaded (%s)" % L.rig_status(self.br)["name"]
        other = next((i for i, r in enumerate(res["rows"]) if r["id"] != self.need("rig_id")), None)
        assert other is not None, "no second rig in list"
        st = L.load_rig(self.br, res["rows"], other, discard=True)
        assert st["id"] == res["rows"][other]["id"], "LoadedID %s instead of %s" % (st["id"], res["rows"][other]["id"])
        return "loaded: %s (%s, %.1f s)" % (st["name"], L.LAST_LOAD.get("via"), L.LAST_LOAD.get("secs", 0))

    def t_laden(self):
        res, idx = self.need("liste"), self.need("index")
        st = L.load_rig(self.br, res["rows"], idx, discard=True)
        assert st["name"] == TEST_RIG, "loaded '%s'" % st["name"]
        assert not st["dirty"], "Rig marked dirty after loading"
        assert st["id"] == self.need("rig_id"), "LoadedID %s does not match database %s" % (st["id"], self.res["rig_id"])
        assert L.LAST_LOAD.get("via") == "jump", "not loaded by jump: %s" % L.LAST_LOAD
        return "LoadedID %s, jumped in %.1f s" % (st["id"][:8], L.LAST_LOAD["secs"])

    def t_kette(self):
        lr = L.LiveRig(None, Catalog(os.path.join(os.path.dirname(HERE), "catalog.json")))
        r = self.br.get_many(lr.overview_paths())
        for p, info in r.items():
            lr.apply_overview(p, info)
        assert lr.name == TEST_RIG, "PresetName '%s'" % lr.name
        blocks = [m for m in lr.slots if m]
        assert blocks, "empty chain"
        self.res["block"] = blocks[0]
        self.res["params"] = lr.params(blocks[0])
        return "%d blocks, routing %s, first block '%s' (%d catalog parameters)" % (
            len(blocks), lr.routing, blocks[0], len(self.res["params"]))

    def _on_path(self):
        return L.LiveRig.path(self.need("block"), "On")

    def _on(self):
        info = self.br.get(self._on_path())
        assert info.get("ok"), info
        return bool(info.get("state"))

    def t_schreiben(self):
        was = self._on()
        self.res["on_orig"] = was
        r = self.br.set(self._on_path(), "state", int(not was))
        assert r.get("ok"), r
        # Immediately after switching rigs, the device drops the first set if
        # live.SETTLE is too short; this tests exactly one write.
        t = time.time()
        while self._on() != (not was):
            assert time.time() - t < 1.0, "Value not applied after 1 s (set reply: %s)" % r.get("string")
            time.sleep(0.05)
        delay = time.time() - t
        st = L.rig_status(self.br)
        assert not st["dirty"], "Bridge parameter write sets Dirty (new behavior?)"
        self.br.set(L.RIGS + "/Dirty", "state", 1)
        assert L.rig_status(self.br)["dirty"], "Cannot set Rigs/Dirty"
        return "'%s' On %s -> %s%s, Dirty set" % (self.res["block"], was, not was,
                                                     " (after %.0f ms)" % (delay * 1000) if delay > 0.02 else "")

    def t_zahl(self):
        """Read a numeric parameter in the first block; write via value and unnormalized."""
        block = self.need("block")
        for p in self.need("params"):
            info = self.br.get(L.LiveRig.path(block, p))
            if L.kind_of(info) == L.L_NUMBER and "unnormalized" in info:
                path = L.LiveRig.path(block, p)
                orig_v, orig_u = info["value"], info["unnormalized"]
                self.br.set(path, "value", 0.25)
                v = self.br.get(path)
                assert abs(v["value"] - 0.25) < 0.02, "value 0.25 -> %s" % v["value"]
                self.br.set(path, "unnormalized", orig_u)
                v = self.br.get(path)
                assert abs(v["value"] - orig_v) < 0.02, "unnormalized %s -> value %s instead of %s" % (orig_u, v["value"], orig_v)
                return "%s/%s: value %s, unnormalized %s %s" % (block, p, orig_v, orig_u, info.get("string", ""))
        return "no numeric parameter in first block; skipped"

    def t_verwerfen(self):
        res = self.need("liste")
        st = L.reload_rig(self.br, res["rows"])
        assert st["name"] == TEST_RIG, "after reload: %s" % st["name"]
        assert not st["dirty"], "still dirty"
        assert self._on() == self.need("on_orig"), "Value not restored after discard"
        return "Rig reloaded, On restored to %s" % self.res["on_orig"]

    def t_speichern(self):
        if self.quick:
            return "skipped (--quick)"
        orig, rig_id = self.need("on_orig"), self.need("rig_id")
        before = L.db_rig(self.br, rig_id)["content"]
        self.br.set(self._on_path(), "state", int(not orig))
        self.br.set(L.RIGS + "/Dirty", "state", 1)
        st = L.save_rig(self.br)
        assert not st["dirty"], "still dirty after save (dialog open? %s)" % L.dialog_buttons(self.br)
        assert L.db_rig(self.br, rig_id)["content"] != before, "Database content unchanged after save"
        # Restore original state and save again
        self.br.set(self._on_path(), "state", int(orig))
        self.br.set(L.RIGS + "/Dirty", "state", 1)
        st = L.save_rig(self.br)
        assert not st["dirty"] and self._on() == orig, "Reset was not saved"
        after = L.db_rig(self.br, rig_id)["content"]
        same = after == before
        return "saved twice, On restored to %s, database content %s" % (
            orig, "same as before" if same else "different from before (%d/%d characters)" % (len(after), len(before)))

    def t_db_farbe(self):
        """Change DB color and refresh active setlist: bank shows new color,
        current rig stays loaded and unchanged."""
        rig_id, idx = self.need("rig_id"), self.need("index")
        st0 = L.rig_status(self.br)
        colour0 = self.br.sql("select color from rigs where id = %s" % L.sql_lit(rig_id))[0]["color"]
        new = 5 if colour0 != 5 else 3
        L.set_rig_color(self.br, rig_id, new)
        try:
            t = time.time()
            res2 = self._refresh(reload_id=rig_id)
            dauer = time.time() - t
            assert res2["status"] == st0, "Refresh changed the loaded rig: %s" % (res2["status"],)
            L.goto_bank(self.br, res2["rows"], idx // 3)
            shown = L.read_bank(self.br)[idx % 3]
            assert shown == (TEST_RIG, L.RIG_COLOURS[new]), "Bank displays %s" % (shown,)
        finally:
            L.set_rig_color(self.br, rig_id, colour0)
            res2 = self._refresh(reload_id=rig_id)
        L.goto_bank(self.br, res2["rows"], idx // 3)
        assert L.read_bank(self.br)[idx % 3] == (TEST_RIG, L.RIG_COLOURS[colour0]), "Color not reset"
        return "Color %s in bank display, refresh %.1f s, rig stayed loaded" % (L.RIG_COLOURS[new], dauer)

    def t_db_kopie(self):
        """save_rig_as: create a live-state copy, load, then delete it."""
        if self.quick:
            return "skipped (--quick)"
        res, rig_id, orig = self.need("liste"), self.need("rig_id"), self.need("on_orig")
        name = TEST_RIG + " KOPIE"
        assert not L.db_name_taken(self.br, "rigs", name), "Previous leftover: delete %s" % name
        before = L.db_rig(self.br, rig_id)["content"]
        self.br.set(self._on_path(), "state", int(not orig))
        self.br.set(L.RIGS + "/Dirty", "state", 1)
        new_id = L.save_rig_as(self.br, name, setlist_id=self._set_id())
        try:
            assert L.db_rig(self.br, rig_id)["content"] == before, "Original row changed"
            copy = json.loads(L.db_rig(self.br, new_id)["content"])["data"]["Patch"]["children"]
            assert copy["Rig"]["children"]["PresetName"]["string"] == name, "copy's inner name"
            assert copy[self.need("block")]["children"]["On"]["state"] == (not orig), "Copy missing live state"
            res2 = self._refresh(reload_id=new_id)
            assert res2["status"]["id"] == new_id and not res2["status"]["dirty"], res2["status"]
            assert self._on() == (not orig), "Loaded copy lacks live state"
            st = L.load_rig(self.br, res2["rows"], [r["id"] for r in res2["rows"]].index(rig_id))
            assert st["id"] == rig_id and not st["dirty"] and self._on() == orig, "Original not restored cleanly"
        finally:
            if L.rig_status(self.br)["id"] == new_id:
                L.load_rig(self.br, res["rows"], self.need("index"), discard=True)
            L.delete_rig(self.br, new_id)
            res2 = self._refresh(reload_id=rig_id)
        assert new_id not in [r["id"] for r in res2["rows"]], "Copy not deleted"
        assert res2["status"]["id"] == rig_id and not res2["status"]["dirty"]
        return "Copy %s created, loaded, deleted; original unchanged" % name

    def t_hardware(self):
        hw = L.read_hardware(self.br)
        for n in L.FS_NUMBERS:
            d = hw["fs"][n]
            assert d["mode"].get("ok"), "Cannot read FootSwitch %d ModeNew" % n
            assert d["modes"], "FootSwitch %d has no mode list" % n
            assert len(d["scenes"]) == 2 and all(len(s) == L.SLOTS for s in d["scenes"]), "Scene slots"
        for p in (1, 2):
            assert len(hw["pedal"][p]["rows"]) == 4
        modes = [hw["fs"][n]["mode"].get("string") for n in L.FS_NUMBERS]
        # Scene preset: assign a matching block preset to an occupied scene slot,
        # read back, then remove; does not set Dirty
        detail = ""
        for n in L.FS_NUMBERS:
            d = hw["fs"][n]
            if d["mode"].get("string") != "Scene":
                continue
            slot = next((i for i, (eff, _, _) in enumerate(d["scenes"][0]) if eff != L.EMPTY), None)
            if slot is None:
                continue
            eff, mode, before = d["scenes"][0][slot]
            names = [r["name"] for r in L.db_presets_of(self.br, eff)]
            if not names:
                continue
            pick = next((x for x in names if x != before), names[0])
            got = L.set_scene_preset(self.br, n, 1, slot, pick)
            assert got == pick, "Scene preset: %r instead of %r" % (got, pick)
            assert L.read_scene_slots(self.br, n, 1)[slot] == (eff, mode, pick), "Scene preset read-back failed"
            assert L.set_scene_preset(self.br, n, 1, slot, before) == before
            assert L.read_scene_slots(self.br, n, 1)[slot] == (eff, mode, before)
            assert not L.rig_status(self.br)["dirty"], "Scene preset set Dirty"
            detail = ", scene preset %s on %s (FS%d) round trip" % (pick, eff, n - 4)
            break
        return "FS1-3 %s, Pedal1 %s%s" % (modes, (hw["pedal"][1]["mode"] or {}).get("string"), detail)

    # ---- Helpers for steps added in 0.9 ----
    def _chain(self):
        lr = L.LiveRig(None, None)
        for p, info in self.br.get_many(lr.overview_paths()).items():
            lr.apply_overview(p, info)
        return list(lr.slots)

    def _scene_map(self):
        """{(switch,state,block):(mode,preset)} for every occupied scene slot."""
        out = {}
        for n in L.FS_NUMBERS:
            for st in (1, 2):
                for eff, mode, preset in L.read_scene_slots(self.br, n, st):
                    if eff != L.EMPTY:
                        out[(n, st, eff)] = (mode, preset)
        return out

    def _discard(self):
        """Reload test rig, discard edits and verify clean."""
        st = L.reload_rig(self.br, self.need("liste")["rows"])
        assert st["name"] == TEST_RIG and not st["dirty"], "Discard: %s dirty=%s" % (st["name"], st["dirty"])

    def t_scenes(self):
        """Set/reset occupied-slot scene mode, reject empty slot, press FS1 in Stomp,
        verify blocks follow the scene, then discard."""
        chain = self._chain()
        hw = L.read_hardware(self.br)
        n = next((k for k in L.FS_NUMBERS if hw["fs"][k]["mode"].get("string") == "Scene"), None)
        if n is None:
            return "skipped (no Scene switch in test rig)"
        scene = hw["fs"][n]["scenes"][0]
        slot = next(i for i, (eff, _, _) in enumerate(scene) if eff != L.EMPTY)
        eff, mode0, _ = scene[slot]
        new = L.SCENE_ON if mode0 != L.SCENE_ON else L.SCENE_OFF
        assert L.set_scene_slot(self.br, n, 1, slot, new) == new
        assert L.read_scene_slots(self.br, n, 1)[slot][1] == new, "Mode read-back failed"
        assert L.set_scene_slot(self.br, n, 1, slot, mode0) == mode0
        empty = next((i for i, m in enumerate(chain) if m is None), None)
        if empty is not None:
            try:
                L.set_scene_slot(self.br, n, 1, empty, L.SCENE_ON)
                raise AssertionError("Mode on empty slot %d was not rejected" % (empty + 1))
            except L.LiveError:
                pass
            assert L.read_scene_slots(self.br, n, 1)[empty][1] == 0, "empty slot has a mode"
        # Let the scene actually switch a block so pressing tests something;
        # the test rig will be discarded afterwards
        L.set_scene_slot(self.br, n, 1, slot, new)
        # Temporarily enter Stomp, required by press_footswitch, then restore
        board = self.br.get(L.BOARD)
        modes = L._entries(self.br, L.BOARD)
        want = {eff: m for eff, m, _ in L.read_scene_slots(self.br, n, 1) if eff != L.EMPTY and m}
        try:
            if board.get("string") != "Stomp":
                self.br.set(L.BOARD, "index", modes.index("Stomp"))
                time.sleep(0.3)
            disp = L.press_footswitch(self.br, n)
            time.sleep(0.3)
            on = {m: bool(self.br.get(L.LiveRig.path(m, "On")).get("state")) for m in want}
            wrong = [m for m, md in want.items() if on[m] != (md == L.SCENE_ON)]
            assert not wrong, "Blocks do not follow scene: %s" % wrong
            assert disp.get("%s/FootSwitch%d" % (L.FSW, n), {}).get("state"), "Switch is not lit"
        finally:
            if board.get("string") != "Stomp" and "index" in board:
                self.br.set(L.BOARD, "index", board["index"])
                time.sleep(0.3)
            self._discard()
        assert self.br.get(L.BOARD).get("string") == board.get("string"), "Board mode not restored"
        return "FS%d: mode %s on %s round trip, empty slot rejected, press switches %d blocks" % (
            n - 4, new, eff, len(want))

    def t_verschieben(self):
        """move_chain into gap/occupied slot, preserving parameters/scenes;
        move back, then discard."""
        orig = self._chain()
        types = L._entries(self.br, L.LiveRig.slot_path(1))
        blocks = [m for m in orig if m]
        if len(blocks) < 2 or None not in orig:
            return "skipped (chain needs two blocks and a gap)"
        scenes0 = self._scene_map()
        src = orig.index(blocks[1]) + 1
        gap = max(i for i, m in enumerate(orig) if m is None) + 1
        probe = L.LiveRig.path(blocks[1], "Colour")
        colour0 = self.br.get(probe).get("string")
        try:
            want = L.plan_block_move(orig, src, gap)                          # Into the gap
            r = L.move_chain(self.br, want, types)
            assert r["chain"] == want and self._chain() == want, "Chain after move 1: %s" % r["chain"]
            assert self.br.get(probe).get("string") == colour0, "Color lost during move"
            last = max(i for i, m in enumerate(want) if m) + 1
            want2 = L.plan_block_move(want, last, 1)                           # Onto occupied slot 1
            r2 = L.move_chain(self.br, want2, types)
            assert self._chain() == want2, "Chain after move 2: %s" % self._chain()
            assert self._scene_map() == scenes0, "Scene entries changed by block name"
            assert L.rig_status(self.br)["dirty"], "Moving does not set Dirty"
            L.move_chain(self.br, orig, types)
            assert self._chain() == orig and self._scene_map() == scenes0, "Return move"
        finally:
            self._discard()
        assert self._chain() == orig, "Chain after discard: %s" % self._chain()
        return "%s: slot %d -> %d, then slot %d -> 1 (%d writes), restored; scenes and color preserved" % (
            blocks[1], src, gap, last, r2["writes"])

    def t_tuner(self):
        """Open/close tuner from two views; restore each previous view
        rather than ExitTuner's default Hybrid."""
        board = self.br.get(L.BOARD)
        modes = L._entries(self.br, L.BOARD)
        starts = [board["string"], "Stomp" if board["string"] != "Stomp" else "Hybrid"]
        seen = []
        try:
            for start in starts:
                if self.br.get(L.BOARD).get("string") != start:
                    self.br.set(L.BOARD, "index", modes.index(start))
                    time.sleep(0.3)
                t = L.tuner_open(self.br)
                assert t["active"], "Tuner page not open"
                assert t["ref"] and L.TUNER_REF_MIN <= t["ref"] <= L.TUNER_REF_MAX, "Reference pitch %s" % t["ref"]
                t0 = time.time()
                for _ in range(20):
                    L.tuner_parse(self.br.get_many(L.TUNER_PATHS))
                ms = (time.time() - t0) / 20 * 1000
                L.tuner_close(self.br)
                now = self.br.get(L.BOARD).get("string")
                assert now == start, "after tuner %s instead of %s" % (now, start)
                seen.append(start)
        finally:
            if self.br.get(L.BOARD).get("string") == "Tuner":
                L.tuner_close(self.br)
            L.restore_board(self.br, board)
        assert self.br.get(L.BOARD).get("string") == board["string"]
        return "Tuner opened/closed from %s, views restored; reference %d Hz, reading %.0f ms" % (" and ".join(seen), t["ref"], ms)

    def t_looper(self):
        """Open looper, record silence, close loop, overdub second layer, peel,
        speed/reverse/clear and close. Skip if a user's loop already exists."""
        board = self.br.get(L.BOARD)
        first = L.looper_open(self.br, None, show=False)
        if first["start"] != "Empty" or first["rec"] == "Rec":
            return "skipped: device has an active loop (%s)" % first["state"]
        try:
            st = L.looper_open(self.br, None)
            assert st["board"] == "Looper", "Looper view not open (%s)" % st["board"]
            st = L.looper_action(self.br, "record")
            assert st["rec"] == "Rec" and st["start"] == "Empty", "Recording: %s/%s" % (st["start"], st["rec"])
            time.sleep(1.0)
            st = L.looper_action(self.br, "record")
            assert st["start"] == "Playing" and st["rec"] == "OverReady", "Close loop: %s/%s" % (st["start"], st["rec"])
            length = st["length"]
            L.looper_action(self.br, "record")
            time.sleep(1.6)   # Overdub counts as a layer only after crossing loop start
            st = L.looper_action(self.br, "record")
            for _ in range(10):   # Layer count sometimes updates a moment later
                if st["layers"] == 2:
                    break
                time.sleep(0.1)
                st = L.looper_parse(self.br.get_many(L.LOOPER_PATHS))
            assert st["layers"] == 2, "Overdub: %d layers" % st["layers"]
            st = L.looper_action(self.br, "undo")
            assert st["layers"] == 1 and st["start"] == "Playing", "Peel: %d layers, %s" % (st["layers"], st["start"])
            st = L.looper_action(self.br, "double_speed")
            assert st["speed"] == "x 2", "Speed %s" % st["speed"]
            st = L.looper_action(self.br, "reverse")
            assert st["reverse"], "not reversed"
            st = L.looper_action(self.br, "reverse")
            pos = [L.looper_parse(self.br.get_many(L.LOOPER_PATHS))["pos"] for _ in range(3) if not time.sleep(0.1)]
            st = L.looper_action(self.br, "play")
            assert st["start"] == "Stopped", "Stop: %s" % st["start"]
        finally:
            st = L.looper_parse(self.br.get_many(L.LOOPER_PATHS))
            if st["start"] != "Empty" or st["rec"] == "Rec":
                st = L.looper_action(self.br, "clear")
            L.looper_close(self.br)
            L.restore_board(self.br, board)
        assert st["start"] == "Empty" and st["speed"] == "x 1", "after Clear %s, %s" % (st["start"], st["speed"])
        now = self.br.get(L.BOARD).get("string")
        assert now == board["string"], "after looper %s instead of %s" % (now, board["string"])
        return "Recording %s, overdub/peel, x2, reverse, position %s, cleared; view %s" % (
            length, "/".join("%d" % p for p in pos), now)

    def t_pegel(self):
        out = []
        for m in ("Input", "Output"):
            vals = L.meter_parse(m, self.br.get_many(L.meter_paths(m)))
            for name, cur, peak in vals:
                assert cur is not None and peak is not None, "Cannot read %s/%s" % (m, name)
                assert -121 <= cur <= 7 and -121 <= peak <= 7, "%s/%s out of range: %s %s" % (m, name, cur, peak)
                out.append("%s %.0f dB" % (name, cur))
        return ", ".join(out)

    def t_kompakt(self):
        """get_compact must return the same values as get_many."""
        chain = [m for m in self._chain() if m]
        lr = L.LiveRig(None, None)
        paths = lr.overview_paths() + L.fs_display_paths() + ["/Engine/Patch/GibtEsNicht/X"]
        for m in chain:
            paths += [lr.path(m, "On"), lr.path(m, "Colour")]
        paths += [lr.path(self.need("block"), p) for p in self.need("params")]
        full, comp = self.br.get_many(paths), L.get_compact(self.br, paths)
        bad = [p for p in dict.fromkeys(paths) if full[p].get("ok") != comp[p].get("ok")
               or any(full[p].get(k) != comp[p].get(k) for k in L.COMPACT_FIELDS)]
        assert not bad, "Differences: %s" % bad[:5]
        t0 = time.time()
        for _ in range(5):
            L.get_compact(self.br, paths)
        return "%d matching paths, %.0f ms per query" % (len(set(paths)), (time.time() - t0) / 5 * 1000)

    def t_setlists(self):
        """Create/populate/reorder/rename/delete a DB setlist without restart:
        device set display never sees it, and it is gone afterwards."""
        rig_id = self.need("rig_id")
        name, name2 = "ZZ MXB REGTEST", "ZZ MXB REGTEST 2"
        for nm in (name, name2):
            assert not L.db_name_taken(self.br, "setlists", nm), "Previous leftover: delete setlist %s" % nm
        sid = L.create_setlist(self.br, name, [rig_id])
        try:
            L.setlist_add(self.br, sid, rig_id)
            assert L.db_setlist_members(self.br).get(sid) == [rig_id, rig_id], "Append"
            L.setlist_set_order(self.br, sid, [None, rig_id])
            assert L.db_setlist_rig_ids(self.br, sid) == [None, rig_id], "Reorder with empty slot"
            assert L.rename_setlist(self.br, sid, name2.lower()) == name2
            assert dict(L.db_setlists(self.br)).get(sid) == name2, "Rename"
        finally:
            L.delete_setlist(self.br, sid)
        assert sid not in dict(L.db_setlists(self.br)), "Setlist not deleted"
        assert sid not in L.db_setlist_members(self.br), "Entries not cascade-deleted"
        return "created, two entries, reordered with empty slot, renamed, deleted"

    def t_import(self):
        """Import test .rig as new and delete; Restore everything with existing
        rig/setlist/preset files must create nothing."""
        import tempfile, shutil
        rig_id = self.need("rig_id")
        row = L.db_rig(self.br, rig_id)
        folder = tempfile.mkdtemp(prefix="mx5reg_")
        try:
            for d in ("Rigs", "Setlists", os.path.join("Blocks", "X")):
                os.makedirs(os.path.join(folder, d))
            path = os.path.join(folder, "Rigs", TEST_RIG + ".rig")
            L._write(path, L.rig_file_json(row))
            name = TEST_RIG + " IMPORT"
            assert not L.db_name_taken(self.br, "rigs", name), "Previous leftover: delete %s" % name
            new_id, what = L.import_rig_file(self.br, path, name=name, mode="new")
            try:
                assert what == "new" and new_id != rig_id
                copy = L.db_rig(self.br, new_id)
                assert copy["name"] == name and copy["content"] == L.content_with_name(row["content"], name), "Inhalt"
            finally:
                L.delete_rig(self.br, new_id)
            assert not L.db_name_taken(self.br, "rigs", name), "Import not deleted"
            # Use active setlist or first existing list; if none, omit setlist file
            sls = [s for s, _ in L.db_setlist_rows(self.br)]
            sl = next((s for s in sls if s["name"] == self.set), sls[0] if sls else None)
            if sl:
                rr = [(r, "") for r in L.db_setlist_rig_ids(self.br, sl["id"])]
                L._write(os.path.join(folder, "Setlists", L.safe_filename(sl["name"]) + ".setlist"),
                         L.setlist_file_json(sl, rr))
            bid = self.br.sql("select id from blocks order by id limit 1")[0]["id"]
            blk = L._exact_row(self.br, "blocks", ["id", "type", "name", "content", "is_readonly"], "id", bid)
            L._write(os.path.join(folder, "Blocks", "X", L.safe_filename(blk["name"]) + ".block"), L.block_file_json(blk))
            c = L.restore_all(self.br, folder)
            assert c["rigs"] == 0 and c["rigs_existing"] == 1 and c["setlists"] == 0 and c["blocks"] == 0, c
            assert not c["errors"], c["errors"]
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        return "Imported as new rig and deleted; restoring existing files created nothing"

    def t_verbindung(self):
        """LiveSession: one lost reply is not disconnection; a silent device
        must trigger on_lost."""
        br = self.br
        def session(lost):
            return L.LiveSession(br, dispatch=lambda f: f(), on_lost=lambda: lost.append(time.time()))
        def stop(s):   # Stop worker without closing bridge
            with s._cv:
                s._stop = True
                s._cv.notify()
            s._thread.join(timeout=10)
        def wait(results, n, secs):
            end = time.time() + secs
            while len(results) < n and time.time() < end:
                time.sleep(0.05)
        real = br.out.send
        # 1) Lost message: timeout, ping succeeds, stay connected
        lost, res = [], []
        s = session(lost)
        dropped = []
        br.out.send = lambda m: dropped.append(m) if not dropped else real(m)
        try:
            s.run(lambda b: b.eval("return 1"), res.append)
            s.run(lambda b: b.eval("return 2"), res.append)
            wait(res, 2, 10)
        finally:
            del br.out.send
            stop(s)
        assert not lost and len(res) == 2 and res[1] == "2", "single lost reply: lost=%s res=%s" % (lost, res)
        # 2) Silent device: report loss and stop worker
        lost, res = [], []
        s = session(lost)
        br.out.send = lambda m: None
        t0 = time.time()
        try:
            s.run(lambda b: b.eval("return 1"), res.append)
            wait(lost, 1, 10)
        finally:
            del br.out.send
            stop(s)
        assert lost and s.lost, "Connection loss not detected"
        took = lost[0] - t0
        br._drain()
        # 3) Same via get_many, which pings before retrying a missing reply
        lost, res = [], []
        s = session(lost)
        dropped = []
        br.out.send = lambda m: dropped.append(m) if not dropped else real(m)
        try:
            s.get_many([L.RIGS + "/LoadedName"], res.append)
            wait(res, 1, 10)
        finally:
            del br.out.send
        assert not lost and res and (L.RIGS + "/LoadedName") in res[0], "get_many after lost reply: %s" % res
        br.out.send = lambda m: None
        t0 = time.time()
        try:
            s.get_many([L.RIGS + "/LoadedName"], res.append)
            wait(lost, 1, 12)
        finally:
            del br.out.send
            stop(s)
        assert lost and s.lost, "Connection loss via get_many not detected"
        took2 = lost[0] - t0
        assert took2 < 5.5, "Connection loss via get_many detected only after %.1f s" % took2
        br._drain()
        assert br.ping().startswith("MX5Bridge"), "Bridge no longer responsive afterwards"
        return "Recovered lost reply; silent device detected after %.1f s (eval) / %.1f s (get_many)" % (took, took2)

    def _zahl_param(self):
        """First block's numeric parameter path/info, or None."""
        block = self.need("block")
        for p in self.need("params"):
            path = L.LiveRig.path(block, p)
            info = self.br.get(path)
            if L.kind_of(info) == L.L_NUMBER and "unnormalized" in info:
                return path, info
        return None

    def t_neustart(self):
        """restart_app keep_edits: change test rig without saving, restart, answer No,
        load setlist/test rig; edits must survive and remain dirty while DB
        row stays unchanged. Discard afterwards."""
        if self.quick:
            return "skipped (--quick)"
        t0 = time.time()
        rid = self.need("rig_id")
        st = L.rig_status(self.br)
        if st["id"] != rid:
            res = self.need("liste")
            L.load_rig(self.br, res["rows"], [r["id"] for r in res["rows"]].index(rid), discard=True)
        saved = L.db_rig(self.br, rid)["content"]
        par = self._zahl_param()
        assert par, "no numeric parameter in first block"
        path, info = par
        edit = 0.81 if abs(info["value"] - 0.81) > 0.05 else 0.23
        self.br.set(path, "value", edit)
        self.br.set(L.RIGS + "/Dirty", "state", 1)
        board = self.br.get(L.BOARD).get("string")
        res = L.restart_app(self.br, rig_id=rid, setlist=self.set, keep_edits=True)
        now = self.br.get(L.BOARD).get("string")
        assert now == board, "View after restart %s instead of %s" % (now, board)
        st = L.rig_status(self.br)
        assert st["id"] == rid, "loaded after restart: %s" % (st,)
        assert res["setlist"] == self.set, "Setlist %s" % res["setlist"]
        assert not L.dialog_buttons(self.br), "Dialog open: %s" % L.dialog_buttons(self.br)
        assert res.get("edits_kept") and not res.get("edits_lost"), "Edits not preserved: %s" % res.get("edits_lost")
        assert st["dirty"], "Rig not marked dirty after restart"
        v = self.br.get(path)["value"]
        assert abs(v - edit) < 0.02, "%s after restart %s instead of %s" % (path, v, edit)
        assert L.db_rig(self.br, rid)["content"] == saved, "Test rig database row changed"
        # Discard: clear Dirty and reload to recover saved value
        L.drop_changes(self.br)
        L.reload_rig(self.br, res["rows"])
        v2 = self.br.get(path)["value"]
        assert abs(v2 - info["value"]) < 0.02, "after discard %s instead of %s" % (v2, info["value"])
        assert not L.rig_status(self.br)["dirty"], "still dirty after discard"
        self.res["liste"] = res
        return "App restarted, %s loaded in %s (%s), edit %s = %.2f preserved, %.0f s" % (
            TEST_RIG, self.set, L.LAST_LOAD.get("via"), path.rsplit("/", 2)[-2] + "/" + path.rsplit("/", 1)[-1], edit,
            time.time() - t0)

    def t_all_rigs(self):
        """All Rigs: load second occurrence of a duplicate display name and newest
        rowid rig by position, verifying LoadedID. Do not modify rigs;
        restore original list/test rig afterwards."""
        if self.quick:
            return "skipped (--quick)"
        res = self.need("liste")
        board = self.br.get(L.BOARD)
        allr = L.enter_setlist(self.br, res["setlists"], L.ALL_RIGS, res["setlist_ids"])
        rows = allr["rows"]
        assert len(rows) == self.br.sql("select count(*) n from rigs")[0]["n"], "All Rigs incomplete"
        names = [L.display_name(r["name"]) for r in rows]
        dup = next((i for i, n in enumerate(names) if names.index(n) != i), None)
        picks = ([dup] if dup is not None else []) + [len(rows) - 1]
        done = []
        try:
            for i in picks:   # Duplicate via bank fallback; newest via jump
                t = time.time()
                st = L.load_rig(self.br, rows, i, discard=True, fast=(i != dup))
                assert st["id"] == rows[i]["id"], "%s: LoadedID %s instead of %s" % (names[i], st["id"], rows[i]["id"])
                done.append("%s (slot %d, %s, %.1f s)" % (names[i], i + 1, "Bank" if i == dup else "Sprung",
                                                           time.time() - t))
        finally:
            back = L.enter_setlist(self.br, res["setlists"], self.set, res["setlist_ids"], discard=True)
            idx = [r["id"] for r in back["rows"]].index(self.need("rig_id"))
            st = L.load_rig(self.br, back["rows"], idx, discard=True)
            L.restore_board(self.br, board)
            self.res["liste"] = back
        assert st["id"] == self.need("rig_id") and not st["dirty"], "Test rig not restored"
        return "loaded: %s; returned to %s" % (", ".join(done), self.set)

    def t_datenbank(self):
        row = L.db_rig(self.br, self.need("rig_id"))
        assert row["name"] == TEST_RIG
        d = json.loads(L.rig_file_json(row))
        content = json.loads(d["content"])
        assert isinstance(d["readonly"], bool) and "id" in d and isinstance(content, dict)
        exact = L._sql_text(self.br, "content", "rigs where id = %s" % L.sql_lit(row["id"]), size=5000)
        assert exact == row["content"], "Chunked read returned different content"
        sets = {sl["name"]: rr for sl, rr in L.db_setlist_rows(self.br)}
        if self.set != L.ALL_RIGS:
            assert any(i == row["id"] for i, _ in sets.get(self.set, [])), "Test rig missing from %s" % self.set
        blocks = self.br.sql("select count(*) n from blocks")[0]["n"]
        settings = L.db_settings(self.br)
        assert "RigVersion" in settings
        return "%d content characters, %d setlists, %d block presets, RigVersion %s" % (
            len(row["content"]), len(sets), blocks, settings["RigVersion"])

    def t_eval(self):
        r = self.br.eval("return String(1 + 1) + '/' + typeof App.getProperty;")
        assert r == "2/function", r
        return r

    def t_umlaute(self):
        """Bridge 0.5: non-ASCII round trips via eval, set/get string and SQL."""
        text = "Grüße € 😀 ÄÖÜ"
        r = self.br.eval("return %s;" % json.dumps(text, ensure_ascii=False))
        assert r == text, "eval: %r" % r
        r = self.br.sql("select 'Grüße' || char(8364) as t, hex('ü') as h")   # Raw accented characters in the request
        assert r and r[0]["t"] == "Grüße€" and r[0]["h"] == "C3BC", "SQL: %r" % r
        # Unchecked text property without Dirty: FS1 first-slot scene preset
        path = L.scene_preset_path(5, 1, 0)
        orig = self.br.get(path).get("string")
        try:
            got = self.br.set(path, "string", text).get("string")
            assert got == text, "set: %r" % got
            assert self.br.get_many([path])[path].get("string") == text, "get_many"
        finally:
            self.br.set(path, "string", orig or L.NO_SCENE_PRESET)
        assert self.br.get(path).get("string") == (orig or L.NO_SCENE_PRESET)
        return "eval, SQL (UTF-8 in database request) and set/get string with %r" % text

    def t_ir(self):
        """Read IR list, change test IR via string without IRNotFound,
        then restore original."""
        lr = L.LiveRig(None, Catalog(os.path.join(os.path.dirname(HERE), "catalog.json")))
        r = self.br.get_many(lr.overview_paths())
        for p, info in r.items():
            lr.apply_overview(p, info)
        mods = [m for m in lr.slots if m and (m[:-2] if m.endswith(" 2") else m) in L.IR_MODULES]
        if not mods:
            return "skipped (no IR block in test rig)"
        m = mods[0]
        irs = L.db_irs(self.br)
        total = sum(len(v) for v in irs["files"].values())
        assert total > 0 and irs["folders"], "No IR files read"
        path = L.LiveRig.path(m, "IR")
        orig = self.br.get(path)["string"]
        d0, n0 = L.parse_ir(orig)
        assert d0 is not None, "IR-Text unexpected: %s" % orig
        # Another IR from a different folder than current
        other = next(((f, n) for f in irs["folders"] for n, _ in irs["files"][f] if f != d0), None)
        assert other, "No second IR"
        lite = "Lite" if "1024" in m else ""
        sec = "2" if m.endswith(" 2") else ""
        nf = "/Engine/PresetCtrl/Resources/IRNotFound%s%s" % (lite, sec)
        try:
            self.br.set(path, "string", L.ir_string(*other))
            time.sleep(0.5)
            info = self.br.get(L.LiveRig.path(m, "IRInfoString"))["string"]
            assert info == "%s / %s" % other, "IRInfoString %s" % info
            assert not self.br.get(nf).get("state"), "IRNotFound set for %s" % (other,)
        finally:
            self.br.set(path, "string", orig)
            time.sleep(0.3)
        assert self.br.get(path)["string"] == orig
        return "%d IRs in %d folders, %s: %s -> %s / %s -> restored" % (total, len(irs["folders"]), m, n0, other[0], other[1])

    def t_dateien(self):
        """Bridge file operations: write/read/list/shell in /tmp."""
        d = "/tmp/mx5bridge/regtest"
        data = bytes(range(256)) * 100 + "Accents äöü".encode("utf-8")
        self.br.make_dir(d)
        n = self.br.write_file(d + "/probe.bin", data, chunk=7000)
        assert n == len(data)
        assert self.br.read_file(d + "/probe.bin", page=9000) == data, "read != written"
        self.br.write_file(d + "/Tést.txt", b"x")
        names = sorted(name for name, is_dir, _ in self.br.list_dir(d))
        assert names == ["Tést.txt", "probe.bin"], names
        rc, out = self.br.shell("ls %s | wc -l" % d)
        assert rc == 0 and out.strip() == "2", (rc, out)
        rc, _ = self.br.shell("rm -r %s" % d)
        assert rc == 0 and all(nm != "regtest" for nm, _, _ in self.br.list_dir("/tmp/mx5bridge")), "not deleted"
        return "%d bytes round trip, accented filename, shell OK" % len(data)

    def t_nam(self):
        """NAM (0.7 with NAM): state/models/usage and Drive remapping using
        temporary preset '998 - ...'; no file changes or restart."""
        import uuid
        nam = L.nam_status(self.br)
        if not nam["installed"]:
            return "skipped (firmware without NAM)"
        models = L.db_nam_models(self.br)["models"]
        assert [m["index"] for m in models] == list(range(len(models)))
        test = dict(nam, active=True)
        use = L.nam_usage(self.br, test)
        pid = str(uuid.uuid4())
        name = "998 - ZZ MXB REGTEST"
        content = json.dumps({"data": {"Anxiety OD": {"childorder": ["Drive"], "children": {"Drive": {"type": 0, "value": 7}}}},
                              "info": {"version": "1.0.9"}}, sort_keys=True, separators=(",", ":"))
        drive = "select json_extract(content, '$.data.\"Anxiety OD\".children.Drive.value') v from blocks where id = %s" \
                % L.sql_lit(pid)
        try:
            self.br.sql_write("insert into blocks (id, type, name, content, is_readonly) values (%s, 'ANXIETY OD', %s, %s, 0)"
                              % (L.sql_lit(pid), L.sql_lit(name), L.sql_lit(content)))
            assert "Preset %s (ANXIETY OD)" % name in L.nam_usage(self.br, test).get(7, []), "Usage missing preset"
            assert L.nam_remap(self.br, test, {7: 3, 99: 98}) >= 1
            assert self.br.sql(drive)[0]["v"] == 3, "Drive not remapped"
            row = self.br.sql("select content from blocks where id = %s" % L.sql_lit(pid))[0]["content"]
            assert row == content.replace('"value":7', '"value":3'), "Content besides Drive changed: %s" % row
        finally:
            self.br.sql_write("delete from blocks where id = %s" % L.sql_lit(pid))
        assert L.nam_usage(self.br, test) == use, "Usage changed after test"
        return "NAM %s %s, %d model(s), %d occupied slot(s); preset Drive remapped 7 -> 3, remaining content unchanged" % (
            nam["ref"], "on" if nam["active"] else "off", len(models), len(use))

    def t_ende(self):
        """Always run, including on failure: enter original list, load original rig,
        discard test edits, delete test rigs, refresh and restore view."""
        v = self.br.ping()
        start = self.need("start")
        res = self.need("liste")
        back = L.enter_setlist(self.br, res["setlists"], start["setlist"], res["setlist_ids"], discard=True)
        ids = [r["id"] for r in back["rows"]]
        assert start["rig"] in ids, "Original rig %s no longer in '%s'" % (start["name"], start["setlist"])
        if L.rig_status(self.br)["id"] != start["rig"]:
            L.load_rig(self.br, back["rows"], ids.index(start["rig"]), discard=True)
        left = self._leftovers()
        for r in left:
            L.delete_rig(self.br, r["id"])
        L.refresh_lists(self.br, res["setlists"], start["setlist"], res["setlist_ids"])
        L.restore_board(self.br, start["board"])
        st = L.rig_status(self.br)
        assert st["id"] == start["rig"] and not st["dirty"], "Final state: %s dirty=%s" % (st["name"], st["dirty"])
        assert not self._leftovers(), "Test rig not deleted"
        assert not L.dialog_buttons(self.br), "Dialog open on device"
        now = self.br.get(L.BOARD).get("string")
        assert now == start["board"].get("string"), "View %s instead of %s" % (now, start["board"].get("string"))
        return "%s; restored %s in '%s', view %s, %d test rig(s) deleted" % (
            v, start["name"], start["setlist"], now, len(left))

    # ---------------- Execution ----------------
    def run(self):
        steps = [("Connect", self.t_verbinden), ("Rig list (database)", self.t_rigliste),
                 ("Create test rig", self.t_vorbereiten), ("Load another rig", self.t_anderes_rig),
                 ("Load test rig", self.t_laden), ("Read chain", self.t_kette),
                 ("Write toggle and Dirty", self.t_schreiben), ("Numeric parameter", self.t_zahl),
                 ("Discard (reload)", self.t_verwerfen), ("Save", self.t_speichern),
                 ("DB: color and refresh", self.t_db_farbe), ("DB: save as new and delete", self.t_db_kopie),
                 ("Hardware assignments", self.t_hardware), ("Scenes and footswitches", self.t_scenes),
                 ("Move blocks", self.t_verschieben), ("Tuner", self.t_tuner), ("Looper", self.t_looper),
                 ("Pegel", self.t_pegel),
                 ("Compact poll", self.t_kompakt), ("All Rigs: load by position", self.t_all_rigs),
                 ("Database export", self.t_datenbank),
                 ("EVAL", self.t_eval), ("Unicode (bridge 0.5)", self.t_umlaute),
                 ("DB: Setlists", self.t_setlists), ("DB: import and restore", self.t_import),
                 ("IR selection", self.t_ir), ("Files (bridge)", self.t_dateien), ("NAM mod (bridge 0.7)", self.t_nam),
                 ("Connection loss", self.t_verbindung), ("App restart", self.t_neustart),
                 ("Cleanup and final state", self.t_ende)]
        t0 = time.time()
        for name, fn in steps:
            if self.br is None and fn != self.t_verbinden:
                self.bad += 1
                self.failed.append(name)
                self.say("  -       %-34s        no connection" % name)
                continue
            self.step(name, fn)
        self.say("Result: %d passed, %d failed, %.0f s%s" % (
            self.ok, self.bad, time.time() - t0, ("  -> " + ", ".join(self.failed)) if self.failed else ""))
        if self.br:
            self.br.close()
        self.log.close()
        return self.bad == 0


if __name__ == "__main__":
    ok = Test(quick=any(arg in sys.argv for arg in ("--quick", "--schnell"))).run()
    sys.exit(0 if ok else 1)
