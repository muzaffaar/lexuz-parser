import gzip
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


def atomic_write(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    temporary.replace(path)


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'state.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY,url TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,priority INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT,source_hash TEXT,state TEXT NOT NULL DEFAULT '{}',updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,priority,id);
        CREATE TABLE IF NOT EXISTS edges(source TEXT,target TEXT,relation TEXT,
          PRIMARY KEY(source,target,relation));
        CREATE TABLE IF NOT EXISTS query_ids(task_id INTEGER,doc_id TEXT,PRIMARY KEY(task_id,doc_id));
        CREATE TABLE IF NOT EXISTS query_children(parent INTEGER,child INTEGER,PRIMARY KEY(parent,child));
        CREATE TABLE IF NOT EXISTS pages(task_id INTEGER,page INTEGER,signature TEXT,source_hash TEXT,
          PRIMARY KEY(task_id,page));
        CREATE TABLE IF NOT EXISTS responses(id INTEGER PRIMARY KEY,task_id INTEGER,method TEXT,url TEXT,
          status INTEGER,retrieved REAL,source_hash TEXT,headers TEXT,bytes INTEGER);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,created REAL,level TEXT,message TEXT);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def get_meta(self, key, default=None):
        r = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))
        self.db.commit()

    def event(self, level, message):
        self.db.execute('INSERT INTO events(created,level,message) VALUES(?,?,?)', (time.time(), level, message))
        self.db.commit()
        print(f'[{level}] {message}', flush=True)

    def enqueue(self, url, kind, priority, source=None, relation='link'):
        self.db.execute('INSERT OR IGNORE INTO tasks(url,kind,priority,updated) VALUES(?,?,?,?)',
                        (url, kind, priority, time.time()))
        self.db.execute('UPDATE tasks SET priority=MIN(priority,?) WHERE url=?', (priority, url))
        if source:
            self.db.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?)', (source, url, relation))
        self.db.commit()
        return self.db.execute('SELECT id FROM tasks WHERE url=?', (url,)).fetchone()[0]

    def update(self, task_id, **fields):
        fields['updated'] = time.time()
        if 'state' in fields:
            fields['state'] = json.dumps(fields['state'])
        allowed = {'status', 'attempts', 'error', 'source_hash', 'state', 'updated'}
        if not set(fields) <= allowed:
            raise ValueError('Unknown task field')
        self.db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                        (*fields.values(), task_id)); self.db.commit()

    def task(self, task_id):
        return self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()

    def next_task(self):
        return self.db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY priority,id LIMIT 1").fetchone()

    def recover(self):
        self.db.execute("UPDATE tasks SET status='pending' WHERE status='running'"); self.db.commit()

    def blob(self, raw):
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / 'blobs' / digest[:2] / (digest + '.gz')
        if not path.exists():
            atomic_write(path, gzip.compress(raw, mtime=0))
        return digest

    def read_blob(self, digest):
        path = self.root / 'blobs' / digest[:2] / (digest + '.gz')
        raw = gzip.decompress(path.read_bytes())
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Archive checksum mismatch: ' + digest)
        return raw

    def response(self, task_id, method, url, status, headers, raw):
        digest = self.blob(raw)
        keep = {k.lower(): v for k, v in headers.items() if k.lower() in
                ('content-type', 'content-disposition', 'etag', 'last-modified', 'retry-after', 'location')}
        self.db.execute('INSERT INTO responses(task_id,method,url,status,retrieved,source_hash,headers,bytes) VALUES(?,?,?,?,?,?,?,?)',
                        (task_id, method, url, status, time.time(), digest, json.dumps(keep), len(raw)))
        self.db.commit()
        return digest

    def validators(self, task_id):
        row = self.db.execute('SELECT headers FROM responses WHERE task_id=? AND status=200 ORDER BY id DESC LIMIT 1',
                              (task_id,)).fetchone()
        headers = json.loads(row[0]) if row else {}
        return {key: headers[source] for source, key in (('etag','If-None-Match'), ('last-modified','If-Modified-Since')) if source in headers}

    def add_page(self, task_id, number, page, digest):
        signature = hashlib.sha256('\n'.join(page['ids']).encode()).hexdigest()
        repeated = self.db.execute('SELECT page FROM pages WHERE task_id=? AND signature=? AND page<>?',
                                   (task_id, signature, number)).fetchone()
        if repeated and page['ids']:
            return False
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?)', (task_id, number, signature, digest))
            self.db.executemany('INSERT OR IGNORE INTO query_ids VALUES(?,?)', ((task_id, x) for x in page['ids']))
        return True

    def reset_query(self, task_id):
        with self.db:
            self.db.execute('DELETE FROM pages WHERE task_id=?', (task_id,))
            self.db.execute('DELETE FROM query_ids WHERE task_id=?', (task_id,))

    def query_count(self, task_id, descendants=False):
        if not descendants:
            return self.db.execute('SELECT COUNT(*) FROM query_ids WHERE task_id=?', (task_id,)).fetchone()[0]
        return self.db.execute('''WITH RECURSIVE tree(id) AS (
          SELECT ? UNION SELECT child FROM query_children JOIN tree ON parent=tree.id)
          SELECT COUNT(DISTINCT doc_id) FROM query_ids WHERE task_id IN (SELECT id FROM tree)''', (task_id,)).fetchone()[0]

    def report(self, persist=True):
        counts = [dict(r) for r in self.db.execute('SELECT kind,status,COUNT(*) count FROM tasks GROUP BY kind,status')]
        issues = [dict(r) for r in self.db.execute("SELECT id,url,kind,status,error FROM tasks WHERE status IN ('failed','blocked','missing','excluded') ORDER BY id")]
        queries = []
        for r in self.db.execute("SELECT * FROM tasks WHERE kind='listing'"):
            state = json.loads(r['state']); expected = state.get('total')
            found = self.query_count(r['id'], descendants=r['status'] in ('split', 'covered'))
            queries.append({'url':r['url'], 'status':r['status'], 'pages':state.get('page',0),
                            'expected':expected, 'discovered_ids':found,
                            'count_matches':found == expected if expected is not None else None,
                            'totals_changed':state.get('totals_changed',False)})
        pending = sum(x['count'] for x in counts if x['status'] in ('pending','running','split'))
        bad = any(x['status'] in ('failed','blocked','excluded') for x in counts)
        matched = bool(queries) and all(x['count_matches'] is True and not x['totals_changed'] for x in queries)
        text_review = [{'url':r['url'],'status':json.loads(r['state']).get('pdf_text')} for r in self.db.execute("SELECT url,state FROM tasks WHERE kind='pdf' AND status='done'") if json.loads(r['state']).get('pdf_text') not in (None,'text_extracted')]
        result = {'generated_at':time.time(), 'tasks':counts, 'unfinished_tasks':pending,
                  'queue_exhausted':not pending, 'all_discovered_resources_saved':not pending and not issues,
                  'discovered_search_totals_reconciled':not pending and not bad and matched,
                  'whole_site_completeness':'Not proven: this report covers exposed homepage searches and discovered links.',
                  'queries':queries, 'issues':issues, 'pdf_text_review':text_review,
                  'options':self.get_meta('options',{}), 'homepage_scope':self.get_meta('homepage_scope',{})}
        if persist:
            write_json(self.root / 'report.json', result)
            write_json(self.root / 'issues.json', issues)
        return result
