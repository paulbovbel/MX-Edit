"""Minimal pure-Python 7z extractor (standard library).

Windows 10/older Windows 11 tar.exe (libarchive 3.5) lacks liblzma and cannot
extract the HeadRush updater (LZMA2 + BCJ2). Supports Copy, LZMA, LZMA2,
BCJ2 (custom decoder), BCJ x86/ARM/ARMT/PPC/SPARC/IA64 and Delta via liblzma,
encoded headers (kEncodedHeader), and CRC verification. No AES or split archives.

    extract(archive_bytes, dest) -> list of extracted filenames
"""
import lzma
import os
import re
import zlib

SIG = b"7z\xbc\xaf\x27\x1c"


class Error(Exception):
    pass


# ---- Read header structures ----
class R:
    def __init__(self, d):
        self.d, self.p = d, 0

    def byte(self):
        b = self.d[self.p]
        self.p += 1
        return b

    def bytes(self, n):
        v = self.d[self.p:self.p + n]
        if len(v) != n:
            raise Error('7z header too short')
        self.p += n
        return v

    def num(self):
        first = self.byte()
        mask, v = 0x80, 0
        for i in range(8):
            if not first & mask:
                return v | ((first & (mask - 1)) << (8 * i))
            v |= self.byte() << (8 * i)
            mask >>= 1
        return v

    def u32(self):
        return int.from_bytes(self.bytes(4), 'little')

    def bits(self, n):
        out, b, m = [], 0, 0
        for _ in range(n):
            if not m:
                b, m = self.byte(), 0x80
            out.append(bool(b & m))
            m >>= 1
        return out

    def defined(self, n):
        return [True] * n if self.byte() else self.bits(n)

    def digests(self, n):
        d = self.defined(n)
        return [self.u32() if x else None for x in d]


class Folder:
    def __init__(self):
        self.coders = []          # (id, n_in, n_out, props)
        self.bind = []            # (in_index, out_index)
        self.packed = []          # Input indices of packed streams
        self.sizes = []           # Sizes of all output streams
        self.crc = None

    def main_out(self):
        bound = {o for _, o in self.bind}
        n = sum(c[2] for c in self.coders)
        return [o for o in range(n) if o not in bound][0]

    def size(self):
        return self.sizes[self.main_out()]


def read_folder(r):
    f = Folder()
    for _ in range(r.num()):
        flag = r.byte()
        if flag & 0x80:
            raise Error('7z: alternative coders are not supported')
        cid = r.bytes(flag & 0x0F)
        n_in, n_out = (r.num(), r.num()) if flag & 0x10 else (1, 1)
        props = r.bytes(r.num()) if flag & 0x20 else b''
        f.coders.append((cid, n_in, n_out, props))
    t_in = sum(c[1] for c in f.coders)
    t_out = sum(c[2] for c in f.coders)
    f.bind = [(r.num(), r.num()) for _ in range(t_out - 1)]
    n_packed = t_in - len(f.bind)
    if n_packed == 1:
        bound = {i for i, _ in f.bind}
        f.packed = [i for i in range(t_in) if i not in bound]
    else:
        f.packed = [r.num() for _ in range(n_packed)]
    return f


def read_streams(r):
    """StreamsInfo -> (pack_pos, pack_sizes, folders, substreams[(size, crc)] per folder)"""
    pack_pos, pack_sizes, folders, subs = 0, [], [], None
    while True:
        t = r.num()
        if t == 0:
            break
        if t == 6:                                    # PackInfo
            pack_pos = r.num()
            n = r.num()
            while True:
                u = r.num()
                if u == 0:
                    break
                if u == 9:
                    pack_sizes = [r.num() for _ in range(n)]
                elif u == 10:
                    r.digests(n)
                else:
                    raise Error('7z: unknown PackInfo property %d' % u)
        elif t == 7:                                  # UnpackInfo
            if r.num() != 11:
                raise Error('7z: expected Folder')
            n = r.num()
            if r.byte():
                raise Error('7z: external folders are not supported')
            folders = [read_folder(r) for _ in range(n)]
            if r.num() != 12:
                raise Error('7z: expected CodersUnpackSize')
            for f in folders:
                f.sizes = [r.num() for _ in range(sum(c[2] for c in f.coders))]
            while True:
                u = r.num()
                if u == 0:
                    break
                if u == 10:
                    for f, c in zip(folders, r.digests(len(folders))):
                        f.crc = c
                else:
                    raise Error('7z: unknown UnpackInfo property %d' % u)
        elif t == 8:                                  # SubStreamsInfo
            counts = [1] * len(folders)
            sizes = None
            u = r.num()
            if u == 13:
                counts = [r.num() for _ in folders]
                u = r.num()
            if u == 9:
                sizes = []
                for f, c in zip(folders, counts):
                    if c:
                        part = [r.num() for _ in range(c - 1)]
                        sizes.append(part + [f.size() - sum(part)])
                    else:
                        sizes.append([])
                u = r.num()
            if sizes is None:
                sizes = [[f.size()] if c else [] for f, c in zip(folders, counts)]
            crcs = [[None] * c for c in counts]
            need = [(fi, k) for fi, (f, c) in enumerate(zip(folders, counts))
                    for k in range(c) if not (c == 1 and f.crc is not None)]
            for fi, (f, c) in enumerate(zip(folders, counts)):
                if c == 1 and f.crc is not None:
                    crcs[fi][0] = f.crc
            while u != 0:
                if u == 10:
                    for (fi, k), d in zip(need, r.digests(len(need))):
                        crcs[fi][k] = d
                else:
                    raise Error('7z: unknown SubStreams property %d' % u)
                u = r.num()
            subs = [list(zip(s, c)) for s, c in zip(sizes, crcs)]
        else:
            raise Error('7z: unknown StreamsInfo property %d' % t)
    if subs is None:
        subs = [[(f.size(), f.crc)] for f in folders]
    return pack_pos, pack_sizes, folders, subs


