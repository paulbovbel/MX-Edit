#!/usr/bin/env python3
"""Root filesystem changes shared by build.py (Linux/debugfs) and patcher.py
(cross-platform, pure Python via ext4.py). Messages are shown to end users.

plan(cat) reads original files via cat(path) -> bytes (b'' means missing) and
returns {target_path: (content_bytes, mode_int)}. VERSION is defined only here
and replaces @VERSION@ in every file from mod/."""
import os, re

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(HERE, 'mod')
BIN = os.path.join(HERE, 'bin')

VERSION = '0.7'

# Evil's USB-MIDI gadget name (assignment QML filename;
# spaces become underscores, as in 'HG04_Control_Surface_MIDI_1'; confirmed in 0.1).
NAME = 'f_midi'

# Firmware 2.7 models (Andere_Geraete/BEFUND.txt: same startup scripts, kernel,
# and factory DB; only the USB product name differs for the bridge). Keys are FIT
# 'compatible' values without 'inmusic,'. device is the display name (@DEVICE@ in mod/).
# tested means verified on hardware and included in the main release; beta means
# the bridge runs according to tester logs, but editor support remains beta
# (a beta notice replaces the risk checkbox). recovery is the power-on footswitch recovery procedure.
# Pedalboard joined the main release on 2026-10-19 (bridge 0.7 logs from Oct 11/13).
# url: Windows updater (7z-SFX); mac_url: macOS updater (ZIP with .app, same Update.img;
# verified on MX5 on 2026-11-01; URLs from the NAM mod's app/main.c).
CDN = 'https://cdn.inmusicbrands.com/HeadRush/FW/Aug24_Firmware_Updates/'
MODELS = {
    'hg04': dict(key='mx5', device='MX5', tested=True, beta=False, recovery='hold footswitches 1 and 2 while powering on',
                 sha1='82b5639ceaba94d3c07b8880694d5de2e21b9f43',
                 url=CDN + 'MX5%20v2.7/Windows%20Updater/HeadRush%20MX5%202.7%20Firmware%20Updater%20-%20Win.exe.zip',
                 mac_url=CDN + 'MX5%20v2.7/MacOS%20Updater/HeadRush%20MX5%202.7%20Firmware%20Updater%20-%20Mac.zip'),
    'mg01': dict(key='pedalboard', device='Pedalboard', tested=True, beta=True,
                 recovery='hold footswitches 1 and 8 while powering on',
                 sha1='482b46433688b8364f5f70281741f21841f1ffe5',
                 url=CDN + 'Pedalboard%20v2.7/Windows%20Updater/HeadRush%20Pedalboard%202.7%20Firmware%20Updater%20-%20Win.exe.zip',
                 mac_url=CDN + 'Pedalboard%20v2.7/MacOS%20Updater/HeadRush%20Pedalboard%202.7%20Firmware%20Updater%20-%20Mac.zip'),
    'hg02': dict(key='gigboard', device='Gigboard', tested=False, beta=False, recovery=None,
                 sha1='9c081790b57e749c5f4a5fc64d914c2a04009869',
                 url=CDN + 'Gigboard%20v2.7/Windows%20Updater/HeadRush%20Gigboard%202.7%20Firmware%20Updater%20-%20Win.exe.zip',
                 mac_url=CDN + 'Gigboard%20v2.7/MacOS%20Updater/HeadRush%20Gigboard%202.7%20Firmware%20Updater%20-%20Mac.zip'),
}


# Testing release (MX-Edit-Testing): testing.flag next to this module (bundled by
# tools/patcher_exe.py --testing) enables unverified models (Gigboard). Otherwise,
# the patcher offers only verified models (MX5, Pedalboard beta) and rejects other
# firmware with an explanatory message.
TESTING = os.path.exists(os.path.join(HERE, 'testing.flag'))
TESTING_URL = 'https://github.com/TicT4x/MX-Edit-Testing'
# User-facing name: the testing release targets all three devices, so it uses
# 'HeadRush Bridge' for windows, updater text and folders. Internal names (ping 'MX5Bridge-<v>',
# mx5bridge-*.sh scripts, device folders) remain unchanged protocol identifiers.
BRAND = 'HeadRush Bridge' if TESTING else 'MX5 Bridge'          # Display text and window title
LABEL = 'HeadRush Bridge' if TESTING else 'MX5Bridge'           # Updater: 'Install <LABEL> 0.7'
FILE_TAG = 'HeadRush_Bridge' if TESTING else 'MX5Bridge'        # Folder/file names


