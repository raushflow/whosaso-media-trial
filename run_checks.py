#!/usr/bin/env python3
"""Location-strip verification runner. Executes strip attempts on synthetic fixtures,
independently re-inventories before/after, asserts preservation and fail-closed refusal.
Writes RESULTS.json; exit 0 only if every assertion passes. Research-only."""
import json, pathlib, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import inventory as inv

F = ROOT / 'fixtures'
OUT = ROOT / 'outputs'
RESULTS = ROOT / 'RESULTS.json'
EXPECTED_VERSIONS = json.loads((ROOT / 'VERSIONS.json').read_text())


def cmd(*args, check=True):
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def tool_versions():
    return {
        'ffmpeg': cmd('ffmpeg', '-version').stdout.splitlines()[0],
        'ffprobe': cmd('ffprobe', '-version').stdout.splitlines()[0],
        'exiftool': cmd('exiftool', '-ver').stdout.strip(),
        'python3': cmd('python3', '--version').stdout.strip(),
    }


def playback_facts(path):
    dur = cmd('ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
              str(path)).stdout.strip()
    vf = cmd('ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
             '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', str(path)).stdout.strip()
    af = cmd('ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'a:0',
             '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', str(path)).stdout.strip()
    rot = cmd('ffprobe', '-v', 'error', '-select_streams', 'v:0',
              '-show_entries', 'side_data=rotation', '-of', 'csv=p=0', str(path)).stdout.strip()
    streams = [l for l in cmd('ffprobe', '-v', 'error', '-show_entries',
                              'stream=codec_type,codec_name', '-of', 'csv=p=0',
                              str(path)).stdout.splitlines() if l.strip()]
    return {'duration': dur, 'video_frames': vf, 'audio_frames': af, 'rotation': rot,
            'streams': sorted(streams)}


def decode_check(path):
    z = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'],
                       capture_output=True, text=True)
    return {'returncode': z.returncode, 'stderr_bytes': len(z.stderr.encode())}


def strip_ffmpeg(src, dst, per_stream):
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(src), '-map', '0',
            '-map_metadata', '-1']
    if per_stream:
        args += ['-map_metadata:s', '-1']
    args += ['-map_chapters', '-1', '-c', 'copy', str(dst)]
    cmd(*args)


def strip_exiftool(src, dst, tagspec):
    shutil.copy2(src, dst)
    cmd('exiftool', tagspec, '-overwrite_original', str(dst))


def strip_ffmpeg_va(src, dst):
    """Corrected recipe shape: explicit video+audio mapping so data/timed-metadata tracks are
    excluded structurally rather than carried or hard-failing the job."""
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(src),
        '-map', '0:v:0', '-map', '0:a:0', '-map_metadata', '-1', '-map_metadata:s', '-1',
        '-map_chapters', '-1', '-c', 'copy', str(dst))


