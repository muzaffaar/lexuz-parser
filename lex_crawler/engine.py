import hashlib
import json
import time
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
import requests
from . import parsing as p
from .http import Blocked, Missing, RobotsDenied, StopRun, Transient
from .storage import atomic_write, write_json

DEFAULTS = {'delay':1.5, 'user_agent':'LexPublicArchive/1.0', 'max_response_mb':64,
            'http_attempts':4, 'pdf_text':True, 'pdf_timeout':90, 'pdf_max_pages':2000, 'partition_threshold':1000, 'max_query_pages':500,
            'metadata':True, 'pdf':True, 'word':True, 'assets':True, 'images':False,
            'history':True, 'variants':True, 'references':True}
IMAGE_EXTENSIONS = ('png','jpg','jpeg','gif','svg','webp','bmp','ico')


def is_image_url(url):
    return urlsplit(url).path.lower().rsplit('.',1)[-1] in IMAGE_EXTENSIONS


class Engine:
    def __init__(self, store, client, options):
        self.store = store; self.client = client; self.options = options
        self.documents_this_run = 0

    def route(self, item, source, priority=20, relation='link'):
        raw = item['url'] if isinstance(item,dict) else item
        kind_hint = item.get('kind','href') if isinstance(item,dict) else 'href'
        url = p.safe_url(raw, source)
        if not url:
            return
        resource_priority = 30 if relation in ('homepage','search_result') else 10
        path = urlsplit(url).path; match = p.DOC_PATH.fullmatch(path)
        q = {k.lower():v for k,v in parse_qsl(urlsplit(url).query)}
        if match and match[1] == 'docs':
            if q.get('type') == 'doc':
                if self.options['word']:
                    self.store.enqueue(url,'word',resource_priority,source,'word_export')
                return
            doc = p.document_url(url)
            if not doc:
                return
            historical = 'ondate' in q
            if historical and not self.options['history']:
                return
            is_control = kind_hint == 'onclick'
            if relation not in ('search_result','homepage'):
                if is_control and not self.options['variants'] and p.identity(doc) != p.identity(source):
                    return
                if not is_control and not self.options['references']:
                    return
            category = 'edition' if historical else ('language' if is_control else relation)
            self.store.enqueue(doc,'document',priority,source,category)
        elif match and match[1] == 'pdfs' and self.options['pdf']:
            self.store.enqueue(url,'pdf_viewer',resource_priority+2,source,'pdf_viewer')
        elif match and match[1] == 'pdffile' and self.options['pdf']:
            self.store.enqueue(url,'pdf',resource_priority,source,'pdf_file')
        elif (path.startswith('/actinfo/') or (match and match[1] == 'doc-passport')) and self.options['metadata']:
            self.store.enqueue(url,'metadata',resource_priority,source,'metadata')

    def home(self, task, response):
        result = p.homepage(response.raw, response.url)
        self.store.set_meta('homepage_scope', {k:result[k] for k in ('categories','languages','seeds')})
        for url in result['seeds']:
            self.store.enqueue(p.safe_url(url),'listing',40,task['url'],'homepage_search')
        for item in result['links']:
            self.route(item,task['url'],20,'homepage')
        self.store.event('info',f'Homepage: {len(result["categories"])} categories, {len(result["languages"])} languages, {len(result["seeds"])} searches')
        self.store.update(task['id'],status='done',source_hash=response.digest,error=None)

    def split_query(self, task, state, page):
        if state.get('force_paging'):
            return False
        children = p.partitions(task['url'], page)
        if not children:
            return False
        for url in children:
            child = self.store.enqueue(p.safe_url(url),'listing',task['priority'] + 1,task['url'],'search_partition')
            self.store.db.execute('INSERT OR IGNORE INTO query_children VALUES(?,?)',(task['id'],child))
        self.store.db.commit()
        self.store.update(task['id'],status='split',state=state,error=None)
        self.store.event('info',f'Split {task["url"]} into {len(children)} smaller searches')
        return True

    def restart_query(self, task, state, reason):
        restarts = state.get('restarts',0)
        if restarts >= 2:
            raise p.LayoutError(f'Search failed after two fresh-session restarts: {reason}')
        self.store.reset_query(task['id'])
        reset = {'restarts':restarts+1, 'force_paging':state.get('force_paging',False)}
        self.store.update(task['id'],status='pending',state=reset,error=None)
        self.store.event('retry',f'Restarting search from a fresh GET after {reason}; document URLs remain deduplicated')

    def search(self, task):
        state = json.loads(task['state'])
        number = state.get('page',0) + 1
        try:
            if state.get('next'):
                self.client.session.cookies.clear()
                self.client.session.cookies.update(state.get('cookies',{}))
                nxt = state['next']
                response = self.client.request(nxt['url'],task['id'],method='POST',data=nxt['data'])
            else:
                response = self.client.request(task['url'],task['id'])
            page = p.listing(response.raw,response.url)
            if not self.store.add_page(task['id'],number,page,response.digest):
                return self.restart_query(task,state,'a repeated page or expired form state')
        except (p.LayoutError, ValueError) as exc:
            if state.get('next'):
                return self.restart_query(task,state,str(exc))
            raise
        previous = state.get('total')
        if previous is not None and page['total'] is not None and previous != page['total']:
            state['totals_changed'] = True
        state.update(page=number, total=page['total'], next=page['next'],
                     cookies=requests.utils.dict_from_cookiejar(self.client.session.cookies))
        self.store.update(task['id'],state=state,source_hash=response.digest)
        for item in page['links']:
            self.route(item,task['url'],20,'search_result')
        found = self.store.query_count(task['id'])
        self.store.event('page',f'{task["url"]}: page {number}, {found}/{page["total"] if page["total"] is not None else "?"} IDs')
        if number == 1 and page['total'] is not None and page['total'] > self.options['partition_threshold']:
            if self.split_query(task,state,page):
                return
        if number >= self.options['max_query_pages'] and page['next']:
            if self.split_query(task,state,page):
                return
            raise p.LayoutError('Page safety limit reached; increase max_query_pages or subdivide this query')
        if not page['next']:
            if page['total'] is not None and found != page['total']:
                if self.split_query(task,state,page):
                    return
                raise p.LayoutError(f'Pagination ended with a coverage mismatch: {found} IDs versus {page["total"]} reported')
            self.store.update(task['id'],status='done',state=state,error=None)
        else:
            self.store.update(task['id'],status='pending',state=state,error=None)

    def save_document(self, task, response):
        record = p.document(response.raw,task['url'])
        record.update(retrieved_at=time.time(),source_sha256=response.digest,
                      text_sha256=hashlib.sha256(record['text'].encode()).hexdigest())
        folder = self.store.root / 'documents' / f'{record["document_id"]}_{task["id"]}'
        folder.mkdir(parents=True,exist_ok=True)
        atomic_write(folder / 'content.html',record.pop('content_html').encode())
        atomic_write(folder / 'text.txt',record.pop('text').encode())
        record['source_blob'] = (Path('blobs') / response.digest[:2] / (response.digest+'.gz')).as_posix()  # portable between OSes
        write_json(folder / 'snapshots' / (response.digest+'.json'),record)
        write_json(folder / 'document.json',record)
        for url in record.get('primary_pdf_urls',[]):
            self.store.enqueue(url,'pdf',10,task['url'],'primary_pdf')
        for item in record['links']:
            is_control = item['kind'] == 'onclick'
            priority = 22 if is_control else 60
            self.route(item,task['url'],priority,'citation')
        if self.options['assets']:
            for url in record['assets']:
                if self.options.get('images') or not is_image_url(url):
                    self.store.enqueue(url,'asset',10,task['url'],'content_asset')
        self.store.update(task['id'],status='done',source_hash=response.digest,error=None)
        self.documents_this_run += 1
        self.store.event('document',f'{task["url"]}: {record["representation"]}, {record["content_block_count"]} blocks, {len(record["tables"])} tables')

    def save_resource(self, task, response):
        kind = task['kind']; raw = response.raw
        folder = self.store.root / 'resources' / str(task['id'])
        content_type = response.headers.get('Content-Type',response.headers.get('content-type','')).lower()
        if kind == 'pdf_viewer':
            links = p.extract_links(p.soup_of(raw),response.url)
            targets = [x for x in links if '/pdffile/' in x['url']]
            # Some viewers embed a file URL in iframe/object attributes instead of JS.
            for n in p.soup_of(raw).select('iframe[src],embed[src],object[data]'):
                value = n.get('src') or n.get('data')
                if '/pdffile/' in value:
                    targets.append({'url':value,'kind':'embed'})
            if not targets:
                raise p.LayoutError('PDF viewer has no observed PDF resource URL')
            for item in targets:
                self.route(item,task['url'],10,'pdf_file')
            atomic_write(folder / 'viewer.html',raw)
        elif kind == 'metadata':
            record = p.supplemental(raw,response.url)
            if not record['text'] or '404.' in record['title']:
                raise p.LayoutError('Metadata returned an empty/error page')
            atomic_write(folder / 'page.html',raw)
            write_json(folder / 'metadata.json',record)
            for item in record['links']:
                self.route(item,task['url'],60,'metadata_reference')
            if self.options['assets']:
                # Passport images are meaningful; omit menus, logos and tracking images.
                bs = p.soup_of(raw)
                main = bs.select_one('main')
                if main:
                    for n in main.select('img[src]'):
                        url = p.safe_url(n['src'],response.url)
                        if url and '/img/' not in urlsplit(url).path and (self.options.get('images') or not is_image_url(url)):
                            self.store.enqueue(url,'asset',10,task['url'],'metadata_asset')
        else:
            if kind == 'pdf' and not raw.startswith(b'%PDF-'):
                raise p.LayoutError('Expected PDF bytes; received another payload')
            if kind == 'word' and not any(x in content_type for x in ('msword','wordprocessingml','octet-stream')):
                raise p.LayoutError('Expected a Word export; received '+content_type)
            if kind == 'asset' and 'text/html' in content_type:
                raise p.LayoutError('Content asset returned HTML instead of its payload')
            extension = '.pdf' if kind == 'pdf' else '.doc' if kind == 'word' else Path(urlsplit(task['url']).path).suffix
            if not extension or len(extension)>12:
                extension = '.bin'
            atomic_write(folder / ('file'+extension),raw)
            if kind == 'pdf' and self.options['pdf_text']:
                self.extract_pdf_text(task,folder / ('file'+extension))
        write_json(folder / 'source.json',{'url':task['url'],'final_url':response.url,'kind':kind,
                                         'sha256':response.digest,'retrieved_at':time.time(),
                                         'bytes':len(raw),'content_type':content_type})
        self.store.update(task['id'],status='done',source_hash=response.digest,error=None)

    def extract_pdf_text(self,task,path):
        # Parsing untrusted PDFs is bounded independently from the network worker.
        result_path = path.with_name('text.json')
        try:
            result = subprocess.run([sys.executable,'-m','lex_crawler.pdf_text',str(path),
                                     '--max-pages',str(self.options['pdf_max_pages'])],
                                    timeout=self.options['pdf_timeout'],capture_output=True,text=True,encoding='utf-8',errors='replace')
            if result.returncode:
                write_json(result_path,{'status':'extraction_failed','error':result.stderr[-2000:]})
            state = json.loads(task['state'])
            state['pdf_text'] = json.loads(result_path.read_text(encoding='utf-8')).get('status','unknown')
            self.store.update(task['id'],state=state)
        except subprocess.TimeoutExpired:
            write_json(result_path,{'status':'extraction_timeout','seconds':self.options['pdf_timeout']})
            self.store.update(task['id'],state={**json.loads(task['state']),'pdf_text':'extraction_timeout'})

    def reconcile(self):
        changed = False
        for task in list(self.store.db.execute("SELECT * FROM tasks WHERE status='split'")):
            children = list(self.store.db.execute('SELECT t.status FROM tasks t JOIN query_children c ON t.id=c.child WHERE c.parent=?',(task['id'],)))
            if not children or any(r['status'] in ('pending','running','split') for r in children):
                continue
            state = json.loads(task['state'])
            found = self.store.query_count(task['id'],descendants=True)
            successful = all(r['status'] in ('done','covered') for r in children)
            if successful and state.get('total') == found:
                self.store.update(task['id'],status='covered',error=None)
                changed = True
            elif not state.get('force_paging'):
                # Facets may omit undated records. Page the original broad query as a fallback.
                state.update(force_paging=True,page=0,next=None)
                self.store.reset_query(task['id'])
                self.store.update(task['id'],status='pending',state=state,error=None)
                self.store.event('retry',f'Partition counts did not reconcile for {task["url"]}; paging the original query as a fallback')
                changed = True
            else:
                self.store.update(task['id'],status='failed',error='Partition coverage did not reconcile')
        return changed

    def skip_images(self):
        """Images are not collected by default: lex.uz shows formulas as tiny inline glyph PNGs (thousands per run,
        and the site answers most of them with an HTML page), plus its own favicon. Work queued or failed earlier
        is closed as 'skipped' so it is neither fetched nor reported as an unresolved problem."""
        conditions = ' OR '.join(f"lower(url) LIKE '%.{ext}'" for ext in IMAGE_EXTENSIONS)
        cursor = self.store.db.execute(
            f"UPDATE tasks SET status='skipped',error='images are not collected (run with --images to collect them)' "
            f"WHERE kind='asset' AND status IN ('pending','failed','running') AND ({conditions})")
        self.store.db.commit()
        if cursor.rowcount:
            self.store.event('info',f'Skipped {cursor.rowcount} queued/failed image download(s); images are off (use --images to collect them)')

    def run(self, max_documents=0):
        self.store.recover()
        if not self.options.get('images'):
            self.skip_images()
        self.store.enqueue(p.HOME,'home',0)
        self.client.check_robots()
        while True:
            self.reconcile()
            task = self.store.next_task()
            if not task:
                # Reconcile ancestors that became eligible during this pass.
                if self.reconcile():
                    continue
                return 'finished'
            if max_documents and self.documents_this_run >= max_documents:
                raise StopRun('Document budget reached; supplemental work and remaining URLs are saved for resume')
            self.store.update(task['id'],status='running',attempts=task['attempts']+1)
            try:
                if task['kind'] == 'listing':
                    self.search(task)
                else:
                    response = self.client.request(task['url'],task['id'],conditional=bool(task['source_hash']))
                    self.store.update(task['id'],source_hash=response.digest)
                    if task['kind'] == 'home':
                        self.home(task,response)
                    elif task['kind'] == 'document':
                        self.save_document(task,response)
                    else:
                        self.save_resource(task,response)
            except StopRun:
                self.store.update(task['id'],status='pending')
                raise
            except Blocked as exc:
                self.store.update(task['id'],status='blocked',error=str(exc))
                raise
            except RobotsDenied as exc:
                self.store.update(task['id'],status='excluded',error=str(exc))
                self.store.event('excluded',f'{task["url"]}: {exc}')
            except Missing as exc:
                self.store.update(task['id'],status='missing',error=str(exc))
                self.store.event('missing',f'{task["url"]}: {exc}')
            except Transient as exc:
                self.store.update(task['id'],status='pending',error=str(exc))
                raise StopRun('Repeated network/server failure; stopped the whole crawler to avoid hammering other URLs') from exc
            except (OSError,MemoryError):
                # Disk-full and local I/O failures must not be hidden as site errors.
                self.store.update(task['id'],status='pending')
                raise
            except Exception as exc:
                self.store.update(task['id'],status='failed',error=f'{type(exc).__name__}: {exc}')
                self.store.event('failed',f'{task["url"]}: {type(exc).__name__}: {exc}')
                if task['kind'] == 'home':
                    raise StopRun('Homepage could not be parsed. Response saved; review the parser before retrying')
