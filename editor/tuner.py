"""tuner.py: Tuner overlay above the editor (ui.Overlay, blurred background).
Opens the MX5 tuner and displays note/deviation with low latency: values are
polled about 30 times per second (~5 ms per query; device updates ~35 ms).

Normal editor polling pauses while tuning; the device cannot edit rigs then.
Closing (x, Escape, outside click, Exit tuner) exits the device tuner.
If the device exits via footswitch, close the overlay. Mute/reference pitch
write directly to TunerMuting/TunerRef.
"""
import time, tkinter as tk

import live as L
import ui as U
from ui import PANEL, CARD, LINE, ACCENT, TEXT, MUTED, DIM, px
from ui import FONT_SMALL, FONT_BOLD

TICK_MS = 8             # Poll delay plus ~5 ms query/editor pump interval gives ~30 frames/s
SLOW_MS = 500           # Delay after an error
GRACE = 1.0             # Seconds: protect local mute/reference writes from stale poll updates
RANGE = 50.0            # Displayed cents range, matching the device: -50..+50
AMBER = "#f2b33d"
SCALE_TXT = "#c9a227"


class TunerDialog(U.Overlay):
    """first: initial reading (live.tuner_open). Stored in editor.tuner_dialog;
    notify editor.tuner_dialog_done(dlg, device_exited) on close."""

    def __init__(self, editor, first):
        super().__init__(editor, "Tuner", width=600, click_out=True)
        self.ed = editor
        self.pending = False        # Poll in progress
        self.closed = False
        self.shown = 0.0            # Smoothed needle position in cents
        self.ref = first.get("ref") or 440
        self.mute = first.get("mute")
        self._written = {}          # Path -> time of local write
        self.build()
        self.show(first)
        self.after(TICK_MS, self.tick)

    # ---------------- Layout ----------------
    def build(self):
        b = self.body
        b.configure(padx=px(6))
        self.lbl_note = tk.Label(b, text="–", bg=PANEL, fg=MUTED, font=(U.SANS, 64, "bold"))
        self.lbl_note.pack(pady=(px(4), 0))
        self.lbl_cents = tk.Label(b, text="", bg=PANEL, fg=MUTED, font=FONT_BOLD)
        self.lbl_cents.pack()
        self.meter = tk.Canvas(b, bg=PANEL, height=px(96), highlightthickness=0)
        self.meter.pack(fill="x", pady=(px(6), px(10)))
        self.meter.bind("<Configure>", lambda e: self.draw())

        row = tk.Frame(b, bg=PANEL)
        row.pack(fill="x", pady=(px(4), 0))
        # Mute
        mute = tk.Frame(row, bg=PANEL)
        mute.pack(side="left")
        tk.Label(mute, text="Mute output", bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=(0, px(8)))
        self.tg_mute = U.Toggle(mute, value=bool(self.mute), command=self.set_mute)
        self.tg_mute.pack(side="left")
        # Reference pitch
        ref = tk.Frame(row, bg=PANEL)
        ref.pack(side="left", padx=(px(28), 0))
        tk.Label(ref, text="Reference", bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=(0, px(8)))
        U.Btn(ref, "−", command=lambda: self.step_ref(-1), padx=8, pady=2).pack(side="left")
        self.lbl_ref = tk.Label(ref, text="", bg=PANEL, fg=TEXT, font=FONT_BOLD, width=7)
        self.lbl_ref.pack(side="left")
        U.Btn(ref, "+", command=lambda: self.step_ref(1), padx=8, pady=2).pack(side="left")
        # Exit
        U.Btn(row, "Close tuner", command=self.close, kind="primary").pack(side="right")

        self.lbl_hint = tk.Label(b, text="The tuner runs on the device; a footswitch there closes it as well.",
                                 bg=PANEL, fg=DIM, font=FONT_SMALL, anchor="w")
        self.lbl_hint.pack(fill="x", pady=(px(14), 0))
        self.show_ref()

    # ---------------- Polling ----------------
    def tick(self):
        if self.closed or not self.winfo_exists():
            return
        self.raise_()    # Stay above anything the editor opens in the meantime
        s = self.ed.session
        if not s:
            self.close(quiet=True)
            return
        if self.pending:
            self.after(TICK_MS, self.tick)
            return
        self.pending = True
        s.get_many(L.TUNER_PATHS, self.reply)

    def reply(self, res):
        self.pending = False
        if self.closed or not self.winfo_exists():
            return
        info = L.tuner_parse(res) if isinstance(res, dict) else {"ok": False}
        if not info.get("ok"):
            self.lbl_hint.configure(text="No reply from the device … %s" % ((res or {}).get("err") or ""), fg=AMBER)
            self.after(SLOW_MS, self.tick)
            return
        if not info["active"]:
            # Device tuner exited via footswitch: close without sending further commands
            self.close(device_exited=True)
            return
        self.show(info)
        self.after(TICK_MS, self.tick)

    def show(self, info):
        note, cents = info.get("note") or "", info.get("cents") or 0.0
        now = time.time()
        if info.get("ref") and now - self._written.get(L.TUNER_REF, 0) > GRACE and info["ref"] != self.ref:
            self.ref = info["ref"]
            self.show_ref()
        if info.get("mute") is not None and now - self._written.get(L.TUNER_MUTE, 0) > GRACE \
                and info["mute"] != self.mute:
            self.mute = info["mute"]
            self.tg_mute.set(self.mute)
        if note:
            good = abs(cents) < L.TUNER_OK
            self.lbl_note.configure(text=note, fg=ACCENT if good else TEXT)
            self.lbl_cents.configure(text="%+.1f cents  ·  %s" % (cents, "in tune" if good else
                                                                    ("too low ♭" if cents < 0 else "too high ♯")),
                                     fg=ACCENT if good else AMBER)
            self.shown += (cents - self.shown) * 0.6
        else:
            self.lbl_note.configure(text="–", fg=MUTED)
            self.lbl_cents.configure(text="no signal", fg=MUTED)
            self.shown *= 0.6
        self.draw(bool(note))
        if self.lbl_hint.cget("fg") != DIM:
            self.lbl_hint.configure(text="The tuner runs on the device; a footswitch there closes it as well.",
                                    fg=DIM)

    def show_ref(self):
        self.lbl_ref.configure(text="%d Hz" % self.ref)

    # ---------------- Display ----------------
    def draw(self, active=None):
        c = self.meter
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10:
            return
        if active is None:
            active = self.lbl_note.cget("text") != "–"
        m = px(28)                      # Border
        x0, x1 = m, w - m
        y = h * 0.56
        mid = (x0 + x1) / 2
        # Scale
        c.create_line(x0, y, x1, y, fill=LINE, width=px(2))
        for k in range(-50, 51, 5):
            x = mid + (x1 - x0) * k / (2 * RANGE)
            big = k % 25 == 0
            c.create_line(x, y - px(9 if big else 5), x, y + px(9 if big else 5), fill=DIM if not big else MUTED,
                          width=px(2 if big else 1))
            if k in (-50, -25, 25, 50):
                c.create_text(x, y + px(20), text="%+d" % k, fill=SCALE_TXT, font=FONT_SMALL)
        c.create_text(mid, y + px(20), text="0", fill=SCALE_TXT, font=FONT_SMALL)
        # Tolerance band
        tol = (x1 - x0) * L.TUNER_OK / (2 * RANGE)
        c.create_rectangle(mid - tol, y - px(14), mid + tol, y + px(14), fill=CARD, outline="")
        # Center mark
        c.create_polygon(mid - px(7), y - px(30), mid + px(7), y - px(30), mid, y - px(18),
                         fill=ACCENT if active and abs(self.shown) < L.TUNER_OK else DIM, outline="")
        if not active:
            return
        v = max(-RANGE, min(RANGE, self.shown))
        x = mid + (x1 - x0) * v / (2 * RANGE)
        good = abs(self.shown) < L.TUNER_OK
        col = ACCENT if good else AMBER
        # Bar from center to needle
        if abs(x - mid) > px(2):
            c.create_rectangle(min(mid, x), y - px(5), max(mid, x), y + px(5), fill=col, outline="", stipple="gray50")
        U.rrect(c, x - px(4), y - px(22), px(8), px(44), px(4), col)

    # ---------------- Controls ----------------
    def set_mute(self, on):
        s = self.ed.session
        if not s:
            return
        self.mute = bool(on)
        self._written[L.TUNER_MUTE] = time.time()
        s.set(L.TUNER_MUTE, "state", 1 if on else 0)

    def step_ref(self, d):
        s = self.ed.session
        if not s:
            return
        self.ref = max(L.TUNER_REF_MIN, min(L.TUNER_REF_MAX, self.ref + d))
        self.show_ref()
        self._written[L.TUNER_REF] = time.time()
        s.set(L.TUNER_REF, "unnormalized", self.ref)

    def cancel(self):
        self.close()

    def close(self, device_exited=False, quiet=False):
        """quiet: close only the window; the editor handles the device itself."""
        if self.closed:
            return
        self.closed = True
        if not quiet:
            self.ed.tuner_dialog_done(self, device_exited)
        U.Overlay.close(self)