def label(device='MX5'):
    """Device LABEL: non-MX5 devices use 'HeadRush Bridge' even in the main release
    (Pedalboard beta); MX5 keeps the exact original 'MX5Bridge' identifier."""
    return LABEL if device == 'MX5' else 'HeadRush Bridge'


def file_tag(device='MX5'):
    """Device FILE_TAG (see label)."""
    return FILE_TAG if device == 'MX5' else 'HeadRush_Bridge'


def offered():
    """Models supported by this patcher (testing release: all)."""
    return [dict(m, compatible=c) for c, m in MODELS.items() if m['tested'] or TESTING]


def model(key):
    """Look up a model by key ('mx5', 'pedalboard', 'gigboard') or compatible ('hg04', ...)."""
    for compat, m in MODELS.items():
        if key in (compat, m['key']):
            return dict(m, compatible=compat)
    raise PatchError('Unknown model "%s" (known: %s)' % (key, ', '.join(m['key'] for m in MODELS.values())))


def model_of_fit(compatible):
    """Map a FIT header compatible string (e.g. 'inmusic,mg01') to a model."""
    c = compatible.split(',')[-1].strip().lower()
    if c not in MODELS:
        raise PatchError('This firmware is for an unsupported device ("%s"). Supported: HeadRush %s.'
                         % (compatible, ', '.join(m['device'] for m in MODELS.values())))
    return model(c)

MIDI_BLOCK = '''    ### MX5 Bridge: USB-MIDI function (noncritical) ###
    { ( mkdir -p functions/midi.mx5bridge &&
        echo 1 >functions/midi.mx5bridge/in_ports &&
        echo 1 >functions/midi.mx5bridge/out_ports &&
        ln -s functions/midi.mx5bridge configs/c.1 ) ||
      logger -t mx5bridge "Could not create USB-MIDI function"; true; } &&
'''
ENABLE = '    ### Enable the gadget:\n    echo ff580000.usb >UDC\n)\n'
HOOK = '/usr/Evil/Scripts/mx5bridge-usb.sh'
DBHOOK = '/usr/Evil/Scripts/mx5bridge-db.sh'
SQLITE_TARGET = '/usr/Evil/Scripts/mx5bridge-sqlite3'
DAEMON = ('# MX5 Bridge: USB-MIDI and database services (noncritical)\n'
          'setsid %s daemon >/dev/null 2>&1 </dev/null &\n'
          'setsid %s daemon >/dev/null 2>&1 </dev/null &\n\n' % (HOOK, DBHOOK))