# ---- Decoding ----
def _lzma_raw(data, filt, size):
    d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filt])
    out = d.decompress(data, size)
    if len(out) < size:
        raise Error('7z: incomplete data (%d of %d bytes)' % (len(out), size))
    return out


def _lzma1(data, props, size):
    d = props[0]
    lc, d = d % 9, d // 9
    lp, pb = d % 5, d // 5
    return _lzma_raw(data, {'id': lzma.FILTER_LZMA1, 'lc': lc, 'lp': lp, 'pb': pb,
                            'dict_size': int.from_bytes(props[1:5], 'little')}, size)


def _lzma2(data, props, size):
    p = props[0] & 0x3F
    dic = 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)
    return _lzma_raw(data, {'id': lzma.FILTER_LZMA2, 'dict_size': dic}, size)


def _filter(data, filt):
    """Decode a single liblzma filter (BCJ/Delta): wrap unchanged data in LZMA2 and
    decode with Filter+LZMA2; liblzma requires LZMA at the end of a filter chain."""
    lz = {'id': lzma.FILTER_LZMA2, 'preset': 0}
    packed = lzma.compress(data, lzma.FORMAT_RAW, filters=[lz])
    return lzma.decompress(packed, lzma.FORMAT_RAW, filters=[filt, lz])


_JUMP = re.compile(rb'[\xe8\xe9]|(?<=\x0f)[\x80-\x8f]')


def bcj2(main, call, jump, rc, size):
    out = bytearray()
    probs = [1024] * 258
    rng, code, rp = 0xFFFFFFFF, 0, 0
    for _ in range(5):
        code = (code << 8) | rc[rp]
        rp += 1
    ip, ci, ji = 0, 0, 0
    n = len(main)
    while len(out) < size and ip < n:
        # Copy through the next jump instruction (previous byte is the last output byte,
        # including after an inserted address)
        b = main[ip]
        prev = out[-1] if out else 0
        if (b & 0xFE) == 0xE8 or (prev == 0x0F and (b & 0xF0) == 0x80):
            j = ip
        else:
            m = _JUMP.search(main, ip + 1)
            j = m.start() if m else n
        end = min(j + 1, n, ip + size - len(out))
        out += main[ip:end]
        if end != j + 1 or len(out) >= size:
            break
        b = main[j]
        ip = j + 1
        if b == 0xE8:
            pi = out[-2] if len(out) >= 2 else 0
        elif b == 0xE9:
            pi = 256
        else:
            pi = 257
        t = probs[pi]
        bound = (rng >> 11) * t
        if code < bound:
            rng = bound
            probs[pi] = t + ((2048 - t) >> 5)
            bit = 0
        else:
            rng -= bound
            code -= bound
            probs[pi] = t - (t >> 5)
            bit = 1
        if rng < 0x1000000:
            rng = (rng << 8) & 0xFFFFFFFF
            code = ((code << 8) | rc[rp]) & 0xFFFFFFFF
            rp += 1
        if not bit:
            continue
        if b == 0xE8:
            v = call[ci:ci + 4]
            ci += 4
        else:
            v = jump[ji:ji + 4]
            ji += 4
        dest = (int.from_bytes(v, 'big') - (len(out) + 4)) & 0xFFFFFFFF
        out += dest.to_bytes(4, 'little')[:size - len(out)]
    return bytes(out[:size])


_FILTERS = {
    b'\x03\x03\x01\x03': lzma.FILTER_X86, b'\x03\x03\x02\x05': lzma.FILTER_POWERPC,
    b'\x03\x03\x04\x01': lzma.FILTER_IA64, b'\x03\x03\x05\x01': lzma.FILTER_ARM,
    b'\x03\x03\x07\x01': lzma.FILTER_ARMTHUMB, b'\x03\x03\x08\x05': lzma.FILTER_SPARC,
}


def run_coder(cid, props, ins, size):
    if cid == b'\x00':
        return ins[0][:size]
    if cid == b'\x21':
        return _lzma2(ins[0], props, size)
    if cid == b'\x03\x01\x01':
        return _lzma1(ins[0], props, size)
    if cid == b'\x03\x03\x01\x1b':
        return bcj2(*ins, size)
    if cid in _FILTERS:
        return _filter(ins[0], {'id': _FILTERS[cid]})[:size]
    if cid == b'\x03':
        return _filter(ins[0], {'id': lzma.FILTER_DELTA, 'dist': props[0] + 1})[:size]
    if cid.startswith(b'\x06\xf1\x07'):
        raise Error('7z: encrypted archive')
    raise Error('7z: method %s is not supported' % cid.hex())


