#!/usr/bin/env python3
"""MX5 Bridge: unofficial PC test program 0.10.

Requires pip install mido python-rtmidi, with bridge.py alongside.
Usage: python mx5bridge_test.py          automatic test
       python mx5bridge_test.py --shell  interactive
       python mx5bridge_test.py --ports  list MIDI ports only
Output is also written to mx5bridge_testlog.txt.

0.7: loading via /Engine/PresetCtrl/Rigs/ReceivePresetIndex=program-1;
saving via /Engine/RedirCtrl/EditModeSaveRig state=1, then
cancel|save|save_new_rig mapped to FS1|FS2|FS3 through RawFootswitches/FSn.
0.8: read rig list from bank display: RigName1-3, NextBank, repeat;
this should not load any rig.
0.9: sql shell command for read-only rig DB queries (bridge 0.3), e.g.
sql select name, prog_num from rigs order by name
0.10: write shell command for transactional writes (bridge 0.4), and
action status|backup|restore|restart; e.g.
write update rigs set color = 3 where name = 'MXBRIDGE TEST'
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                    # Standalone bridge PC test: bridge.py alongside
sys.path.insert(1, os.path.dirname(HERE))   # Editor werkzeuge folder: bridge.py in parent editor folder
try:
    import mido
except ImportError:
    sys.exit("Please install first:  pip install mido python-rtmidi")
from bridge import Bridge, BridgeError

VERSION = "0.9"
LOG = open("mx5bridge_testlog.txt", "a", encoding="utf-8")
P = "/Engine/Patch"
PC = "/Engine/PresetCtrl"
RIGS = PC + "/Rigs"
RIG_PROPS = ["LoadedID", "LoadedName", "LoadedColor", "Dirty", "LoadedProgNum"]
SAVE = "/Engine/RedirCtrl/EditModeSaveRig"
FS = "/Engine/RedirCtrl/RawFootswitches/FS%d"
BOARD = "/Engine/RedirCtrl/BoardMode"
CHANNEL = "/Engine/MIDICtrl/Channel"
USB = "/media/az01-internal/Evil/usb_mnt"
BANK_PROPS = ["RigName1", "RigName2", "RigName3", "RigColour1", "RigColour2", "RigColour3",
              "SetName1", "SetRigName1", "SetRigName2", "SetRigName3", "CurrentPreset",
              "LoadRig1", "LoadRig2", "LoadRig3", "NextBank", "PrevBank", "NextPreset", "PrevPreset"]


class Abort(Exception):
    pass


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    LOG.write(s + "\n")
    LOG.flush()


def ask(q):
    a = input(q + " [y/n] ").strip().lower()
    log("   ", q, "->", a)
    return a in ("y", "yes")


def ask_text(q):
    a = input(q + " ").strip()
    log("   ", q, "->", a)
    return a


def step(title, fn):
    log("\n=== %s ===" % title)
    try:
        return fn()
    except BridgeError as e:
        log("  ERROR:", e)
    except Abort:
        raise
    except Exception as e:
        log("  EXCEPTION:", type(e).__name__, e)


def alive(b):
    try:
        b.ping()
        return True
    except BridgeError:
        log("  Bridge stopped responding. Aborting; please restart the MX5.")
        raise Abort()


def connect():
    ins, outs = mido.get_input_names(), mido.get_output_names()
    log("Inputs:", ins)
    log("Outputs:", outs)
    try:
        i, o = Bridge.find_ports()
    except BridgeError as e:
        log(e)
        log("Note: the port appears about 10 s after MX5 startup or after")
        log("leaving USB Transfer mode.")
        sys.exit(1)
    log("Using input '%s', output '%s'" % (i, o))
    return Bridge(i, o)


def rig_state(b):
    res = b.get_many(["%s/%s" % (RIGS, n) for n in RIG_PROPS])
    return {k.rsplit("/", 1)[1]: v for k, v in res.items()}


def short(v):
    if not v.get("ok"):
        return "(missing)"
    keys = ("string", "unnormalized", "index", "state", "numEntries", "maximum")
    return {k: v[k] for k in keys if k in v}


def prog_of(st):
    s = st["LoadedProgNum"].get("string", "-")
    return int(s) if s.isdigit() else None


def press(b, path, hold=0.15):
    b.set(path, "state", 1)
    time.sleep(hold)
    return short(b.set(path, "state", 0))


def bank_names(b):
    res = b.get_many(["%s/RigName%d" % (PC, n) for n in (1, 2, 3)])
    return [res["%s/RigName%d" % (PC, n)].get("string") for n in (1, 2, 3)]


def midi_channel(b):
    try:
        ch = int(b.get(CHANNEL).get("unnormalized", 1))
    except (BridgeError, ValueError, TypeError):
        ch = 1
    return max(0, min(15, ch - 1))


def send_pc(b, program, channel):
    b.out.send(mido.Message("program_change", channel=channel, program=max(0, min(127, program))))


def js_list(expr):
    return ("var l = %s; if (l === undefined || l === null) return 'undefined'; "
            "try { return l.length + ' entries: ' + Array.prototype.slice.call(l, 0, 40).join(' | '); } "
            "catch (e) { return 'not an array: ' + l; }" % expr)


def js_members(expr):
    return ("var o = %s; if (o === undefined || o === null) return 'undefined'; var r = []; "
            "for (var k in o) { var t; try { t = typeof o[k]; } catch (e) { t = '?'; } r.push(k + ':' + t); } "
            "return r.length + ' members: ' + r.join(' ');" % expr)


def browse_banks(b, start_id, limit=60):
    """Press NextBank until the initial triplet repeats; return (list, loaded_changed)."""
    first = bank_names(b)
    seen, banks = [first], 1
    log("  Bank 1: %s" % first)
    for i in range(limit):
        press(b, "%s/NextBank" % PC)
        time.sleep(0.4)
        names = bank_names(b)
        if names == first and i > 0:
            break
        banks += 1
        seen.append(names)
        log("  Bank %d: %s" % (banks, names))
        if names == first:
            break
    now = rig_state(b)["LoadedID"].get("string")
    rigs = [n for grp in seen for n in grp if n]
    return rigs, banks, now != start_id


def autotest(b):
    ver = step("1. PING", lambda: b.ping())
    log("  Reply:", ver)
    if not b.v2:
        log("Bridge 0.2 is required.")
        return

    def props():
        st = rig_state(b)
        for k, v in st.items():
            log("  Rigs/%s: %s" % (k, short(v)))
        bm = b.get(BOARD)
        log("  BoardMode: %s | entries: %s" % (short(bm), b.entries(BOARD)))
        res = b.get_many(["%s/%s" % (PC, n) for n in BANK_PROPS])
        for n in BANK_PROPS:
            log("  PresetCtrl/%s: %s" % (n, short(res["%s/%s" % (PC, n)])))
        return st, bm
    st, bm = step("2. Rig and bank properties", props) or (None, None)
    alive(b)
    if not st:
        return

    def banks():
        if st["Dirty"].get("state"):
            log("  Rig has unsaved edits; test skipped (please save first).")
            return
        start_id, start_name = st["LoadedID"].get("string"), st["LoadedName"].get("string")
        if not ask("Browse banks (NextBank until first repeat)? Please watch the device display."):
            log("  skipped")
            return
        rigs, n, changed = browse_banks(b, start_id)
        log("  %d banks, %d rig names: %s" % (n, len(rigs), rigs))
        log("  Loaded rig changed: %s (now %s)" % (changed, rig_state(b)["LoadedName"].get("string")))
        ask_text("What changed on the device display?")
        if len(set(rigs)) <= 3 and ask("Only one bank found. Try again in Rig mode (BoardMode)?"):
            old = bm.get("index", 0)
            names = b.entries(BOARD)
            target = next((i for i, s in enumerate(names) if s == "Rig"), None)
            if target is None:
                log("  No Rig mode in the list.")
            else:
                log("  BoardMode -> Rig:", short(b.set(BOARD, "index", target)))
                time.sleep(1.0)
                rigs, n, changed = browse_banks(b, start_id)
                log("  %d banks, %d rig names: %s" % (n, len(rigs), rigs))
                log("  Loaded rig changed: %s" % changed)
                ask_text("What changed on the device display?")
                log("  BoardMode restored:", short(b.set(BOARD, "index", old)))
        if rig_state(b)["LoadedID"].get("string") != start_id:
            log("  Please reload '%s' on the MX5." % start_name)
    step("3. Rig list through bank display", banks)
    alive(b)

    def load_from_bank():
        st2 = rig_state(b)
        start_name, start_prog = st2["LoadedName"].get("string"), prog_of(st2)
        names = bank_names(b)
        log("  Current bank: %s | loaded: %s" % (names, start_name))
        other = next((i for i, n in enumerate(names) if n and n != start_name), None)
        if other is None:
            log("  No other rig in the bank; skipped.")
            return
        if not ask("Press LoadRig%d (loads '%s')?" % (other + 1, names[other])):
            log("  skipped")
            return
        log("  LoadRig%d:" % (other + 1), press(b, "%s/LoadRig%d" % (PC, other + 1)))
        time.sleep(2.5)
        log("  loaded:", rig_state(b)["LoadedName"].get("string"))
        if start_prog is not None:
            b.set("%s/ReceivePresetIndex" % RIGS, "unnormalized", start_prog - 1)
            time.sleep(2.5)
            log("  restored via program %d: loaded %s" % (start_prog, rig_state(b)["LoadedName"].get("string")))
        else:
            log("  Please reload '%s' on the MX5." % start_name)
    step("4. Load rig from bank", load_from_bank)


def shell(b):
    log("connected:", b.ping())
    print("Commands: ping | get <path> | set <path> <field> <value> | entries <path> | many <path>;<path>")
    print("         pc <number> | fs <1-3> | press <path> | save | bank | ls <folder> | members <js> | eval <js>")
    print("         sql <query> (bridge 0.3, read-only) | write <statements> (0.4, transaction)")
    print("         action status|backup|restore|restart (0.4) | sh <shell-line> | dir <folder> (0.3) | quit")
    ch = midi_channel(b)
    while True:
        try:
            line = input("mx5> ").strip()
        except EOFError:
            break
        if not line:
            continue
        log("mx5>", line)
        cmd, _, rest = line.partition(" ")
        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "ping":
                log(b.ping())
            elif cmd == "get":
                log(b.get(rest))
            elif cmd == "set":
                parts = rest.rsplit(" ", 2)
                log(b.set(*parts) if len(parts) == 3 else "set <path> <field> <value>")
            elif cmd == "entries":
                log(b.entries(rest))
            elif cmd == "many":
                for k, v in b.get_many([p.strip() for p in rest.split(";") if p.strip()]).items():
                    log(" ", k, v)
            elif cmd == "pc":
                send_pc(b, int(rest), ch)
                time.sleep(2.0)
                log(short(b.get("%s/LoadedName" % RIGS)))
            elif cmd == "fs":
                log(press(b, FS % int(rest)))
            elif cmd == "press":
                log(press(b, rest))
            elif cmd == "save":
                log(short(b.set(SAVE, "state", 1)))
            elif cmd == "bank":
                log(bank_names(b))
            elif cmd == "ls":
                log(b.eval(js_list("App.entryList('file://%s', ['*'], false)" % (rest or USB))))
            elif cmd == "members":
                log(b.eval(js_members(rest)))
            elif cmd == "eval":
                log(b.eval(rest))
            elif cmd == "sql":
                rows = b.sql(rest)
                log("%d rows" % len(rows))
                for r in rows[:60]:
                    log(" ", json.dumps(r, ensure_ascii=False)[:300])
            elif cmd == "write":
                log("%d rows changed" % b.sql_write(rest))
            elif cmd == "action":
                log(json.dumps(b.action(rest.strip()), ensure_ascii=False))
            elif cmd == "sh":
                rc, out = b.shell(rest)
                log("Exit %d" % rc, out.rstrip())
            elif cmd == "dir":
                for name, is_dir, mtime in b.list_dir(rest or "/media/az01-internal/Evil/usb_mnt/Impulse Responses"):
                    log(" ", name + ("/" if is_dir else ""))
            else:
                print("unknown command")
        except BridgeError as e:
            log("ERROR:", e)


if __name__ == "__main__":
    log("\n##### MX5 Bridge Test %s" % VERSION, time.strftime("%Y-%m-%d %H:%M:%S"), "#####")
    if "--ports" in sys.argv:
        log("Inputs:", mido.get_input_names())
        log("Outputs:", mido.get_output_names())
        sys.exit()
    bridge = connect()
    try:
        shell(bridge) if "--shell" in sys.argv else autotest(bridge)
    except Abort:
        log("Cancelled.")
    finally:
        bridge.close()
    log("Done. Log: mx5bridge_testlog.txt")
