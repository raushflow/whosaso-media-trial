#!/usr/bin/env python3
"""Independent inventory for location-strip verification. Research-only, synthetic fixtures."""
import json, struct, subprocess, hashlib, pathlib

CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'dinf', b'edts', b'udta',
              b'moof', b'traf', b'mfra', b'keys', b'ilst'}
META_BOX = b'meta'
# Recognized location signatures. This list is necessarily incomplete: the detector only knows
# recognized keys and signatures, so admission NEVER rests on this list alone. Anything
# unclassified is refused by admission_check below.
LOCATION_SIGNATURES = [b'loci', b'\xa9xyz', b'ISO6709', b'loCX']
# Structural/metadata box types observed in ffmpeg 4.4.2 and exiftool 12.40 outputs for the
# tested H264/AAC forms, plus ctoo (encoder tag, non-location). Any other box type, and any
# malformed box, is UNCLASSIFIED and fails admission. Location-bearing types (loci, cxyz,
# keys/mdta location items, loCX, ...) are deliberately NOT in this list.
KNOWN_BOXES = {'ftyp', 'free', 'mdat', 'moov', 'mvhd', 'trak', 'tkhd', 'edts', 'elst',
               'mdia', 'mdhd', 'hdlr', 'minf', 'vmhd', 'smhd', 'dinf', 'dref', 'stbl',
               'stsd', 'stts', 'stss', 'ctts', 'stsc', 'stsz', 'stco', 'sgpd', 'sbgp',
               'udta', 'meta', 'ilst', '\xa9too'}
# Coordinate literals are NOT reliable alone (muxers normalize ISO6709 strings); they are
# recorded as a secondary signal only.
COORDINATE_LITERALS = [b'19.07', b'72.87', b'19.0760', b'72.8777']


def sha256_file(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def box_tree(path):
    """Return a flat list of 'path size=N' entries for every box, recursing into known
    containers. Full EOF coverage: any byte not accounted for by a well-formed box produces
    an anomaly entry whose name contains '<' (trailing bytes, malformed sizes, truncated
    extended headers, nested size-0 boxes, non-zero free/skip payloads). Anomaly entries are
    refused by admission_check. Parse scope is stated in README: structural leaf payloads
    (stsd/stts/...) are accepted opaque and exercised through the decode assertions; mdat
    essence is covered by byte-signature scan and belongs to the codec lane."""
    b = pathlib.Path(path).read_bytes()
    out = []

    def walk(start, end, prefix):
        i = start
        while i + 8 <= end:
            size = struct.unpack_from('>I', b, i)[0]
            typ = bytes(b[i + 4:i + 8])
            header = 8
            if size == 1:
                if i + 16 > end:
                    out.append(prefix + '/<malformed truncated-extended-size at +'
                               + str(i - start) + '>')
                    return
                size = struct.unpack_from('>Q', b, i + 8)[0]
                header = 16
            if size == 0:
                if prefix:
                    out.append(prefix + '/<malformed size0-nested ' + repr(typ) + '>')
                    return
                size = end - i
            if size < header or i + size > end:
                out.append(prefix + '/<malformed ' + repr(typ) + ' at +' + str(i - start)
                           + '>')
                return
            name = prefix + '/' + typ.decode('latin-1')
            out.append(name + ' size=' + str(size))
            if typ in CONTAINERS:
                walk(i + header, i + size, name)
            elif typ == META_BOX:
                walk(i + header + 4, i + size, name)  # skip version/flags
            elif typ in (b'free', b'skip'):
                # Defined opaque-payload rule: padding boxes must be zero-filled. Any non-zero
                # opaque payload is unclassified content and refused.
                payload = b[i + header:i + size]
                if any(payload):
                    out.append(name + ' <nonzero-opaque-payload>')
            i += size
        if i != end:
            out.append(prefix + '/<trailing ' + str(end - i) + ' bytes>')

    walk(0, len(b), '')
    return out


def byte_scan(path):
    b = pathlib.Path(path).read_bytes()
    sig = [repr(s) for s in LOCATION_SIGNATURES if b.find(s) >= 0]
    coord = [repr(c) for c in COORDINATE_LITERALS if b.find(c) >= 0]
    return {'location_signatures': sig, 'coordinate_literals': coord}


def ffprobe_tags(path):
    def run(kind):
        z = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', kind,
                            '-of', 'json', str(path)], capture_output=True, check=True)
        return json.loads(z.stdout)
    fmt = run('format_tags').get('format', {}).get('tags', {})
    streams = [s.get('tags', {}) for s in run('stream_tags').get('streams', [])]
    return {'format_tags': fmt, 'stream_tags': streams}


def exiftool_gps(path):
    z = subprocess.run(['exiftool', '-j', '-a', '-G1', str(path)], capture_output=True, check=True)
    rec = json.loads(z.stdout)[0]
    return sorted(k for k in rec if 'GPS' in k or 'Location' in k)


def essence_hashes(path):
    """Demuxed elementary-stream SHA256 per stream index, via ffmpeg -c copy -f hash."""
    z = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=index,codec_type',
                        '-of', 'json', str(path)], capture_output=True, check=True)
    out = {}
    for s in json.loads(z.stdout)['streams']:
        ix = s['index']
        h = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:' + str(ix),
                            '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'],
                           capture_output=True, check=True)
        out[str(ix) + ':' + s['codec_type']] = h.stdout.decode().strip().split('=')[-1]
    return out


def full_inventory(path):
    return {'sha256': sha256_file(path), 'box_tree': box_tree(path), 'byte_scan': byte_scan(path),
            'ffprobe': ffprobe_tags(path), 'exiftool_location_fields': exiftool_gps(path)}


def location_present(inv):
    """Recognized-location detector: any axis positive means a KNOWN location form is present.
    This only covers recognized keys and signatures; absence here is not proof of absence.
    Coordinate literals count as a signal here because these tests use fixed known coordinate
    strings; a production scan would use an ISO6709-pattern match instead of fixed literals."""
    if inv['exiftool_location_fields']:
        return True
    scan = inv['byte_scan']
    if scan['location_signatures'] or scan['coordinate_literals']:
        return True
    for src in [inv['ffprobe']['format_tags']] + inv['ffprobe']['stream_tags']:
        if any('loc' in k.lower() or 'gps' in k.lower() for k in src):
            return True
    return False


def unclassified_boxes(inv):
    """Box-tree entries that are neither known structural types nor parseable: unclassified
    metadata, unknown box types and malformed boxes. These are refused or contained, never
    waved through."""
    bad = []
    for entry in inv['box_tree']:
        name = entry.split(' size=')[0]
        if '<' in name:  # any anomaly marker: malformed, trailing bytes, nonzero opaque payload
            bad.append(entry)
            continue
        typ = name.rsplit('/', 1)[-1]
        if typ not in KNOWN_BOXES:
            bad.append(entry)
    return bad


def admission_check(inv):
    """Fail-closed admission verdict for a stored/served artifact. Admissible only when no
    recognized location form is present AND no unclassified or malformed box exists. Anything
    else is rejected (or quarantined) as unclassified, because the recognized-key detector
    cannot prove a negative."""
    loc = location_present(inv)
    uncl = unclassified_boxes(inv)
    return {'location_signals': loc, 'unclassified': uncl, 'admissible': not loc and not uncl}
