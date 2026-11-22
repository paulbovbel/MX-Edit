"""settings.py: editor preferences stored in %APPDATA%/MX5Editor/einstellungen.json,
and the Settings overlay (ui.Overlay, like tuner/IR dialogs; left sidebar).

Preferences are a flat dictionary. Unknown/missing keys use DEFAULTS;
unreadable files are ignored and defaults apply."""
import json, os, re, tkinter as tk

import ui as U
from ui import PANEL, LINE, TEXT, MUTED, DIM, px
from ui import FONT_BOLD, FONT_SMALL

DEFAULTS = {
    # IR/NAM browsers: a click loads the file immediately and keeps the browser open (audition)
    "audition": True,
    # Footswitch bar below the chain (live); when disabled, use 'Footswitches & pedals' in the menu
    "footswitch_bar": True,
    # UI size percentage in addition to Windows scaling; takes effect after restart
    "ui_scale": 100,
    # Dialog, menu and page crossfades (ui.ghost_fade)
    "animations": True,
    # Connect to MX5 immediately when starting without a folder
    "auto_connect": True,
    # Fixed MIDI ports (empty means automatic name matching in bridge.py)
    "midi_in": "",
    "midi_out": "",
}

SCALES = [80, 90, 100, 110, 125, 150]
AUTO_PORT = "Automatic"


def path():
    """Windows %APPDATA%, macOS ~/Library/Application Support, Linux $XDG_CONFIG_HOME or ~/.config."""
    if U.WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif U.MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "MX5Editor", "einstellungen.json")


def load():
    data = dict(DEFAULTS)
    try:
        with open(path(), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in DEFAULTS and type(v) is type(DEFAULTS[k])})
    except (OSError, ValueError):
        pass
    if data["ui_scale"] not in SCALES:
        data["ui_scale"] = DEFAULTS["ui_scale"]
    return data


def save(data):
    p = path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
        return None
    except OSError as e:
        return str(e)


# ---------------- MIDI ports ----------------
def port_names():
    """(inputs, outputs) reported by mido; two empty lists if mido is unavailable."""
    try:
        import mido
        return mido.get_input_names(), mido.get_output_names()
    except Exception:
        return [], []


def _port_base(name):
    # Windows rtmidi appends a changeable index, e.g. 'HeadRush MX5 1'
    return re.sub(r"\s+\d+$", "", name)


def pick_port(names, wanted):
    """Match a saved port exactly, falling back to a match without its trailing index."""
    if wanted in names:
        return wanted
    hits = [n for n in names if _port_base(n) == _port_base(wanted)]
    if len(hits) == 1:
        return hits[0]
    raise LookupError("MIDI port “%s” not found" % wanted)


def find_ports(want_in, want_out):
    """(input, output) for Bridge(): fixed ports from preferences, remaining ports automatic."""
    from bridge import Bridge
    ins, outs = port_names()
    auto = None if (want_in and want_out) else Bridge.find_ports()
    return (pick_port(ins, want_in) if want_in else auto[0],
            pick_port(outs, want_out) if want_out else auto[1])


