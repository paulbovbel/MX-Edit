#!/usr/bin/env python3
"""Build MX-Edit as a single-file executable; users do not need Python.

Usage from any directory: python werkzeuge/exe_bauen.py
Diagnostics build: python werkzeuge/exe_bauen.py diagnose
Requires: pip install pyinstaller mido python-rtmidi pillow
Output: <editor>/exe/MX-Edit.exe; exe/build intermediates are removed.
macOS: exe/MX-Edit.app; Linux: exe/MX-Edit. PyInstaller only targets its host;
.github/workflows/build.yml builds Mac/Linux releases on GitHub.

Regenerate/embed mxedit.ico from ui.app_icon (knob matching title-bar logo;
each 16-256 px size drawn separately). mx5editor.py also uses it for window/
taskbar. Bundle catalog.json and modellseiten.json beside the editor, or
inside sys._MEIPASS for packaged builds. mido imports its backend dynamically,
so a hidden import is required."""
import os, shutil, subprocess, sys

ED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ED, 'exe')
ICON = os.path.join(ED, 'mxedit.ico')


def bold_font(size):
    """Bold icon font: Windows Segoe UI/Arial, macOS Helvetica/Arial, Linux DejaVu;
    otherwise Pillow's built-in font."""
    from PIL import ImageFont
    cands = [os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', n) for n in ('segoeuib.ttf', 'arialbd.ttf')]
    cands += ['/System/Library/Fonts/Supplemental/Arial Bold.ttf', '/Library/Fonts/Arial Bold.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf']
    for p in cands:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def make_icon(path=ICON):
    """App icon from ui.app_icon, all sizes; .icns on macOS."""
    sys.path.insert(0, ED)
    import ui
    if path.endswith('.icns'):   # Without a Tk window; Linux/Mac builds run headless
        ui.app_icon(512).save(path, format='ICNS')
        return
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    ui.setup(root, 1.0)
    ui.save_app_icon(path)
    root.destroy()


def make_text_icon(text, path):
    """Text icon, e.g. HR for diagnostics."""
    from PIL import Image, ImageDraw, ImageFont
    S = 256
    im = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 8, S - 8, S - 8), radius=52, fill='#141519', outline='#1fc9a1', width=10)
    font = bold_font(104)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((S - w) / 2 - box[0], (S - h) / 2 - box[1]), text, font=font, fill='#1fc9a1')
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def build_diagnose():
    """HeadRush_Bridge_Diagnose.exe: console werkzeuge/geraet_diagnose.py for
    Pedalboard/Gigboard testers, bundling diagnose_pfade.txt."""
    work = os.path.join(OUT, 'build_diag')
    os.makedirs(work, exist_ok=True)
    icon = os.path.join(work, 'diagnose.ico')     # HR (HeadRush) for all three devices
    make_text_icon('HR', icon)
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--console',
           '--name', 'HeadRush_Bridge_Diagnose', '--icon', icon,
           '--distpath', OUT, '--workpath', work, '--specpath', work,
           '--add-data', os.path.join(here, 'diagnose_pfade.txt') + os.pathsep + '.',
           '--paths', ED, '--hidden-import', 'mido.backends.rtmidi',
           os.path.join(here, 'geraet_diagnose.py')]
    subprocess.run(cmd, check=True, cwd=ED)
    shutil.rmtree(work, ignore_errors=True)
    exe = os.path.join(OUT, 'HeadRush_Bridge_Diagnose.exe')
    print('Done:', exe, '%.1f MB' % (os.path.getsize(exe) / 1e6))


def main():
    if sys.argv[1:] == ['diagnose']:
        return build_diagnose()
    work = os.path.join(OUT, 'build')
    os.makedirs(work, exist_ok=True)
    mac = sys.platform == 'darwin'
    if os.name == 'nt':
        make_icon()
    icon = ICON
    if mac:   # macOS: .app/onedir with .icns; bundle repository mxedit.ico as data too
        icon = os.path.join(work, 'mxedit.icns')
        make_icon(icon)
    sep = os.pathsep
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
           '--name', 'MX-Edit', '--icon', icon,
           '--distpath', OUT, '--workpath', work, '--specpath', work,
           '--add-data', os.path.join(ED, 'catalog.json') + sep + '.',
           '--add-data', os.path.join(ED, 'modellseiten.json') + sep + '.',
           '--add-data', ICON + sep + '.',
           '--hidden-import', 'mido.backends.rtmidi',
           '--hidden-import', 'PIL._tkinter_finder']   # Linux: otherwise ImageTk cannot find Tk
    cmd += ['--osx-bundle-identifier', 'com.tict4x.mxedit'] if mac else ['--onefile']
    cmd.append(os.path.join(ED, 'mx5editor.py'))
    subprocess.run(cmd, check=True, cwd=ED)
    shutil.rmtree(work, ignore_errors=True)
    exe = os.path.join(OUT, 'MX-Edit' + ('.exe' if os.name == 'nt' else '.app' if mac else ''))
    print('Done:', exe, '%.1f MB' % (os.path.getsize(exe) / 1e6) if os.path.isfile(exe) else '')


if __name__ == '__main__':
    main()
