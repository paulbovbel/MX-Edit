#!/usr/bin/env python3
"""Build MX5 Bridge firmware from the original Update.img (HeadRush MX5 2.7).

Usage (Linux/WSL; requires debugfs, e2fsck, xz):
    python3 build.py <original Update.img> <output Update.img>
Run tools/build-sqlite3.sh once first to create bin/sqlite3.

Optional NAM mod (https://github.com/lolgab/headrush-nam-mod, NOT distributed
with the bridge): provide an Update.img already patched by tools/nam-mod.sh
(downloads NAM from GitHub and builds its tool) or the NAM GUI's updater.
build.py detects it and launches with NAM (LD_PRELOAD) only when enabled
('mx5bridge-db.sh nam', nam-on/nam-off actions). Without LD_PRELOAD, patched
Evil behaves like stock firmware: the NAM trampoline jumps to original Anxiety OD.

bridgepatch.py defines the changes and VERSION, shared with patcher.py,
which performs the same work without Linux.

0.7 vs 0.6: optional NAM support; status reports NAM state. After three consecutive
app crashes shortly after startup with NAM, the crash guard disables NAM first;
database recovery is attempted only afterwards.

0.5 vs 0.4: non-ASCII characters travel in both directions as escapes
(0x7F + four hex digits) instead of '?'; removed command 0x06 (RIG, never
functional); all script versions come from VERSION.
0.4 vs 0.3: database writes (0x09) and actions (0x0A:
status/backup/restore/restart); /usr/Evil/Scripts/evil invokes the service
before each app launch (recovery) and after each exit (crash counter),
and checks the bridge's restart marker."""
import hashlib, os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitpatch, bridgepatch

def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit('Command failed: %s' % cmd)
    return r.stdout + r.stderr

def debugfs_cat_bytes(img, path):
    return subprocess.run(['debugfs', '-R', 'cat "%s"' % path, img], capture_output=True).stdout

def main(stock_img, out_img):
    raw = open(stock_img, 'rb').read()
    print('Original Update.img sha1:', hashlib.sha1(raw).hexdigest())
    assert fitpatch.verify(raw)
    work = tempfile.mkdtemp(prefix='mx5b_')
    xz_in = os.path.join(work, 'rootfs.xz')
    img = os.path.join(work, 'rootfs.img')
    open(xz_in, 'wb').write(fitpatch.extract(raw, 'rootfs'))
    run('xz -dc "%s" > "%s"' % (xz_in, img), shell=True)

    info = stock_img + '.nam.txt'   # Source information from tools/nam-mod.sh, otherwise unknown
    nam_info = open(info, encoding='ascii').read() if os.path.exists(info) else None
    plan, nam = bridgepatch.plan(lambda p: debugfs_cat_bytes(img, p), nam_info)
    if not nam:
        print('without NAM mod')
    files = {}
    for i, (target, (data, mode)) in enumerate(plan.items()):
        local = os.path.join(work, 'f%02d' % i)
        open(local, 'wb').write(data)
        files[target] = (local, '0%o' % mode)

    # Write to the ext4 image
    cmds = []
    for target, (local, mode) in files.items():
        cmds.append('rm "%s"' % target)
        cmds.append('write "%s" "%s"' % (local, target))
        cmds.append('sif "%s" mode %s' % (target, mode))
        cmds.append('sif "%s" uid 0' % target)
        cmds.append('sif "%s" gid 0' % target)
    script = os.path.join(work, 'debugfs.cmd')
    open(script, 'w').write('\n'.join(cmds) + '\n')
    subprocess.run(['debugfs', '-w', '-f', script, img], capture_output=True)
    # Verify: read back content and check the filesystem
    for target, (local, mode) in files.items():
        got = debugfs_cat_bytes(img, target)
        assert got == open(local, 'rb').read(), 'Read-back verification failed: ' + target
        st = run(['debugfs', '-R', 'stat "%s"' % target, img])
        assert ('Mode:  %s' % mode[-4:]) in st and 'User:     0   Group:     0' in st, st[:200]
    fsck = subprocess.run(['e2fsck', '-fn', img], capture_output=True, text=True)
    print(fsck.stdout.strip().splitlines()[-1])
    assert fsck.returncode == 0, fsck.stdout + fsck.stderr
    print('%d files written and verified, e2fsck clean' % len(files))

    # Compress like the original: CRC32, 64 MiB dictionary, 128 MiB blocks
    xz_out = os.path.join(work, 'rootfs_new.xz')
    run('xz -zc -T2 --check=crc32 --block-size=134217728 --lzma2=preset=6e,dict=64MiB "%s" > "%s"'
        % (img, xz_out), shell=True)
    run('xz -dc "%s" | cmp - "%s"' % (xz_out, img), shell=True)
    new = fitpatch.replace_image(raw, 'rootfs', open(xz_out, 'rb').read())
    assert fitpatch.verify(new)
    # Splash images and header metadata must remain unchanged
    for im in ('splash', 'recoverysplash'):
        assert fitpatch.extract(new, im) == fitpatch.extract(raw, im)
    open(out_img, 'wb').write(new)
    print('Done:', out_img, len(new), 'Bytes, sha1', hashlib.sha1(new).hexdigest())
    shutil.rmtree(work)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