def main():
    versions = tool_versions()
    version_pin_ok = versions == EXPECTED_VERSIONS
    results = {'tool_versions': versions, 'version_pin_ok': version_pin_ok, 'cases': []}
    failures = []
    if not version_pin_ok:
        failures.append('tool versions differ from VERSIONS.json; run is INCONCLUSIVE for reproduction')

    base_facts = playback_facts(F / 'base.mp4')
    base_essence = inv.essence_hashes(F / 'base.mp4')
    OUT.mkdir(exist_ok=True)

    def record(name, ok, detail):
        results['cases'].append({'case': name, 'ok': ok, 'detail': detail})
        if not ok:
            failures.append(name)

    # Fixtures run through the generic S1/S3 positive strip loop. f7/f8/f9 have dedicated
    # cases below (data-track, in-essence SEI, HDR re-encode paths) and only join N0.
    fixtures = ['f1-udta.mp4', 'f2-mdta.mov', 'f3-pertrack.mp4', 'f4-rotate.mp4',
                'f5-unknown-box.mp4', 'f6-novel-box.mp4']
    n0_fixtures = fixtures + ['f7-datatrack.mp4', 'f8-sei-essence.mp4', 'f9-hevc-hlg.mov',
                              'f10-noaudio.mp4', 'f11-twoaudio.mp4', 'f12-avoffset.mp4']

    # Fixture trust gate: the shipped expected FIXTURE-DIGESTS.json must match the actual
    # fixture files on disk BEFORE anything runs. Generation (gen_fixtures.py) writes
    # GENERATED-DIGESTS.json separately and never overwrites the expected file. A corrupted
    # or substituted fixture fails the whole run closed.
    expected = json.loads((ROOT / 'FIXTURE-DIGESTS.json').read_text())
    actual = {p.name: {'bytes': p.stat().st_size, 'sha256': inv.sha256_file(p)}
              for p in sorted(F.iterdir())}
    digest_gate_ok = (actual == expected)
    record('fixture-digests-match-shipped-expected', digest_gate_ok,
           {'expected_files': sorted(expected), 'actual_files': sorted(actual),
            'mismatches': [k for k in set(expected) | set(actual)
                           if expected.get(k) != actual.get(k)]})
    if not digest_gate_ok:
        results['failures'] = failures
        results['verdict'] = 'FAILURES: fixture trust gate failed; no case results are trustworthy'
        RESULTS.write_text(json.dumps(results, indent=2) + '\n')
        print(results['verdict'])
        sys.exit(1)

    # Negative control N0: the verifier must REFUSE every unstripped fixture (fail-closed).
    for name in n0_fixtures:
        before = inv.full_inventory(F / name)
        chk0 = inv.admission_check(before)
        record('N0-verifier-refuses-unstripped:' + name, not chk0['admissible'],
               {'admission': chk0, 'exiftool_fields': before['exiftool_location_fields'],
                'byte_scan': before['byte_scan']})

    # Positive strip cases: S1 ffmpeg full strip (all fixtures) and S3 exiftool broad strip
    # (f3 is covered by the N3 residue case instead: exiftool leaves a per-track udta/name box
    # that strict admission must refuse).
    s3_fixtures = [n for n in fixtures if n != 'f3-pertrack.mp4']
    for name in fixtures:
        src = F / name
        before = inv.full_inventory(src)
        stem = pathlib.Path(name).stem
        plan = [('S1-ffmpeg-full-strip', 's1-' + stem + '.mp4',
                 lambda s, d: strip_ffmpeg(s, d, per_stream=True))]
        if name in s3_fixtures:
            plan.append(('S3-exiftool-broad-strip', 's3-' + stem + pathlib.Path(name).suffix,
                         lambda s, d: strip_exiftool(s, d, '-all=')))
        for label, out_name, fn in plan:
            dst = OUT / out_name
            fn(src, dst)
            after = inv.full_inventory(dst)
            chk = inv.admission_check(after)
            facts = playback_facts(dst)
            essence = inv.essence_hashes(dst)
            dec = decode_check(dst)
            ok = True
            why = {}
            # Admission for stored/served artifacts: no recognized location form AND no
            # unclassified or malformed box. Unclassified content is rejected/contained,
            # never waved through on a clean signature scan.
            why['admissible'] = chk['admissible']
            ok &= why['admissible']
            # Preservation: only for ffmpeg stream-copy cases is essence required identical.
            if label.startswith('S1'):
                why['essence_identical'] = (essence == inv.essence_hashes(src))
                ok &= why['essence_identical']
            why['duration_preserved'] = facts['duration'] == base_facts['duration']
            why['video_frames_preserved'] = facts['video_frames'] == base_facts['video_frames']
            why['audio_frames_preserved'] = facts['audio_frames'] == base_facts['audio_frames']
            why['rotation_preserved'] = facts['rotation'] == playback_facts(src)['rotation']
            why['streams_preserved'] = facts['streams'] == base_facts['streams']
            why['decode_clean'] = dec['returncode'] == 0 and dec['stderr_bytes'] == 0
            ok &= all(why.values())
            record(label + ':' + name, ok, {
                'why': why, 'admission': chk,
                'before_sha256': before['sha256'], 'after_sha256': after['sha256'],
                'before_box_tree': before['box_tree'], 'after_box_tree': after['box_tree'],
                'before_inventory': {k: before[k] for k in ('byte_scan', 'ffprobe',
                                                            'exiftool_location_fields')},
                'after_inventory': {k: after[k] for k in ('byte_scan', 'ffprobe',
                                                          'exiftool_location_fields')},
                'playback_after': facts, 'essence_after': essence, 'decode': dec})

    # Residue case N3: exiftool -all= on f3 removes all recognized location forms but leaves
    # a per-track udta/name box (track title). Strict admission must REFUSE the artifact as
    # unclassified residue while the recognized-location scan stays negative, and the ffmpeg
    # S1 path on the same fixture must be admissible (asserted in its own case above).
    n3 = OUT / 'n3-f3-exiftool-residue.mp4'
    strip_exiftool(F / 'f3-pertrack.mp4', n3, '-all=')
    after_n3 = inv.full_inventory(n3)
    chk_n3 = inv.admission_check(after_n3)
    n3_ok = (not chk_n3['location_signals']) and (not chk_n3['admissible']) and \
        any('udta/name' in u for u in chk_n3['unclassified'])
    record('N3-exiftool-track-name-residue-refused-unclassified', n3_ok,
           {'admission': chk_n3,
            'exiftool_fields': after_n3['exiftool_location_fields']})

    # Negative strip case N1: targeted exiftool GPS-only strip of F5 must LEAVE the unknown
    # proprietary box, and the verifier must refuse the artifact (fail-closed).
    n1 = OUT / 'n1-f5-gps-only.mp4'
    strip_exiftool(F / 'f5-unknown-box.mp4', n1, '-GPS:all=')
    after_n1 = inv.full_inventory(n1)
    chk_n1 = inv.admission_check(after_n1)
    record('N1-targeted-strip-leaves-unknown-box-and-verifier-refuses', not chk_n1['admissible'],
           {'admission': chk_n1, 'byte_scan': after_n1['byte_scan'],
            'exiftool_fields': after_n1['exiftool_location_fields']})

    # Negative strip case N2: same targeted strip of F6, whose 'zq9x' box type is in NO
    # signature list. The artifact must be refused by the unclassified-box rule with a
    # NEGATIVE recognized-location scan, proving refusal does not depend on known signatures.
    n2 = OUT / 'n2-f6-gps-only.mp4'
    strip_exiftool(F / 'f6-novel-box.mp4', n2, '-GPS:all=')
    after_n2 = inv.full_inventory(n2)
    chk_n2 = inv.admission_check(after_n2)
    refused_by_unclassified = (not chk_n2['admissible']) and (not chk_n2['location_signals']) and any('zq9x' in u for u in chk_n2['unclassified'])
    record('N2-novel-box-refused-by-unclassified-rule-not-signature', refused_by_unclassified,
           {'admission': chk_n2, 'byte_scan': after_n2['byte_scan'],
            'exiftool_fields': after_n2['exiftool_location_fields']})

    # Parser-integrity negatives (reviewer counterexamples, now asserted). All derive from the
    # admissible S1 output and must be REFUSED with the named anomaly present.
    clean = (OUT / 's1-f1-udta.mp4').read_bytes()

    def tamper(name, data, marker):
        p = OUT / name
        p.write_bytes(data)
        chk = inv.admission_check(inv.full_inventory(p))
        ok = (not chk['admissible']) and any(marker in u for u in chk['unclassified'])
        record(name, ok, {'admission': chk})

    # N4a: 7 trailing bytes after the last top-level box.
    tamper('N4a-trailing-7-bytes', clean + b'zq9xBAD', '<trailing 7 bytes>')
    # N4b: 5 trailing bytes hidden inside moov by extending its size field.
    import struct as _struct
    moov_off = clean.rindex(b'moov') - 4
    moov_size = _struct.unpack_from('>I', clean, moov_off)[0]
    nested = bytearray(clean) + b'TAIL!'
    _struct.pack_into('>I', nested, moov_off, moov_size + 5)
    tamper('N4b-nested-trailing-5-bytes-in-moov', bytes(nested), '/moov/<trailing 5 bytes>')
    # N5: valid free box whose opaque payload is non-zero but carries no known signature.
    tamper('N5-free-box-nonzero-opaque-payload',
           clean + _struct.pack('>I', 16) + b'free' + b'PAYLOAD!', '<nonzero-opaque-payload>')
    # N6: truncated extended-size header (size==1, fewer than 16 bytes remain).
    tamper('N6-truncated-extended-size-header',
           clean + _struct.pack('>I', 1) + b'zzzz' + b'xx', '<malformed truncated-extended-size')

    # r5 open-gate cases: timed-data tracks, in-essence location, derivatives, HDR re-encode.

    # D1: the -map 0 strip recipe must FAIL on a data-track file, not silently drop or leak.
    z = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i',
                        str(F / 'f7-datatrack.mp4'), '-map', '0', '-map_metadata', '-1',
                        '-map_metadata:s', '-1', '-map_chapters', '-1', '-c', 'copy',
                        str(OUT / 'd1-f7-map0.mp4')], capture_output=True, text=True)
    record('D1-map0-recipe-fails-on-datatrack',
           z.returncode != 0 and 'codec' in (z.stderr.lower()),
           {'returncode': z.returncode, 'stderr': z.stderr.strip()[:300]})

    # S4 corrected recipe: explicit video+audio mapping excludes the data track.
    s4 = OUT / 's4-f7-va-only.mp4'
    strip_ffmpeg_va(F / 'f7-datatrack.mp4', s4)
    inv_s4 = inv.full_inventory(s4)
    chk_s4 = inv.admission_check(inv_s4)
    facts_s4 = playback_facts(s4)
    ok = chk_s4['admissible'] and facts_s4['streams'] == base_facts['streams'] \
        and inv.essence_hashes(s4) == inv.essence_hashes(F / 'base.mp4') \
        and facts_s4['video_frames'] == base_facts['video_frames'] \
        and facts_s4['duration'] == base_facts['duration']
    record('S4-explicit-va-map-excludes-datatrack', ok,
           {'admission': chk_s4, 'playback': facts_s4})

    # E1: stream copy CANNOT remove in-essence location. S1 on F8 keeps the SEI bytes and
    # admission must refuse (caught only because the payload is a known literal).
    e1 = OUT / 'e1-f8-streamcopy.mp4'
    strip_ffmpeg_va(F / 'f8-sei-essence.mp4', e1)
    chk_e1 = inv.admission_check(inv.full_inventory(e1))
    record('E1-streamcopy-keeps-in-essence-sei-and-refuses',
           (not chk_e1['admissible']) and chk_e1['location_signals'],
           {'admission': chk_e1})

    # E2: the re-encode path drops the unknown SEI. Decode + libx264 with strip flags on F8
    # must produce an admissible output with every frame preserved.
    e2 = OUT / 'e2-f8-reencode.mp4'
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i',
        str(F / 'f8-sei-essence.mp4'), '-map', '0:v:0', '-map', '0:a:0',
        '-map_metadata', '-1', '-map_metadata:s', '-1', '-map_chapters', '-1',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'copy', str(e2))
    inv_e2 = inv.full_inventory(e2)
    chk_e2 = inv.admission_check(inv_e2)
    facts_e2 = playback_facts(e2)
    facts_f8 = playback_facts(F / 'f8-sei-essence.mp4')
    ok = chk_e2['admissible'] and facts_e2['video_frames'] == facts_f8['video_frames'] \
        and facts_e2['duration'] == facts_f8['duration'] \
        and decode_check(e2)['stderr_bytes'] == 0
    record('E2-reencode-drops-sei-preserves-frames', ok,
           {'admission': chk_e2, 'playback': facts_e2, 'source_playback': facts_f8})

    # H2: HDR->SDR convert + strip on the HEVC HLG fixture: admissible, frames/duration
    # preserved, BT.709 output, sane luma (appearance still needs human review).
    h2 = OUT / 'h2-f9-hdr-sdr.mp4'
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i',
        str(F / 'f9-hevc-hlg.mov'), '-map', '0:v:0', '-map', '0:a:0',
        '-map_metadata', '-1', '-map_metadata:s', '-1', '-map_chapters', '-1',
        '-vf', 'zscale=t=linear:npl=100,tonemap=hable,zscale=t=bt709:p=bt709:m=bt709:r=tv,format=yuv420p',
        '-c:v', 'libx264', '-c:a', 'copy', str(h2))
    inv_h2 = inv.full_inventory(h2)
    chk_h2 = inv.admission_check(inv_h2)
    facts_h2 = playback_facts(h2)
    facts_f9 = playback_facts(F / 'f9-hevc-hlg.mov')
    zv = cmd('ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=color_transfer,color_primaries', '-of', 'json', str(h2))
    colors = json.loads(zv.stdout)['streams'][0]
    frame = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '1', '-i', str(h2), '-frames:v', '1',
                            '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                           capture_output=True, check=True).stdout
    luma_mean = sum(frame) / len(frame)
    ok = chk_h2['admissible'] and facts_h2['video_frames'] == facts_f9['video_frames'] \
        and facts_h2['duration'] == facts_f9['duration'] \
        and colors.get('color_transfer') == 'bt709' and colors.get('color_primaries') == 'bt709' \
        and 32 <= luma_mean <= 224 and decode_check(h2)['stderr_bytes'] == 0
    record('H2-hdr-to-sdr-convert-strip-preserves-and-cleans', ok,
           {'admission': chk_h2, 'playback': facts_h2, 'colors': colors,
            'luma_mean_at_1s': round(luma_mean, 1)})

    # V1: derivative re-inventory. Thumbnail and HLS derivatives from the location-bearing F1
    # must pass their format-appropriate inventory (exiftool for JPEG, text scan for the
    # manifest, byte scan for segments).
    deriv = OUT / 'deriv'
    deriv.mkdir(exist_ok=True)
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', '1',
        '-i', str(F / 'f1-udta.mp4'), '-frames:v', '1', str(deriv / 'thumb.jpg'))
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(F / 'f1-udta.mp4'),
        '-map', '0:v:0', '-map', '0:a:0', '-map_metadata', '-1', '-map_metadata:s', '-1',
        '-c', 'copy', '-f', 'hls', '-hls_time', '3', '-hls_list_size', '0',
        str(deriv / 'pl.m3u8'))
    thumb_gps = inv.exiftool_gps(deriv / 'thumb.jpg')
    manifest_text = (deriv / 'pl.m3u8').read_text()
    seg_hits = []
    for seg in deriv.glob('*.ts'):
        scan = inv.byte_scan(seg)
        seg_hits.append({'file': seg.name, 'scan': scan})
    ok = (not thumb_gps
          and not any(s in manifest_text for s in ('19.07', '72.87', 'loci', 'ISO6709'))
          and all(not h['scan']['location_signatures'] and not h['scan']['coordinate_literals']
                  for h in seg_hits))
    record('V1-derivatives-reinventoried-clean', ok,
           {'thumb_exiftool_gps': thumb_gps, 'manifest': manifest_text,
            'segment_scans': seg_hits})

    # V2: DASH derivative re-inventory (manifest XML text scan + init/media segment byte scan).
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(F / 'f1-udta.mp4'),
        '-map', '0:v:0', '-map', '0:a:0', '-map_metadata', '-1', '-map_metadata:s', '-1',
        '-c', 'copy', '-f', 'dash', '-seg_duration', '3', str(deriv / 'dash.mpd'))
    mpd_text = (deriv / 'dash.mpd').read_text()
    dash_hits = []
    for seg in sorted(deriv.glob('*.m4s')):
        scan = inv.byte_scan(seg)
        dash_hits.append({'file': seg.name, 'scan': scan})
    # Reviewer hardening: fail closed on an empty segment set (all([]) is True), and require
    # full manifest-referenced-file coverage: the MPD references files through $-templates,
    # so expand them per Representation id. Every Representation must have its init segment
    # scanned and at least one media chunk scanned, and no on-disk segment may be outside
    # what the manifest can reference (no orphan/unscanned segments).
    templates = set(re.findall(r'(?:media|initialization)="([^"]+)"', mpd_text))
    rep_ids = re.findall(r'<Representation[^>]*\bid="([^"]+)"', mpd_text)
    scanned_names = {h['file'] for h in dash_hits}

    def tpl_regex(tpl, rid):
        t = re.escape(tpl)
        t = t.replace(re.escape('$RepresentationID$'), re.escape(str(rid)))
        t = re.sub(r'\\\$Number%0(\d+)d\\\$', lambda m: r'\d{%d}' % int(m.group(1)), t)
        t = t.replace(re.escape('$Number$'), r'\d+')
        return re.compile('^' + t + '$')

    missing = []
    matched_any = set()
    for rid in rep_ids or ['']:
        for tpl in templates:
            rx = tpl_regex(tpl, rid)
            hits = [n for n in scanned_names if rx.match(n)]
            matched_any.update(hits)
            is_init = '$Number' not in tpl
            if is_init and not hits:
                missing.append('init:' + tpl + ':rep' + str(rid))
            if not is_init and not hits:
                missing.append('media:' + tpl + ':rep' + str(rid))
    orphans = sorted(n for n in scanned_names if n not in matched_any)
    ok2 = (not any(s in mpd_text for s in ('19.07', '72.87', 'loci', 'ISO6709'))
           and len(dash_hits) > 0
           and not missing
           and not orphans
           and all(not h['scan']['location_signatures'] and not h['scan']['coordinate_literals']
                   for h in dash_hits))
    record('V2-dash-derivative-reinventoried-clean', ok2,
           {'manifest': mpd_text, 'segment_scans': dash_hits,
            'manifest_templates': sorted(templates), 'representation_ids': rep_ids,
            'referenced_but_missing': missing, 'on_disk_but_unreferenced': orphans})

    # V3: WebP thumbnail derivative re-inventory (exiftool scan).
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', '1',
        '-i', str(F / 'f1-udta.mp4'), '-frames:v', '1', str(deriv / 'thumb.webp'))
    webp_gps = inv.exiftool_gps(deriv / 'thumb.webp')
    webp_scan = inv.byte_scan(deriv / 'thumb.webp')
    record('V3-webp-thumbnail-reinventoried-clean',
           not webp_gps and not webp_scan['location_signatures']
           and not webp_scan['coordinate_literals'],
           {'webp_exiftool_gps': webp_gps, 'webp_scan': webp_scan})

    # A1-A4: audio supported-profile characterization. These record behavior for the profile
    # gate; they are NOT a profile decision.
    # A1: exact one-audio map fails on a video-only upload.
    z = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i',
                        str(F / 'f10-noaudio.mp4'), '-map', '0:v:0', '-map', '0:a:0',
                        '-map_metadata', '-1', '-c', 'copy', str(OUT / 'a1-f10.mp4')],
                       capture_output=True, text=True)
    record('A1-exact-audio-map-fails-on-video-only',
           z.returncode != 0 and 'matches no streams' in z.stderr,
           {'returncode': z.returncode, 'stderr': z.stderr.strip()[:200]})
    # A2: optional-audio map variant handles video-only cleanly.
    a2 = OUT / 'a2-f10-optional-audio.mp4'
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(F / 'f10-noaudio.mp4'),
        '-map', '0:v:0', '-map', '0:a?', '-map_metadata', '-1', '-map_metadata:s', '-1',
        '-map_chapters', '-1', '-c', 'copy', str(a2))
    inv_a2 = inv.full_inventory(a2)
    chk_a2 = inv.admission_check(inv_a2)
    facts_a2 = playback_facts(a2)
    record('A2-optional-audio-map-video-only-admissible',
           chk_a2['admissible'] and facts_a2['streams'] == ['h264,video'],
           {'admission': chk_a2, 'playback': facts_a2})
    # A3: on two audio tracks, -map 0:a:0 SILENTLY keeps only the first. Characterized behavior;
    # the profile gate must refuse multi-audio or define mixing, never silently drop a track.
    a3 = OUT / 'a3-f11-first-audio-only.mp4'
    strip_ffmpeg_va(F / 'f11-twoaudio.mp4', a3)
    facts_a3 = playback_facts(a3)
    record('A3-two-audio-silently-reduced-to-first-CHARACTERIZED',
           facts_a3['streams'] == ['aac,audio', 'h264,video'],
           {'playback': facts_a3,
            'note': 'second audio track silently dropped; supported-profile decision required'})
    # A4: A/V offset is preserved through the stream-copy recipe.
    a4 = OUT / 'a4-f12-offset.mp4'
    strip_ffmpeg_va(F / 'f12-avoffset.mp4', a4)

    def start(path, sel):
        return float(cmd('ffprobe', '-v', 'error', '-select_streams', sel,
                         '-show_entries', 'stream=start_time', '-of', 'csv=p=0',
                         str(path)).stdout.strip())
    off_src = start(F / 'f12-avoffset.mp4', 'a:0') - start(F / 'f12-avoffset.mp4', 'v:0')
    off_out = start(a4, 'a:0') - start(a4, 'v:0')
    record('A4-av-offset-preserved-stream-copy', abs(off_src - off_out) < 0.01,
           {'source_offset_s': off_src, 'output_offset_s': off_out})

    # A5: optional FIRST-audio map (-map 0:a:0?) is a different recipe from -map 0:a? :
    # it keeps at most one audio track. Reviewer note: do not conflate the two when choosing
    # the supported profile.
    a5a = OUT / 'a5-f10-optional-first-audio.mp4'
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(F / 'f10-noaudio.mp4'),
        '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1', '-map_metadata:s', '-1',
        '-map_chapters', '-1', '-c', 'copy', str(a5a))
    inv_a5a = inv.full_inventory(a5a)
    chk_a5a = inv.admission_check(inv_a5a)
    record('A5a-optional-first-audio-map-video-only-admissible',
           chk_a5a['admissible'] and playback_facts(a5a)['streams'] == ['h264,video'],
           {'admission': chk_a5a, 'playback': playback_facts(a5a)})
    a5b = OUT / 'a5-f11-optional-first-audio.mp4'
    cmd('ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(F / 'f11-twoaudio.mp4'),
        '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1', '-map_metadata:s', '-1',
        '-map_chapters', '-1', '-c', 'copy', str(a5b))
    record('A5b-optional-first-audio-map-two-audio-keeps-first-CHARACTERIZED',
           playback_facts(a5b)['streams'] == ['aac,audio', 'h264,video'],
           {'playback': playback_facts(a5b),
            'note': 'like A3, second track dropped; profile decision required'})

    # Hazard case H1: exact exiftool invocation observed to delete the source file.
    # Command: exiftool -all= -overwrite_original -o <out> <src>
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        td = pathlib.Path(td)
        victim = td / 'victim.mp4'
        shutil.copy2(F / 'f1-udta.mp4', victim)
        z = subprocess.run(['exiftool', '-all=', '-overwrite_original', '-o',
                            str(td / 'out.mp4'), str(victim)], capture_output=True, text=True)
        deleted = not victim.exists()
        record('H1-exiftool-o-overwrite_original-deletes-source', deleted,
               {'invocation': 'exiftool -all= -overwrite_original -o out.mp4 victim.mp4',
                'returncode': z.returncode, 'stdout': z.stdout.strip(),
                'stderr': z.stderr.strip(), 'source_deleted': deleted,
                'output_exists': (td / 'out.mp4').exists()})

    results['failures'] = failures
    results['verdict'] = 'ALL ASSERTIONS PASS' if not failures else 'FAILURES: ' + '; '.join(failures)
    RESULTS.write_text(json.dumps(results, indent=2) + '\n')
    print(results['verdict'])
    sys.exit(0 if not failures else 1)


if __name__ == '__main__':
    main()
