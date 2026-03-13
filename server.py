#!/usr/bin/env python3
"""Compact asciinema recording viewer. Run: python3 server.py [directory] [port]"""

import json, os, re, sys, mimetypes
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from functools import lru_cache

# Parse args: server.py [directory] [port]
_args = sys.argv[1:]
CINEMA_DIR = Path(_args.pop(0)).expanduser().resolve() if _args and not _args[0].isdigit() else Path.home() / "cinema"
PORT = int(_args.pop(0)) if _args and _args[0].isdigit() else 8000
STATIC_DIR = Path(__file__).parent

if not CINEMA_DIR.is_dir():
    print(f"Error: directory not found: {CINEMA_DIR}")
    sys.exit(1)

ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b\[[\?]?[0-9;]*[hlmsu]'
    r'|\x1b[()][AB012]'
    r'|\x1b\[\d*[ABCDJK]'
    r'|\x1b\[?\d*;\d*[Hf]'
    r'|\x1b[78DME>=<]'
    r'|\x1b\[\d*[LPMS@X]'
    r'|\x1b\[\d*;\d*r'
    r'|\x1b\](?:0|1|2);[^\x07]*\x07'
)


def strip_ansi(text):
    return ANSI_RE.sub('', text)


def parse_cast(filepath):
    """Parse cast file, return (header, events) with absolute timestamps."""
    abs_time = 0.0
    events = []
    with open(filepath, 'r', errors='replace') as f:
        header = json.loads(f.readline())
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            abs_time += ev[0]
            if ev[1] == 'o':
                events.append((round(abs_time, 3), ev[2]))
    return header, events


def build_transcript(events, gap=1.5):
    """Build collapsed transcript from terminal output events."""
    if not events:
        return []

    chunks = []
    chunk_start = events[0][0]
    chunk_data = []
    prev_time = events[0][0]

    for time, data in events:
        if chunk_data and time - prev_time > gap:
            _flush_chunk(chunks, chunk_start, chunk_data)
            chunk_start = time
            chunk_data = []
        chunk_data.append(data)
        prev_time = time

    _flush_chunk(chunks, chunk_start, chunk_data)
    return chunks


def _flush_chunk(chunks, start_time, data_list):
    raw = ''.join(data_list)
    clean = strip_ansi(raw)
    # Remove null bytes and other control chars except \r\n\t
    clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', clean)

    lines = []
    overwrite_count = 0

    for raw_line in clean.split('\n'):
        # Handle \r overwrites within a line
        segments = raw_line.split('\r')
        if len(segments) > 1:
            overwrite_count += len(segments) - 1
        # Take last non-empty segment
        final = ''
        for seg in reversed(segments):
            s = seg.strip()
            if s:
                final = s
                break
        if final:
            lines.append(final)

    if not lines:
        return

    # Collapse consecutive duplicate/near-duplicate lines
    collapsed = []
    dup_count = 0
    for i, line in enumerate(lines):
        if i > 0 and _similar(line, lines[i - 1]):
            dup_count += 1
        else:
            if dup_count > 0:
                collapsed[-1]['collapsed'] = dup_count
            collapsed.append({'text': line[:500]})
            dup_count = 0
    if dup_count > 0 and collapsed:
        collapsed[-1]['collapsed'] = dup_count

    MAX_LINES = 30
    total = len(collapsed)
    entry = {
        'time': round(start_time, 2),
        'lines': collapsed[:MAX_LINES],
        'overwrites': overwrite_count,
    }
    if total > MAX_LINES:
        entry['hiddenLines'] = total - MAX_LINES
    chunks.append(entry)


def _similar(a, b):
    """Check if two lines are similar (e.g., progress bar updates)."""
    if a == b:
        return True
    # Same prefix up to numbers/percentages changing
    if len(a) > 10 and len(b) > 10:
        # Strip digits and compare
        da = re.sub(r'\d+', '#', a)
        db = re.sub(r'\d+', '#', b)
        if da == db:
            return True
    return False


@lru_cache(maxsize=16)
def _cached_transcript(filepath, mtime):
    _, events = parse_cast(filepath)
    return build_transcript(events)


