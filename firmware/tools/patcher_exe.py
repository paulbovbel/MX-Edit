#!/usr/bin/env python3
"""Build a single-file patcher executable; users do not need Python.

Usage: python tools/patcher_exe.py (requires: pip install pyinstaller pillow)
Output: <source>/exe/MX5Bridge_Patcher.exe; intermediates in exe/build are removed.
macOS: exe/MX5Bridge_Patcher.app; Linux: exe/MX5Bridge_Patcher (single file).
PyInstaller builds only for its host OS; .github/workflows/build.yml builds
Mac/Linux releases on GitHub.

mod/ and bin/sqlite3 are bundled as data (bridgepatch reads sys._MEIPASS).
patcher.py places downloads/output next to the executable (BASE). GUI entry:
patcher_gui.py; console entry: python patcher.py. This script generates the
icon: turquoise 'MX5' with a downward arrow."""
import os, shutil, subprocess, sys

Q = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(Q, 'exe')
ICON = os.path.join(Q, 'patcher.ico')    # Also used when running patcher_gui.py from source


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


def make_icon(text='MX5', path=ICON):
    from PIL import Image, ImageDraw, ImageFont
    S = 256
    im = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 8, S - 8, S - 8), radius=52, fill='#141519', outline='#1fc9a1', width=10)
    font = bold_font(84)
    box = d.textbbox((0, 0), text, font=font)
    w = box[2] - box[0]
    d.text(((S - w) / 2 - box[0], 40 - box[1]), text, font=font, fill='#1fc9a1')
    # Downward arrow (firmware installation)
    d.rectangle((S / 2 - 14, 140, S / 2 + 14, 180), fill='#e8e8ea')
    d.polygon([(S / 2 - 40, 178), (S / 2 + 40, 178), (S / 2, 222)], fill='#e8e8ea')
    if path.endswith('.icns'):
        im.resize((512, 512), Image.LANCZOS).save(path, format='ICNS')
    else:
        im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main():
    """--testing: MX-Edit-Testing release (HeadRush_Bridge_Testing_Patcher.exe,
    with testing.flag; also offers Pedalboard and Gigboard)."""
    testing = '--testing' in sys.argv[1:]
    name = 'HeadRush_Bridge_Testing_Patcher' if testing else 'MX5Bridge_Patcher'
    os.makedirs(OUT, exist_ok=True)
    make_icon()
    work = os.path.join(OUT, 'build_' + name)
    os.makedirs(work, exist_ok=True)
    icon = ICON
    if testing:   # Testing release (all HeadRush devices): 'HR' instead of 'MX5', bundled as patcher.ico
        icon = os.path.join(work, 'patcher.ico')
        make_icon('HR', icon)
    sep = os.pathsep
    mac = sys.platform == 'darwin'
    if mac:   # macOS: .app (onedir) with .icns; Linux: single file; Windows: single .exe
        bundle_icon = os.path.join(work, 'patcher.icns')
        make_icon('HR' if testing else 'MX5', bundle_icon)
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
           '--name', name, '--icon', bundle_icon if mac else icon,
           '--distpath', OUT, '--workpath', work, '--specpath', work,
           '--add-data', os.path.join(Q, 'mod') + sep + 'mod',
           '--add-data', os.path.join(Q, 'bin', 'sqlite3') + sep + 'bin',
           '--add-data', icon + sep + '.']
    cmd += ['--osx-bundle-identifier', 'com.tict4x.mx5bridge.patcher'] if mac else ['--onefile']
    if testing:
        flag = os.path.join(work, 'testing.flag')
        open(flag, 'w').write('MX-Edit-Testing\n')
        cmd += ['--add-data', flag + sep + '.']
    cmd.append(os.path.join(Q, 'patcher_gui.py'))
    subprocess.run(cmd, check=True, cwd=Q)
    shutil.rmtree(work, ignore_errors=True)
    exe = os.path.join(OUT, name + ('.exe' if os.name == 'nt' else '.app' if mac else ''))
    print('Done:', exe, '%.1f MB' % (os.path.getsize(exe) / 1e6) if os.path.isfile(exe) else '')


if __name__ == '__main__':
    main()
