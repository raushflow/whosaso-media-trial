#!/usr/bin/env python3
"""Bounded single-worker demo runner for the Whosaso free-host trial.

Synthetic fixtures only. No personal media, no external calls, no secrets, no Whosaso
app/backend connection. Measures the QUALIFIED strip/inventory pipeline (the reviewer's
bounded-GO location-strip packet) on a free host: wall time, child CPU, child max RSS,
refusal behavior. GET /trial runs the full 46-assertion qualified suite once; results are
cached (use /trial?fresh=1 to re-run). GET /trial/transcode runs one 15s 1080p synthetic
H264 transcode with timing (CPU-cost probe). GET /health is liveness.
"""
import json, os, resource, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
CACHE = {}

def run_timed(argv, timeout):
    t0 = time.monotonic()
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    t1 = time.monotonic()
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        'argv': argv[0] + ' ...',
        'returncode': p.returncode,
        'wall_s': round(t1 - t0, 3),
        'child_cpu_s': round((r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime), 3),
        'child_maxrss_kb_delta': r1.ru_maxrss,  # max over all children so far
        'stdout_tail': p.stdout.strip().splitlines()[-3:],
        'stderr_tail': p.stderr.strip().splitlines()[-3:],
    }

def versions():
    def ver(argv):
        p = subprocess.run(argv, capture_output=True, text=True)
        return (p.stdout or p.stderr).strip().splitlines()[0]
    return {'ffmpeg': ver(['ffmpeg', '-version']),
            'exiftool': ver(['exiftool', '-ver']),
            'python': sys.version.split()[0]}

def trial():
    with LOCK:
        steps = []
        steps.append(run_timed(['python3', 'gen_fixtures.py'], 300))
        steps.append(run_timed(['python3', 'run_checks.py'], 900))
        ok = steps[-1]['returncode'] == 0
        return {'trial': 'qualified-strip-pipeline', 'ok': ok,
                'versions': versions(), 'steps': steps}

def transcode():
    with LOCK:
        src = os.path.join(ROOT, 'outputs', 'trial-src.mp4')
        os.makedirs(os.path.join(ROOT, 'outputs'), exist_ok=True)
        gen = run_timed(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                         '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=30:duration=15',
                         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=15',
                         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                         '-c:a', 'aac', '-shortest', src], 300)
        enc = run_timed(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                         '-vf', 'scale=640:360', '-c:v', 'libx264', '-preset', 'veryfast',
                         '-crf', '28', '-c:a', 'aac', '-f', 'null', '-'], 900)
        try:
            os.remove(src)
        except OSError:
            pass
        return {'trial': 'synthetic-transcode-probe', 'versions': versions(),
                'generate_15s_1080p': gen, 'transcode_to_360p': enc}

def digests():
    with LOCK:
        g = run_timed(['python3', 'gen_fixtures.py'], 300)
        try:
            gen = json.load(open(os.path.join(ROOT, 'GENERATED-DIGESTS.json')))
        except Exception as e:
            return {'trial': 'fixture-digest-compare', 'error': str(e), 'generate': g}
        shipped = json.load(open(os.path.join(ROOT, 'FIXTURE-DIGESTS.json')))
        diff = {k: {'shipped': shipped.get(k), 'generated': gen.get(k)}
                for k in sorted(set(shipped) | set(gen))
                if shipped.get(k) != gen.get(k)}
        return {'trial': 'fixture-digest-compare', 'mismatches': diff, 'generate': g}

class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == '/health':
                return self._send({'ok': True})
            fresh = parse_qs(u.query).get('fresh') == ['1']
            if u.path == '/trial':
                if fresh or 'trial' not in CACHE:
                    CACHE['trial'] = trial()
                return self._send(CACHE['trial'])
            if u.path == '/trial/digests':
                return self._send(digests())
            if u.path == '/trial/transcode':
                if fresh or 'transcode' not in CACHE:
                    CACHE['transcode'] = transcode()
                return self._send(CACHE['transcode'])
            return self._send({'error': 'unknown path',
                               'paths': ['/health', '/trial', '/trial/transcode', '/trial/digests']}, 404)
        except subprocess.TimeoutExpired:
            return self._send({'error': 'step timed out'}, 504)
        except Exception as e:
            return self._send({'error': '%s: %s' % (type(e).__name__, e)}, 500)

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '10000'))
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
