import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import requests
from .parsing import safe_url

class StopRun(Exception):
    pass

class Blocked(Exception):
    pass

class Missing(Exception):
    pass

class RobotsDenied(Exception):
    pass

class Oversized(Exception):
    pass

class Transient(Exception):
    pass


def retry_seconds(value, fallback=5):
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0, (when - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return fallback


def challenge(raw, content_type):
    # reCAPTCHA JS is present on normal Lex.uz pages. Never flag that alone.
    if 'html' not in content_type.lower():
        return False
    t = raw[:200000].decode('utf-8', errors='ignore').lower()
    title = t.split('<title',1)[-1].split('</title>',1)[0] if '<title' in t else ''
    return any(x in title for x in ('just a moment', 'access denied', 'verify you are human', 'security check', 'captcha challenge'))


@dataclass
class Response:
    raw: bytes
    url: str
    status: int
    headers: dict
    digest: str


class Client:
    def __init__(self, store, options, max_requests=0):
        self.store = store; self.options = options
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': options['user_agent'], 'Accept-Language':'uz,ru;q=0.8,en;q=0.6'})
        self.last = 0.; self.count = 0; self.max_requests = max_requests
        self.delay = options['delay']; self.robots = None

    def request(self, url, task_id=0, method='GET', data=None, conditional=False, enforce_robots=True):
        if safe_url(url) is None:
            raise Blocked('Request or redirect left the allowed lex.uz origin')
        target = url; headers = self.store.validators(task_id) if conditional else {}
        failures = 0; redirects = 0
        while True:
            if self.max_requests and self.count >= self.max_requests:
                raise StopRun('Request budget reached; run again to resume')
            cooldown = self.store.get_meta('not_before', 0) - time.time()
            if cooldown > 0:
                raise StopRun(f'Server cooldown active for another {cooldown:.0f}s; run again later')
            if enforce_robots and self.robots and not self.robots.can_fetch(self.options['user_agent'], target):
                raise RobotsDenied('robots.txt disallows this URL for the configured User-Agent')
            time.sleep(max(0, self.last + self.delay - time.monotonic()))
            self.last = time.monotonic(); self.count += 1
            try:
                with self.session.request(method, target, data=data, headers=headers, timeout=(15,90),
                                          allow_redirects=False, stream=True) as r:
                    limit = self.options['max_response_mb'] * 1024 * 1024
                    length = r.headers.get('Content-Length')
                    if length and length.isdigit() and int(length) > limit:
                        raise Oversized(f'Response exceeds {self.options["max_response_mb"]} MiB limit')
                    chunks = []; size = 0
                    for chunk in r.iter_content(65536):
                        size += len(chunk)
                        if size > limit:
                            raise Oversized('Decoded response exceeded the configured size limit')
                        chunks.append(chunk)
                    raw = b''.join(chunks); status = r.status_code; response_headers = dict(r.headers)
                    digest = self.store.response(task_id, method, target, status, response_headers, raw)
                    if status in (301,302,303,307,308):
                        redirects += 1
                        dest = safe_url(r.headers.get('Location',''), target)
                        if redirects > 6 or not dest:
                            raise Blocked('Unsafe redirect or redirect loop')
                        if any(x in urlsplit(dest).path.lower() for x in ('login','signin','captcha')):
                            raise Blocked('Site redirected to an authentication/challenge page')
                        target = dest
                        if status == 303 or (status in (301,302) and method == 'POST'):
                            method = 'GET'; data = None
                        continue
                    if status in (401,403):
                        raise Blocked(f'HTTP {status}: access denied; no evasion attempted')
                    if status in (404,410):
                        raise Missing(f'HTTP {status}: resource unavailable at {target}')
                    if status == 429:
                        seconds = retry_seconds(r.headers.get('Retry-After'), 60)
                        self.store.set_meta('not_before', time.time() + max(1, seconds))
                        raise StopRun(f'HTTP 429: global cooldown saved for {seconds:.0f}s')
                    if status in (400,500) and method == 'POST' and any(x in raw.lower() for x in (b'validation of viewstate', b'invalid postback', b'invalid viewstate', b'state information is invalid')):
                        raise ValueError('Expired or invalid ASP.NET form state')
                    if status in (408,425,500,502,503,504):
                        seconds = retry_seconds(r.headers.get('Retry-After'), 2 ** (failures + 1) + random.random())
                        if seconds > 20:
                            self.store.set_meta('not_before', time.time() + seconds)
                            raise StopRun(f'HTTP {status}: server requested {seconds:.0f}s wait; checkpoint saved')
                        raise Transient(f'HTTP {status}|{seconds}')
                    if status == 304:
                        task = self.store.task(task_id)
                        if task and task['source_hash']:
                            raw = self.store.read_blob(task['source_hash']); digest = task['source_hash']
                            previous = self.store.db.execute('SELECT headers FROM responses WHERE task_id=? AND status=200 ORDER BY id DESC LIMIT 1',(task_id,)).fetchone()
                            if previous:
                                import json
                                response_headers = {**json.loads(previous[0]), **response_headers}
                        else:
                            raise Transient('304 without a stored response|1')
                    elif not 200 <= status < 300:
                        raise ValueError(f'HTTP {status}')
                    if challenge(raw, r.headers.get('Content-Type','')):
                        raise Blocked('Challenge page detected; normal document content was not returned')
                    return Response(raw, target, status, response_headers, digest)
            except requests.exceptions.SSLError as exc:
                raise Blocked('TLS validation failed; check local certificates/clock, not verify=False') from exc
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError, Transient) as exc:
                failures += 1
                if failures >= self.options['http_attempts']:
                    raise Transient(f'Transport/server failure after {failures} attempts: {exc}') from exc
                wait = float(str(exc).rsplit('|',1)[1]) if isinstance(exc,Transient) and '|' in str(exc) else min(15,2 ** failures)
                self.store.event('retry', f'{target}: {type(exc).__name__}, retry {failures}, waiting {wait:.1f}s')
                time.sleep(wait)

    def check_robots(self):
        try:
            response = self.request('https://lex.uz/robots.txt', enforce_robots=False)
        except Missing:
            self.store.event('info','robots.txt is unavailable (404/410); recorded, not treated as a grant of permission')
            self.robots = None; return
        content = response.raw.decode('utf-8',errors='replace')
        if '<html' in content.lower():
            raise Blocked('robots.txt returned HTML instead of rules; inspect before continuing')
        parser = RobotFileParser(); parser.parse(content.splitlines()); self.robots = parser
        delay = parser.crawl_delay(self.options['user_agent']) or parser.crawl_delay('*') or 0
        self.delay = max(self.delay, delay)