class PatchError(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise PatchError(msg)


def mod_text(name, device='MX5'):
    """Read a file from mod/ and substitute the version and device name."""
    text = open(os.path.join(MOD, name), encoding='ascii').read()
    text = text.replace('@VERSION@', VERSION).replace('@DEVICE@', device)
    assert '@VERSION@' not in text and '@DEVICE@' not in text
    return text


def patch_start(text):
    need(text.count(ENABLE) == 1, 'USB start script has an unexpected layout')
    need('mx5bridge' not in text, 'USB start script is already patched (MX5 Bridge already installed?)')
    need('ln -s functions/uac2_az01.otg0 configs/c.1 &&\n' + ENABLE in text, 'USB start script has an unexpected layout')
    mp = 'modprobe configfs &&\n'
    need(text.count(mp) == 1, 'USB start script has an unexpected layout')
    text = text.replace(mp, '%s evil-begin audio\n' % HOOK + mp)
    return text.replace(ENABLE, MIDI_BLOCK + ENABLE)


def patch_stop(text):
    first = '(\n    cd /sys/kernel/config/usb_gadget/g1 &&\n'
    uac = '    rm /sys/kernel/config/usb_gadget/g1/configs/c.1/uac2_az01.otg0\n'
    cfg = '    rmdir /sys/kernel/config/usb_gadget/g1/configs/c.1\n'
    for s in (first, uac, cfg):
        need(text.count(s) == 1, 'USB stop script has an unexpected layout: %r' % s)
    need(text.endswith(')\n'), 'USB stop script has an unexpected layout')
    text = text.replace(uac, uac + '    rm /sys/kernel/config/usb_gadget/g1/configs/c.1/midi.mx5bridge 2>/dev/null\n')
    text = text.replace(cfg, cfg + '    rmdir /sys/kernel/config/usb_gadget/g1/functions/midi.mx5bridge 2>/dev/null\n')
    return text + '%s evil-end\n' % HOOK


def patch_ms_setup(text):
    dv = '. /usr/Evil/Scripts/def_vars\n'
    need(text.count(dv) == 1 and 'mx5bridge' not in text, 'setup-mass-storage.sh has an unexpected layout')
    return text.replace(dv, dv + '\n%s evil-begin storage\n' % HOOK)


def patch_ms_remove(text):
    need('mx5bridge' not in text and text.endswith('fi\n'), 'remove-mass-storage.sh has an unexpected layout')
    return text + '%s evil-end\n' % HOOK


# App launch in the startup loop; NAM prepends 'env LD_PRELOAD=... NAM_HOOK_SLOT_..._ADDR=...'
# (core/launcher_script.c) and a comment block before 'while [ 1 ]'.
EVIL_START = '\tsystemd-inhibit --what=handle-power-key /usr/Evil/Evil\n'
EVIL_RUN = re.compile(r'\tsystemd-inhibit --what=handle-power-key '
                      r'(env LD_PRELOAD=/usr/Evil/libnam_preload\.so'
                      r'(?: NAM_HOOK_SLOT_GONK(?:_V2)?_ADDR=0x[0-9a-f]+)+ )?'
                      r'/usr/Evil/Evil\n\texitCode=\$\?\n')
NAM_BLOCK = '# --- NAM mod ---\n'
NAM_LIBS = ('/usr/Evil/libnam_preload.so', '/usr/Evil/libnam_hook.so')
NAM_INFO = '/usr/Evil/Scripts/mx5bridge-nam.txt'


def patch_evil(text):
    """Startup loop: prestart, postexit and restart marker. With NAM installed,
    use its launch line only if 'mx5bridge-db.sh nam' allows it; otherwise launch
    normally. Return (text, NAM launch line or None)."""
    need('mx5bridge' not in text, 'start script "evil" is already patched (MX5 Bridge already installed?)')
    runs = list(EVIL_RUN.finditer(text))
    code = [l for l in text.split('\n') if not l.lstrip().startswith('#')]   # The NAM block mentions it.
    need(len(runs) == 1 and sum('systemd-inhibit' in l for l in code) == 1, 'start script "evil" has an unexpected layout')
    m = runs[0]
    need(text[:m.start()].endswith('while [ 1 ]\ndo\n'), 'start script "evil" has an unexpected layout')
    nam = m.group(0).split('\n')[0] + '\n' if m.group(1) else None
    need(bool(nam) == (NAM_BLOCK in text), 'NAM start line and NAM comment block do not match')
    start = EVIL_START
    if nam:
        start = ('\t### NAM mod (github.com/lolgab/headrush-nam-mod), only when enabled\n'
                 '\tif %s nam; then\n\t%s\telse\n\t%s\tfi\n' % (DBHOOK, nam, EVIL_START))
    patch = ('\t### MX5 Bridge: requested recovery, crash counter, restart marker\n'
             '\t%s prestart\n'
             '\tmx5bStart=$(date +%%s)\n'
             '%s'
             '\texitCode=$?\n'
             '\t%s postexit $exitCode $mx5bStart\n'
             '\tif [ -e /tmp/mx5bridge/restart ]; then\n'
             '\t\trm -f /tmp/mx5bridge/restart\n'
             '\t\tsleep 2\n'
             '\t\tcontinue\n'
             '\tfi\n' % (DBHOOK, start, DBHOOK))
    return text[:m.start()] + patch + text[m.end():], nam


def patch_runevil(text):
    anchor = 'log info "Starting application ..."\n'
    need(text.count(anchor) == 1 and 'mx5bridge' not in text, 'runevil has an unexpected layout')
    return text.replace(anchor, DAEMON + anchor)


def sqlite_binary():
    p = os.path.join(BIN, 'sqlite3')
    need(os.path.exists(p), 'bin/sqlite3 is missing - run tools/build-sqlite3.sh first')
    data = open(p, 'rb').read()
    need(data[:4] == b'\x7fELF' and data[4] == 1 and data[18] == 0x28, 'bin/sqlite3 is not a 32-bit ARM binary')
    return data


def device_of(start_script):
    """Extract the device name from the original startup script's USB audio product
    name ('HeadRush MX5' / 'HeadRush Pedalboard' / 'HeadRush GigBoard')."""
    m = re.findall(r"echo 'HeadRush ([A-Za-z0-9]+)' >strings/0x409/product", start_script)
    need(len(m) == 1, 'USB start script has an unexpected layout (product name not found)')
    for mm in MODELS.values():
        if mm['device'].lower() == m[0].lower():
            return mm['device']
    raise PatchError('This firmware is for an unsupported device (HeadRush %s)' % m[0])


def plan(cat, nam_info=None, log=print, device=None):
    """Return ({target_path: (bytes, mode)}, NAM launch line or None).
    cat(path) -> bytes, b'' means missing. nam_info: NAM source information
    (tools/nam-mod.sh), otherwise 'unknown'. device: expected device ('MX5',
    'Pedalboard', 'Gigboard'); None detects it from the startup script."""
    text = lambda p: cat(p).decode()
    found = device_of(text('/usr/Evil/Scripts/usb-otg-audio-start.sh'))
    need(device in (None, found), 'This firmware is for the HeadRush %s, not the %s' % (found, device))
    device = found
    files = {}
    enc = lambda s: s.encode('ascii') if isinstance(s, str) else s
    files['/usr/Evil/Scripts/usb-otg-audio-start.sh'] = (enc(patch_start(text('/usr/Evil/Scripts/usb-otg-audio-start.sh'))), 0o100755)
    files['/usr/Evil/Scripts/usb-otg-audio-stop.sh'] = (enc(patch_stop(text('/usr/Evil/Scripts/usb-otg-audio-stop.sh'))), 0o100755)
    files['/usr/Evil/Scripts/mx5bridge-diag.sh'] = (enc(mod_text('mx5bridge-diag.sh')), 0o100755)
    files['/usr/Evil/Scripts/mx5bridge-usb.sh'] = (enc(mod_text('mx5bridge-usb.sh', device)), 0o100755)
    files[DBHOOK] = (enc(mod_text('mx5bridge-db.sh')), 0o100755)
    files[SQLITE_TARGET] = (sqlite_binary(), 0o100755)
    for target, fn in (('/usr/Evil/Scripts/setup-mass-storage.sh', patch_ms_setup),
                       ('/usr/Evil/Scripts/remove-mass-storage.sh', patch_ms_remove),
                       ('/usr/Evil/Scripts/runevil', patch_runevil)):
        files[target] = (fn(text(target)).encode(), 0o100755)
    evil, nam = patch_evil(text('/usr/Evil/Scripts/evil'))
    files['/usr/Evil/Scripts/evil'] = (evil.encode(), 0o100755)

    # Enable NAM only if already present in the input (tools/nam-mod.sh or NAM GUI).
    libs = [len(cat(p)) for p in NAM_LIBS]
    need(all(libs) == bool(nam) and any(libs) == bool(nam),
         'NAM libraries and NAM start line do not match: %s' % libs)
    if nam:
        log('NAM mod found: ' + nam.strip())
        info = nam_info or 'nam_mod_ref=unknown\n'
        if 'nam_instanzen=' not in info:   # E.g. a NAM GUI updater: extract it from the launch line.
            info += 'nam_instanzen=%d\n' % (4 if 'NAM_HOOK_SLOT_GONK_V2_ADDR=' in nam else 2)
        info += 'startzeile=%s' % nam.strip().replace('systemd-inhibit --what=handle-power-key ', '') + '\n'
        files[NAM_INFO] = (info.encode('ascii'), 0o100644)
    for kind in ('Device', 'Assignments'):
        tpl = mod_text('Bridge_%s.qml.in' % kind)
        if kind == 'Device':
            assert 'bridgeVersion: "MX5Bridge-%s"' % VERSION in tpl
        files['/usr/Evil/Assignments/%s_%s.qml' % (NAME, kind)] = (enc(tpl.replace('@NAME@', NAME)), 0o100644)
    # New files must not overwrite original files; scripts require LF endings.
    for target, (data, mode) in files.items():
        if target.endswith(('.sh', '.qml')) or target.endswith(('runevil', '/evil')):
            need(b'\r' not in data, 'CRLF in ' + target)
        if target in (DBHOOK, SQLITE_TARGET, NAM_INFO) or '/Assignments/' in target:
            need(cat(target) == b'', 'already exists: ' + target)
    return files, nam
