#!/usr/bin/env python3
"""Deterministic synthetic GPS-positive fixture generation. No personal media, no network.
Run from the packet root; writes fixtures/ and FIXTURE-DIGESTS.json."""
import json, pathlib, shutil, struct, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from inventory import sha256_file

F = pathlib.Path(__file__).resolve().parent / 'fixtures'
F.mkdir(exist_ok=True)
LOC = '+19.0760+72.8777+005.0/'


def ff(args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + args, check=True)


def make():
    base = F / 'base.mp4'
    ff(['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(base)])
    ff(['-i', str(base), '-c', 'copy',
        '-metadata', 'location=' + LOC, '-metadata', 'location-eng=' + LOC,
        '-metadata', 'make=SyntheticCam', '-metadata', 'model=Unit1', str(F / 'f1-udta.mp4')])
    ff(['-i', str(base), '-c', 'copy', '-movflags', 'use_metadata_tags',
        '-metadata', 'com.apple.quicktime.location.ISO6709=' + LOC,
        '-metadata', 'com.apple.quicktime.make=SyntheticCam', str(F / 'f2-mdta.mov')])
    ff(['-i', str(base), '-c', 'copy',
        '-metadata', 'location=' + LOC,
        '-metadata:s:v', 'title=track with location', str(F / 'f3-pertrack.mp4')])
    ff(['-i', str(base), '-c', 'copy',
        '-metadata:s:v', 'rotate=90', '-metadata', 'location=' + LOC, str(F / 'f4-rotate.mp4')])
    inject_unknown_location_box(base, F / 'f5-unknown-box.mp4', b'loCX', None)
    # F6's payload is BINARY-packed coordinates, not literal text: signature and literal scans
    # must be truly negative for it, so refusal can only come from the unclassified-box rule.
    inject_unknown_location_box(base, F / 'f6-novel-box.mp4', b'zq9x',
                                struct.pack('>ddd', 19.0760, 72.8777, 5.0) + b'binloc')
    make_datatrack_fixture(base, F / 'f7-datatrack.mp4')
    make_sei_essence_fixture(F / 'f8-sei-essence.mp4')
    make_hevc_hlg_fixture(F / 'f9-hevc-hlg.mov')
    # Audio profile characterization fixtures (supported-profile gate; no profile decision here).
    ff(['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=3', '-an',
        '-c:v', 'libx264', '-bf', '0', '-pix_fmt', 'yuv420p',
        '-metadata', 'location=' + LOC, str(F / 'f10-noaudio.mp4')])
    ff(['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=880:duration=3',
        '-map', '0:v', '-map', '1:a', '-map', '2:a',
        '-c:v', 'libx264', '-bf', '0', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-metadata', 'location=' + LOC, str(F / 'f11-twoaudio.mp4')])
    ff(['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=3',
        '-itsoffset', '0.5', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx264', '-bf', '0', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-metadata', 'location=' + LOC, str(F / 'f12-avoffset.mp4')])


def make_datatrack_fixture(base, dst):
    """Append a timed-metadata track ('meta' handler, mebx sample entry) whose single sample
    carries coordinate text in mdat. Simulates iPhone-style data tracks."""
    src = pathlib.Path(base).read_bytes()

    def children(b, start, end):
        out, i = [], start
        while i + 8 <= end:
            size = struct.unpack_from('>I', b, i)[0]
            out.append((bytes(b[i + 4:i + 8]), i, size))
            i += size
        return out

    def find(b, path):
        start, end = 0, len(b)
        bx = None
        for want in path:
            bx = next((c for c in children(b, start, end) if c[0] == want), None)
            assert bx, want
            start, end = bx[1] + 8, bx[1] + bx[2]
        return bx

    def box(t, pl):
        return struct.pack('>I', 8 + len(pl)) + t + pl

    payload = b'+19.0760+072.8777+005.000000/ timed metadata sample'
    mdhd = box(b'mdhd', b'\x00\x00\x00\x00' + b'\x00' * 8 + struct.pack('>II', 1000, 3000)
               + b'\x55\xc4\x00\x00')
    hdlr = box(b'hdlr', b'\x00\x00\x00\x00' + b'\x00' * 4 + b'meta' + b'\x00' * 12
               + b'TimedMetadata\x00')
    nmhd = box(b'nmhd', b'\x00\x00\x00\x00')
    dref = box(b'dref', b'\x00\x00\x00\x00' + struct.pack('>I', 1)
               + box(b'url ', b'\x00\x00\x00\x01'))
    dinf = box(b'dinf', dref)
    mebx_entry = struct.pack('>I', 16) + b'mebx' + b'\x00' * 6 + struct.pack('>H', 1)
    stsd = box(b'stsd', b'\x00\x00\x00\x00' + struct.pack('>I', 1) + mebx_entry)
    stts = box(b'stts', b'\x00\x00\x00\x00' + struct.pack('>II', 1, 1) + struct.pack('>I', 3000))
    stsc = box(b'stsc', b'\x00\x00\x00\x00' + struct.pack('>III', 1, 1, 1))
    stsz = box(b'stsz', b'\x00\x00\x00\x00' + struct.pack('>II', 0, 1)
               + struct.pack('>I', len(payload)))
    mdat = find(src, [b'mdat'])
    mdat_end = mdat[1] + mdat[2]
    stco = box(b'stco', b'\x00\x00\x00\x00' + struct.pack('>II', 1, mdat_end))
    stbl = box(b'stbl', stsd + stts + stsc + stsz + stco)
    minf = box(b'minf', nmhd + dinf + stbl)
    tkhd_payload = (b'\x00\x00\x00\x03' + b'\x00' * 8 + struct.pack('>I', 3) + b'\x00' * 4
                    + struct.pack('>I', 3000) + b'\x00' * 8 + b'\x00' * 4
                    + b'\x00\x00\x00\x00'
                    + struct.pack('>9I', 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
                    + b'\x00' * 8)
    tkhd = box(b'tkhd', tkhd_payload)
    mdia = box(b'mdia', mdhd + hdlr + minf)
    trak = box(b'trak', tkhd + mdia)
    b = bytearray(src)
    b[mdat_end:mdat_end] = payload
    struct.pack_into('>I', b, mdat[1], mdat[2] + len(payload))
    moov = find(bytes(b), [b'moov'])
    insert_at = moov[1] + moov[2]
    b[insert_at:insert_at] = trak
    struct.pack_into('>I', b, moov[1], moov[2] + len(trak))
    pathlib.Path(dst).write_bytes(b)


def make_sei_essence_fixture(dst):
    """H264 stream carrying an SEI user_data_unregistered unit with coordinate text in the
    essence itself (not container metadata). Source encoded without B-frames so the raw
    annexb remux keeps every frame."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='sei-fixture-'))
    bf0 = tmp / 'base-bf0.mp4'
    ff(['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx264', '-bf', '0', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
        str(bf0)])
    raw = tmp / 'base.h264'
    ff(['-i', str(bf0), '-c:v', 'copy', '-an', '-bsf:v', 'h264_mp4toannexb', str(raw)])
    d = raw.read_bytes()
    starts, i = [], 0
    while True:
        j = d.find(b'\x00\x00\x00\x01', i)
        if j < 0:
            break
        starts.append(j)
        i = j + 4
    sps = next(k for k in starts if d[k + 4] & 31 == 7)
    nxt = min(k for k in starts if k > sps)
    payload = bytes(range(16)) + b'+19.0760+072.8777/'
    sei = b'\x00\x00\x00\x01' + bytes([0x06, 0x05, len(payload)]) + payload + bytes([0x80])
    (tmp / 'with-sei.h264').write_bytes(d[:nxt] + sei + d[nxt:])
    ff(['-r', '30', '-i', str(tmp / 'with-sei.h264'), '-i', str(bf0),
        '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', str(dst)])
    shutil.rmtree(tmp)


def make_hevc_hlg_fixture(dst):
    """10-bit HEVC HLG/BT.2020 (tag hvc1) with container location metadata."""
    ff(['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx265', '-x265-params', 'pools=1', '-pix_fmt', 'yuv420p10le',
        '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67',
        '-colorspace', 'bt2020nc', '-tag:v', 'hvc1', '-c:a', 'aac', '-shortest',
        '-metadata', 'location=' + LOC, str(dst)])


def inject_unknown_location_box(src, dst, boxtype, payload):
    """Append an unknown udta child box of the given 4-byte type carrying the given payload
    (literal coordinate text when payload is None), fixing ancestor sizes. Simulates a
    proprietary location form no metadata flag targets."""
    b = bytearray(pathlib.Path(src).read_bytes())

    def children(start, end):
        out, i = [], start
        while i + 8 <= end:
            size = struct.unpack_from('>I', b, i)[0]
            out.append((bytes(b[i + 4:i + 8]), i, size))
            i += size
        return out

    def find(path):
        start, end = 0, len(b)
        box = None
        for want in path:
            box = next((c for c in children(start, end) if c[0] == want), None)
            assert box, 'missing ' + repr(want)
            start, end = box[1] + 8, box[1] + box[2]
        return box

    if payload is None:
        payload = b'+19.0760+072.8777+005.000000/ synthetic proprietary location record'
    newbox = struct.pack('>I', 8 + len(payload)) + boxtype + payload
    moov = find([b'moov'])
    # udta must exist (base has none); append new udta at end of moov
    udta_payload = newbox
    udta = struct.pack('>I', 8 + len(udta_payload)) + b'udta' + udta_payload
    insert_at = moov[1] + moov[2]
    b[insert_at:insert_at] = udta
    struct.pack_into('>I', b, moov[1], moov[2] + len(udta))
    pathlib.Path(dst).write_bytes(b)


def main():
    make()
    # Actual digests of what was just generated. The SHIPPED expected digests live in
    # FIXTURE-DIGESTS.json and are never overwritten here; run_checks.py compares the two
    # before trusting any fixture, so a corrupted or substituted artifact fails closed.
    digests = {p.name: {'bytes': p.stat().st_size, 'sha256': sha256_file(p)}
               for p in sorted(F.iterdir())}
    (F.parent / 'GENERATED-DIGESTS.json').write_text(json.dumps(digests, indent=2) + '\n')
    print(json.dumps(digests, indent=2))


if __name__ == '__main__':
    main()