@lru_cache(maxsize=16)
def _cached_search_index(filepath, mtime):
    """Build search index: list of (abs_time, stripped_text) per event group."""
    _, events = parse_cast(filepath)
    index = []
    # Group into ~0.5s windows for search granularity
    if not events:
        return index
    group_start = events[0][0]
    group_text = []
    prev_time = events[0][0]
    for time, data in events:
        if group_text and time - prev_time > 0.5:
            text = strip_ansi(''.join(group_text))
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            if text.strip():
                index.append((round(group_start, 2), text))
            group_start = time
            group_text = []
        group_text.append(data)
        prev_time = time
    if group_text:
        text = strip_ansi(''.join(group_text))
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        if text.strip():
            index.append((round(group_start, 2), text))
    return index


def list_recordings():
    files = []
    for f in sorted(CINEMA_DIR.iterdir()):
        if f.is_file() and not f.name.startswith('.'):
            try:
                with open(f) as fh:
                    header = json.loads(fh.readline())
                size = f.stat().st_size
                files.append({
                    'name': f.name,
                    'size': size,
                    'cols': header.get('term', {}).get('cols', header.get('width', 80)),
                    'rows': header.get('term', {}).get('rows', header.get('height', 24)),
                    'timestamp': header.get('timestamp', 0),
                    'theme': header.get('term', {}).get('theme'),
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return files


def search_recordings(query, filename=None):
    query_lower = query.lower()
    results = []
    files = [CINEMA_DIR / filename] if filename else sorted(CINEMA_DIR.iterdir())

    for f in files:
        if isinstance(f, Path) and f.is_file() and not f.name.startswith('.'):
            mtime = f.stat().st_mtime
            index = _cached_search_index(str(f), mtime)
            for time, text in index:
                pos = text.lower().find(query_lower)
                if pos != -1:
                    # Extract context around match
                    start = max(0, pos - 40)
                    end = min(len(text), pos + len(query) + 40)
                    context = text[start:end].strip()
                    # Clean up for display
                    context = re.sub(r'\s+', ' ', context)
                    if context:
                        results.append({
                            'file': f.name,
                            'time': time,
                            'context': context,
                            'matchStart': pos - start,
                            'matchEnd': pos - start + len(query),
                        })
                        if len(results) >= 200:
                            return results
    return results


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == '/' or path == '/index.html':
            self._serve_file(STATIC_DIR / 'index.html', 'text/html')
        elif path.startswith('/cast/'):
            name = path[6:]
            cast_path = CINEMA_DIR / name
            if cast_path.exists() and cast_path.parent == CINEMA_DIR:
                self._serve_cast(cast_path)
            else:
                self._error(404, 'Not found')
        elif path == '/api/files':
            self._json(list_recordings())
        elif path.startswith('/api/transcript/'):
            name = path[16:]
            cast_path = CINEMA_DIR / name
            if cast_path.exists():
                mtime = cast_path.stat().st_mtime
                transcript = _cached_transcript(str(cast_path), mtime)
                self._json(transcript)
            else:
                self._error(404, 'Not found')
        elif path == '/api/search':
            q = params.get('q', [''])[0]
            f = params.get('file', [None])[0]
            if not q:
                self._json([])
            else:
                self._json(search_recordings(q, f))
        else:
            self._error(404, 'Not found')

    def _serve_file(self, path, content_type):
        try:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(data))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._error(404, 'Not found')

    def _serve_cast(self, path):
        """Serve cast file, patching header if needed for player compatibility."""
        try:
            with open(path, 'rb') as f:
                header_line = f.readline()
                rest = f.read()
            # Patch palette from colon-separated string to array
            header = json.loads(header_line)
            theme = header.get('term', {}).get('theme', {})
            if theme and isinstance(theme.get('palette'), str):
                theme['palette'] = theme['palette'].split(':')
                header_line = json.dumps(header, ensure_ascii=False).encode() + b'\n'
            data = header_line + rest
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-asciicast')
            self.send_header('Content-Length', len(data))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._error(404, 'Not found')

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(msg.encode())


if __name__ == '__main__':
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Serving {CINEMA_DIR} at http://localhost:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