class SettingsDialog(U.Overlay):
    """Settings page: save every change immediately. Editor.pref_changed applies
    changes that do not require a restart."""

    def __init__(self, editor):
        super().__init__(editor, "Settings", width=600, click_out=True)
        self.ed = editor
        self.closed = False
        b = self.body
        p = editor.prefs

        self.section("BROWSERS")
        self.row("Audition while browsing",
                 "IR and NAM browser: a click loads the file at once, so you can hear it right away. The browser "
                 "stays open until you close it (“Done” keeps the current choice, “Revert” goes back to the one "
                 "you started with). When off, the browser closes after you apply a file.",
                 lambda f: self.toggle(f, "audition"))

        self.section("DISPLAY")
        self.row("Footswitch bar",
                 "The three footswitches below the signal chain (live mode). When hidden, footswitches and pedals "
                 "open from the ⋯ menu (“Footswitches & pedals …”).",
                 lambda f: self.toggle(f, "footswitch_bar"))
        self.row("Animations",
                 "Dialogs, menus and pages fade in and out. Turn off for an instant switch.",
                 lambda f: self.toggle(f, "animations"))
        self.lbl_scale = None
        self.row("Interface size",
                 "Scales text and controls on top of the system display scaling.", None)
        sf = tk.Frame(b, bg=PANEL)
        sf.pack(fill="x", pady=(0, px(4)))
        U.Segmented(sf, ["%d %%" % v for v in SCALES], SCALES.index(p["ui_scale"]),
                    on_pick=lambda i: self.set_scale(SCALES[i]), padx=8).pack(side="left")
        self.lbl_scale = tk.Label(b, text="", bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w")
        self.lbl_scale.pack(fill="x", pady=(0, px(10)))
        self.scale_hint()

        self.section("CONNECTION")
        self.row("Connect on start",
                 "Look for the MX5 as soon as the editor starts. Turn off if you mostly edit .rig files offline – "
                 "“Connect to Device” still connects on demand.",
                 lambda f: self.toggle(f, "auto_connect"))
        self.row("MIDI ports",
                 "“Automatic” finds the MX5 by its port name. Choose the ports yourself if several devices "
                 "match or the name differs. Used for the next connection.", None)
        pf = tk.Frame(b, bg=PANEL)
        pf.pack(fill="x", pady=(0, px(10)))
        self.dd_ports = {}
        for col, (key, label) in enumerate((("midi_in", "Input"), ("midi_out", "Output"))):
            tk.Label(pf, text=label, bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w").grid(
                row=0, column=col, sticky="w", padx=(0, px(12)))
            dd = U.Dropdown(pf, [AUTO_PORT], p[key] or AUTO_PORT, width=22, title="MIDI " + label.lower(),
                            on_pick=lambda i, k=key: self.pick_port(k, i), font=FONT_SMALL,
                            on_open=lambda k=key: self.open_ports(k))
            dd.grid(row=1, column=col, sticky="we", padx=(0, px(12)), pady=(px(2), 0))
            pf.columnconfigure(col, weight=1)
            self.dd_ports[key] = dd

        tk.Frame(b, bg=LINE, height=1).pack(fill="x", pady=(px(4), px(8)))
        tk.Label(b, text="Saved in %s" % path(), bg=PANEL, fg=DIM, font=FONT_SMALL, anchor="w", justify="left",
                 wraplength=px(540)).pack(fill="x")
        bf = tk.Frame(b, bg=PANEL)
        bf.pack(fill="x", pady=(px(14), 0))
        U.Btn(bf, "Close", command=self.close, kind="primary").pack(side="right")
        self.bind_keys()

    # ---- Layout ----
    def section(self, text):
        tk.Label(self.body, text=text, bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w").pack(
            fill="x", pady=(px(4), px(6)))

    def row(self, label, text, control):
        """Row with title/explanation at left; control(frame) builds the right control (or None)."""
        row = tk.Frame(self.body, bg=PANEL)
        row.pack(fill="x", pady=(0, px(10) if control else px(6)))
        if control:
            control(row).pack(side="right", anchor="n", padx=(px(16), 0), pady=(px(2), 0))
        tf = tk.Frame(row, bg=PANEL)
        tf.pack(side="left", fill="x", expand=True)
        tk.Label(tf, text=label, bg=PANEL, fg=TEXT, font=FONT_BOLD, anchor="w").pack(fill="x")
        tk.Label(tf, text=text, bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
                 wraplength=px(480)).pack(fill="x", pady=(px(2), 0))

    def toggle(self, parent, key):
        return U.Toggle(parent, self.ed.prefs.get(key), command=lambda v: self.set(key, v), bg=PANEL)

    # ---- Changes ----
    def set(self, key, value):
        self.ed.prefs[key] = value
        err = save(self.ed.prefs)
        self.ed.status.set("Could not save the settings: %s" % err if err else "Settings saved.")
        self.ed.pref_changed(key)

    def set_scale(self, value):
        self.set("ui_scale", value)
        self.scale_hint()

    def scale_hint(self):
        now, want = self.ed.ui_scale, self.ed.prefs["ui_scale"]
        self.lbl_scale.configure(text="" if now == want else
                                 "Takes effect the next time the editor starts (now %d %%)." % now)

    def open_ports(self, key):
        """Read ports only when opening the list; devices may have been connected meanwhile."""
        ins, outs = port_names()
        names = ins if key == "midi_in" else outs
        cur = self.ed.prefs[key]
        if cur and cur not in names:
            names = names + [cur]   # Saved port, currently disconnected
        self._ports = [""] + names
        self.dd_ports[key].show([AUTO_PORT] + names)

    def pick_port(self, key, i):
        self.set(key, self._ports[i])

    def cancel(self):
        self.close()

    def close(self, result=None):
        if self.closed:
            return
        self.closed = True
        self.ed.settings_done(self)
        super().close(result)