def decode_folder(f, packs):
    """packs: packed streams in f.packed order"""
    in_owner, out_owner = [], []
    for ci, c in enumerate(f.coders):
        in_owner += [ci] * c[1]
        out_owner += [ci] * c[2]

    def out_stream(o):
        ci = out_owner[o]
        cid, n_in, n_out, props = f.coders[ci]
        if n_out != 1:
            raise Error('7z: coders with multiple outputs are not supported')
        first = in_owner.index(ci)
        ins = []
        for i in range(first, first + n_in):
            bp = [ob for ib, ob in f.bind if ib == i]
            ins.append(out_stream(bp[0]) if bp else packs[f.packed.index(i)])
        return run_coder(cid, props, ins, f.sizes[o])

    data = out_stream(f.main_out())
    if f.crc is not None and zlib.crc32(data) != f.crc:
        raise Error('7z: CRC error')
    return data


def _folder_data(arc, pack_pos, pack_sizes, folders):
    pos = 32 + pack_pos
    k = 0
    for f in folders:
        packs = []
        for _ in f.packed:
            packs.append(arc[pos:pos + pack_sizes[k]])
            pos += pack_sizes[k]
            k += 1
        yield f, packs


def read_header(arc):
    if arc[:6] != SIG:
        raise Error('missing 7z signature')
    if zlib.crc32(arc[12:32]) != int.from_bytes(arc[8:12], 'little'):
        raise Error('7z: corrupt start header')
    off = int.from_bytes(arc[12:20], 'little')
    size = int.from_bytes(arc[20:28], 'little')
    hdr = arc[32 + off:32 + off + size]
    if len(hdr) != size or zlib.crc32(hdr) != int.from_bytes(arc[28:32], 'little'):
        raise Error('7z: incomplete or corrupt archive')
    while True:
        r = R(hdr)
        t = r.num()
        if t == 1:
            return r
        if t != 0x17:
            raise Error('7z: unknown header type %d' % t)
        pack_pos, pack_sizes, folders, _ = read_streams(r)
        f, packs = next(_folder_data(arc, pack_pos, pack_sizes, folders))
        hdr = decode_folder(f, packs)


def extract(arc, dest):
    r = read_header(arc)
    streams = ([], [], [], [])
    names, empty, empty_file, attrs = [], [], [], []
    while True:
        t = r.num()
        if t == 0:
            break
        if t == 2:                                    # ArchiveProperties
            while r.num():
                r.bytes(r.num())
        elif t == 3:
            raise Error('7z: AdditionalStreams are not supported')
        elif t == 4:
            streams = read_streams(r)
        elif t == 5:                                  # FilesInfo
            n = r.num()
            empty = [False] * n
            while True:
                u = r.num()
                if u == 0:
                    break
                size = r.num()
                end = r.p + size
                if u == 0x0E:
                    empty = r.bits(n)
                elif u == 0x0F:
                    empty_file = r.bits(sum(empty))
                elif u == 0x11:
                    if r.byte():
                        raise Error('7z: external names are not supported')
                    raw = r.d[r.p:end].decode('utf-16-le')
                    names = raw.split('\0')[:n]
                r.p = end
        else:
            raise Error('7z: unknown header property %d' % t)
    pack_pos, pack_sizes, folders, subs = streams
    files = [(sz, crc) for s in subs for sz, crc in s]
    written = []
    fi = iter(files)
    ei = 0
    datas = _folder_data(arc, pack_pos, pack_sizes, folders)
    cur, cur_pos = b'', 0
    queue = [(f, s) for f, s in zip(folders, subs)]
    qi = 0
    for idx, name in enumerate(names):
        path = os.path.join(dest, *name.replace('\\', '/').split('/'))
        if '..' in name.replace('\\', '/').split('/') or os.path.isabs(name):
            raise Error('7z: unsafe path %s' % name)
        if empty[idx]:
            is_file = ei < len(empty_file) and empty_file[ei]
            ei += 1
            if is_file:
                os.makedirs(os.path.dirname(path) or dest, exist_ok=True)
                open(path, 'wb').close()
            else:
                os.makedirs(path, exist_ok=True)
            continue
        while cur_pos >= len(cur):
            if qi >= len(queue):
                raise Error('7z: more files than data')
            f, packs = next(datas)
            cur, cur_pos = decode_folder(f, packs), 0
            qi += 1
        sz, crc = next(fi)
        data = cur[cur_pos:cur_pos + sz]
        cur_pos += sz
        if crc is not None and zlib.crc32(data) != crc:
            raise Error('7z: CRC error in %s' % name)
        os.makedirs(os.path.dirname(path) or dest, exist_ok=True)
        with open(path, 'wb') as out:
            out.write(data)
        written.append(name)
    return written
