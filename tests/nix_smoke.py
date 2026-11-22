"""Start the installed GUI on a virtual display, without MIDI/device access."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def main():
    editor = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(editor))
    with tempfile.TemporaryDirectory(prefix="mx-edit-smoke-") as temporary:
        home = Path(temporary)
        os.environ["HOME"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
        rigs = home / "Rigs"
        rigs.mkdir()

        import mido.backends.rtmidi  # noqa: F401: load the native backend, not a device
        import mx5editor

        if mx5editor.U.Image is None or not mx5editor.MODEL_PAGES:
            raise RuntimeError("Pillow or packaged model-page assets are missing")
        errors = []
        with (
            patch.object(mx5editor.Editor, "connect", side_effect=RuntimeError("Unexpected MIDI access")),
            patch("rtmidi.MidiIn", side_effect=RuntimeError("Unexpected MIDI input")),
            patch("rtmidi.MidiOut", side_effect=RuntimeError("Unexpected MIDI output")),
        ):
            app = mx5editor.Editor(folder=str(rigs))
            try:
                app.report_callback_exception = lambda *exception: errors.append(exception)
                app.after(750, app.quit)
                app.mainloop()
                if app.session is not None or app.folder != str(rigs):
                    raise RuntimeError("Editor did not stay in offline folder mode")
                if errors:
                    raise RuntimeError(f"GUI callback failed: {errors}")
            finally:
                app.destroy()
    print("Installed GUI, assets and MIDI backend passed offline smoke test.")


if __name__ == "__main__":
    main()
