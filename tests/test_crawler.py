import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
import requests
from lex_crawler import parsing as p
from lex_crawler.engine import DEFAULTS, Engine
from lex_crawler.http import Client, Response, StopRun, Blocked, retry_seconds, challenge
from lex_crawler.storage import Store

FIXTURES = Path(__file__).parent / 'fixtures'
def fixture(name): return gzip.decompress((FIXTURES / (name+'.html.gz')).read_bytes())

class Parsers(unittest.TestCase):
    def test_real_document_lossless_structure(self):
        d=p.document(fixture('document'),'https://lex.uz/docs/8299357')
        self.assertEqual(d['content_block_count'],539);self.assertEqual(len(d['tables']),9)
        self.assertEqual(len(p.soup_of(d['content_html']).select('sup')),60)
        self.assertTrue(any(c['rowspan']!='1' for t in d['tables'] for row in t['rows'] for c in row))
        self.assertNotIn('Ҳужжатга таклиф юбориш',d['text']);self.assertTrue(d['annotations'])
    def test_real_homepage_scope(self):
        home=p.homepage(fixture('home'))
        self.assertEqual(set(home['categories']),set(p.CATEGORIES))
        self.assertEqual(home['languages'],['1','2','3','4']);self.assertGreaterEqual(len(home['seeds']),24)
    def test_webforms_current_state(self):
        url='https://lex.uz/search/official?lang=3&pub_date=month'
        a=p.listing(fixture('listing1'),url);b=p.listing(fixture('listing2'),url)
        self.assertEqual(len(a['ids']),20);self.assertEqual(len(b['ids']),20)
        self.assertFalse(set(a['ids']) & set(b['ids']))
        fields=dict(a['next']['data']);self.assertIn('__VIEWSTATE',fields)
        self.assertEqual(fields['__EVENTTARGET'],'ucFoundActsControl$LinkButton1')
    def test_partition_uses_actual_years(self):
        url='https://lex.uz/search/nat?lang=3';page=p.listing(fixture('national'),url)
        parts=p.partitions(url,page);self.assertEqual(len(parts),len(page['years']))
        self.assertTrue(all('fyear=' in u for u in parts))
    def test_date_split_is_complete_and_disjoint(self):
        parts=p.partitions('https://lex.uz/search/nat?lang=3&from=01.01.2026&to=04.01.2026',{})
        self.assertIn('to=02.01.2026',parts[0]);self.assertIn('from=03.01.2026',parts[1])
        self.assertEqual(p.partitions('https://lex.uz/search/nat?from=01.01.2026&to=01.01.2026',{}),[])
    def test_canonical_identity(self):
        self.assertEqual(p.document_url('/docs/-8299357#123'),'https://lex.uz/docs/-8299357')
        self.assertEqual(p.document_url('/docs/4969812?ONDATE=27.08.2020%2000#x'),p.document_url('/docs/4969812?ONDATE=27.08.2020'))
        self.assertIsNone(p.document_url('/docs/1?type=doc'));self.assertIsNone(p.document_url('/docs/1?action=compare&ONDATE2=x'))
        self.assertIsNone(p.safe_url('https://lex.uz.evil.example/docs/1'));self.assertIsNone(p.safe_url('https://lex.uz:444/docs/1'))
    def test_empty_or_error_is_not_success(self):
        with self.assertRaises(p.LayoutError):p.document(b'<h1>Error</h1>','https://lex.uz/docs/1')
        with self.assertRaises(p.LayoutError):p.listing(b'<form id="Form1"></form>','https://lex.uz/search/nat')
        page=p.listing(b'<div class="refind__result-export__title">0 hujjat</div>','https://lex.uz/search/nat')
        self.assertEqual(page['total'],0)
    def test_form_fields_preserve_repeated_names(self):
        form=p.soup_of('<form><input name="x" value="1"><input name="x" value="2"><input type="checkbox" name="a" checked><input type="checkbox" name="b"><input name="c" disabled><select name="d"><option value="z">Z</option></select></form>').form
        self.assertEqual(p.form_fields(form),[('x','1'),('x','2'),('a',''),('d','z')])
    def test_historical_controls_are_discovered(self):
        links=p.extract_links(p.soup_of(fixture('history')),'https://lex.uz/docs/4969812')
        self.assertTrue(any('ONDATE=' in x['url'] and x['kind']=='onclick' for x in links))
    def test_normal_recaptcha_widget_is_not_a_block(self):
        self.assertFalse(challenge(fixture('document'),'text/html'))
        self.assertTrue(challenge(b'<title>Just a moment...</title>','text/html'))

class PdfTests(unittest.TestCase):
    def test_pdf_only_document_is_collected_not_rejected(self):
        d=p.document(fixture('pdf-only'),'https://lex.uz/docs/8509858')
        self.assertEqual(d['representation'],'pdf_only')
        self.assertEqual(d['primary_pdf_urls'],['https://lex.uz/pdffile/8509858'])
        self.assertEqual(d['content_block_count'],0)
    def test_localized_inline_pdf_paths(self):
        links=p.extract_links(p.soup_of('<script>PDFObject.embed("/uz/pdffile/42", "#pdfBody")</script>'),p.HOME)
        self.assertEqual(p.safe_url(links[0]['url']),'https://lex.uz/pdffile/42')
    def test_pdf_extraction_flags_low_text_and_keeps_unicode(self):
        from types import SimpleNamespace
        from lex_crawler.pdf_text import extract
        pages=[SimpleNamespace(extract_text=lambda:'Ўзбекистон Республикаси '+('law '*30)),SimpleNamespace(extract_text=lambda:'')]
        with patch('lex_crawler.pdf_text.PdfReader',return_value=SimpleNamespace(is_encrypted=False,pages=pages)):
            result=extract('unused.pdf')
        self.assertEqual(result['status'],'review_low_text_pages')
        self.assertEqual(result['low_text_pages'],[2]);self.assertIn('Ўзбекистон',result['pages'][0]['text'])
    def test_extraction_result_is_read_back_as_utf8(self):
        # Regression: text.json is written as UTF-8 but was read with the platform default (cp1252 on
        # Windows), so any PDF with real Cyrillic text ("с" = D1 81) failed with UnicodeDecodeError.
        from types import SimpleNamespace
        from unittest.mock import Mock
        from lex_crawler.storage import write_json
        with tempfile.TemporaryDirectory() as tmp:
            pdf=Path(tmp)/'file.pdf';pdf.write_bytes(b'%PDF-1.4')
            def fake_run(*a,**k):
                write_json(pdf.with_name('text.json'),{'status':'text_extracted','pages':[{'page':1,'text':'Ўзбекистон Республикаси','characters':23}]})
                return SimpleNamespace(returncode=0,stderr='')
            fake=SimpleNamespace(options={'pdf_max_pages':10,'pdf_timeout':5},store=Mock())
            with patch('lex_crawler.engine.subprocess.run',side_effect=fake_run):
                Engine.extract_pdf_text(fake,{'id':1,'state':'{}'},pdf)
            self.assertEqual(fake.store.update.call_args.kwargs['state']['pdf_text'],'text_extracted')
    def test_pdf_password_is_not_bypassed(self):
        from types import SimpleNamespace
        from lex_crawler.pdf_text import extract
        with patch('lex_crawler.pdf_text.PdfReader',return_value=SimpleNamespace(is_encrypted=True)):
            self.assertEqual(extract('unused.pdf')['status'],'encrypted')

class ZipAssetTests(unittest.TestCase):
    """Some acts are a wrapper page + `/files/<n>.zip` that holds the real PDF (lex.uz/docs/5875370)."""
    PAGE = ('<div id="divCont"><div class="ACT_TITLE lx_elem" id="1">T</div>'
            '<div class="COMMENT_FOR_WARNING"><div id="2">Hujjat <a href="/files/5875605.zip">matni</a> PDF shaklida berilgan.</div></div></div>').encode()

    def test_page_lists_the_zip_as_a_content_asset(self):
        d=p.document(self.PAGE,'https://lex.uz/docs/5875370')
        self.assertEqual(d['assets'],['https://lex.uz/files/5875605.zip'])

    def test_downloaded_zip_is_saved_as_file_zip_not_rejected(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp);tid=s.enqueue('https://lex.uz/files/5875605.zip','asset',10)
            engine=Engine(s,None,dict(DEFAULTS))
            raw=b'PK\x03\x04'+b'\0'*64
            resp=SimpleNamespace(raw=raw,url='https://lex.uz/files/5875605.zip',status=200,headers={'Content-Type':'application/zip'},digest='ab'*32)
            engine.save_resource(s.task(tid),resp)
            folder=Path(tmp)/'resources'/str(tid)
            self.assertEqual((folder/'file.zip').read_bytes(),raw)
            self.assertEqual(json.loads((folder/'source.json').read_text(encoding='utf-8'))['content_type'],'application/zip')
            s.close()

class ImageTests(unittest.TestCase):
    """lex.uz renders formulas as tiny inline PNGs (thousands per run, mostly answered with an HTML page) and adds a
    favicon; they are not collected unless asked for, but PDFs/zips/Word files linked from the act still are."""
    PAGE = ('<div id="divCont"><div class="ACT_TITLE lx_elem" id="1">T</div>'
            '<div class="ACT_TEXT lx_elem" id="2">Omil <img height="19" src="/files/8439506.png" width="13"/> qiymati '
            '<img src="/image/favicon.gif"/> va <a href="/files/5875605.zip">matni</a>, <a href="/files/9.jpg">rasm</a>.</div></div>').encode()

    def saved(self, options):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp);tid=s.enqueue('https://lex.uz/docs/77','document',10)
            engine=Engine(s,None,{**DEFAULTS,**options})
            engine.save_document(s.task(tid),SimpleNamespace(raw=self.PAGE,url='https://lex.uz/docs/77',status=200,headers={},digest='cd'*32))
            urls={r['url'] for r in s.db.execute("SELECT url FROM tasks WHERE kind='asset'")}
            s.close()
        return urls

    def test_images_are_not_queued_by_default_but_the_zip_still_is(self):
        self.assertEqual(self.saved({}),{'https://lex.uz/files/5875605.zip'})

    def test_images_can_be_switched_on(self):
        self.assertEqual(len(self.saved({'images':True})),4)

    def test_earlier_queued_and_failed_images_are_closed_without_a_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp)
            ids={u:s.enqueue(u,'asset',10) for u in ('https://lex.uz/files/1.png','https://lex.uz/files/2.PNG','https://lex.uz/image/favicon.gif','https://lex.uz/files/3.zip','https://lex.uz/files/4.jpg')}
            s.update(ids['https://lex.uz/files/4.jpg'],status='failed',error='LayoutError: Content asset returned HTML instead of its payload')
            Engine(s,None,dict(DEFAULTS)).skip_images()
            status={r['url']:r['status'] for r in s.db.execute("SELECT url,status FROM tasks")}
            report=s.report(persist=False)
            s.close()
        self.assertEqual({u.rsplit('/',1)[1]:v for u,v in status.items()},{'1.png':'skipped','2.PNG':'skipped','favicon.gif':'skipped','3.zip':'pending','4.jpg':'skipped'})
        self.assertEqual(report['issues'],[])  # the failures no longer read as unresolved problems

    def test_a_query_string_or_case_does_not_hide_an_image(self):
        from lex_crawler.engine import is_image_url
        self.assertTrue(is_image_url('https://lex.uz/files/1.PNG?x=1'))
        self.assertFalse(is_image_url('https://lex.uz/files/1.pdf'))
        self.assertFalse(is_image_url('https://lex.uz/files/12345'))

class SeedTests(unittest.TestCase):
    def test_seed_queues_only_real_lex_document_urls_ahead_of_the_backlog(self):
        from lex_crawler.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            code=main(['--data',tmp,'seed','https://lex.uz/uz/docs/5875370','https://lex.uz/docs/-8488547?ONDATE=01.07.2020 00','https://evil.example/docs/1','not a url'])
            self.assertEqual(code,0)
            s=Store(tmp)
            rows={r['url']:(r['kind'],r['priority']) for r in s.db.execute('SELECT url,kind,priority FROM tasks')}
            s.close()
        self.assertEqual(rows['https://lex.uz/docs/5875370'],('document',5))
        self.assertIn('https://lex.uz/docs/-8488547?ONDATE=01.07.2020',rows)
        self.assertEqual(len(rows),2)  # nothing else got in
    def test_seed_with_nothing_valid_fails(self):
        from lex_crawler.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(['--data',tmp,'seed','https://example.com/x']),2)

class StatisticsTests(unittest.TestCase):
    """lex.uz's own statistics page, used to measure how much of the database the archive covers."""
    def raw(self): return gzip.decompress((FIXTURES/'statistic.html.gz').read_bytes())

    def test_real_page_parses_and_is_internally_consistent(self):
        from lex_crawler.statistics import parse_statistics
        r=parse_statistics(self.raw())
        self.assertEqual((r['total_documents'],r['header_total']),(165212,165212))
        self.assertTrue(r['consistent'],r['problems'])
        self.assertEqual(len(r['categories']),11)
        self.assertEqual(sum(c['total'] for c in r['categories']),165212)
        law=next(x for x in r['rows'] if x['category']=='Qonun hujjatlari' and x['is_total'])
        self.assertEqual(law['ru']+law['uz_cyrl']+law['uz_latn']+law['en'],law['total'])  # each language variant is its own document
        self.assertEqual(law['in_force']+law['new_edition']+law['lost_force'],law['total'])
        form=next(x for x in r['rows'] if x['category']=='Qonun hujjatlari' and x['form']=='Kodeks')
        self.assertEqual((form['total'],form['en']),(95,10))  # rowspan-carried category is filled in for every row

    def test_a_misread_number_is_reported_not_stored_silently(self):
        from lex_crawler.statistics import parse_statistics
        raw=self.raw().replace(b'JAMI</th><th class="st-table__num">24671<',b'JAMI</th><th class="st-table__num">24670<',1)
        self.assertIn(b'>24670<',raw)
        r=parse_statistics(raw)
        self.assertFalse(r['consistent'])
        self.assertTrue(any('Qonun hujjatlari' in x or 'header' in x for x in r['problems']),r['problems'])

    def test_layout_change_fails_loudly(self):
        from lex_crawler.parsing import LayoutError
        from lex_crawler.statistics import parse_statistics
        with self.assertRaises(LayoutError): parse_statistics(b'<html><body>no table here</body></html>')
        with self.assertRaises(LayoutError): parse_statistics(b'<table><tr><td>a</td><td>b</td></tr></table>')

    def test_stats_command_saves_a_timestamped_file_and_latest(self):
        from types import SimpleNamespace
        from lex_crawler.cli import main
        raw=self.raw()
        class FakeClient:
            def __init__(self,*a,**k):pass
            def check_robots(self):pass
            def request(self,url,task_id=0,**k):
                assert url=='https://lex.uz/uz/statistic',url
                return SimpleNamespace(raw=raw,digest='ab'*32)
        with tempfile.TemporaryDirectory() as tmp:
            with patch('lex_crawler.cli.Client',FakeClient):
                self.assertEqual(main(['--data',tmp,'stats']),0)
            files=sorted(p.name for p in (Path(tmp)/'source_stats').iterdir())
            latest=json.loads((Path(tmp)/'source_stats'/'latest.json').read_text(encoding='utf-8'))
        self.assertIn('latest.json',files); self.assertEqual(len(files),2)
        self.assertEqual((latest['total_documents'],latest['consistent']),(165212,True))
        self.assertIn('captured_at',latest)

class StorageTests(unittest.TestCase):
    def test_blob_dedupe_integrity_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp);tid=s.enqueue('https://lex.uz/docs/1','document',20)
            self.assertEqual(tid,s.enqueue('https://lex.uz/docs/1','document',20))
            digest=s.blob(b'hello');self.assertEqual(s.read_blob(digest),b'hello')
            s.update(tid,status='running');s.close();s=Store(tmp);s.recover()
            self.assertEqual(s.task(tid)['status'],'pending')
            (s.root/'blobs'/digest[:2]/(digest+'.gz')).write_bytes(gzip.compress(b'wrong'))
            with self.assertRaises(ValueError):s.read_blob(digest)
            s.close()
    def test_failed_work_prevents_complete_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp);tid=s.enqueue('https://lex.uz/docs/1','document',20);s.update(tid,status='failed',error='bad markup')
            report=s.report();self.assertFalse(report['all_discovered_resources_saved']);self.assertFalse(report['discovered_search_totals_reconciled']);s.close()

HOME=b'<select id="lang_1"><option value="3">UZ</option></select><script>var search_type="/search/nat";</script>'
def mini_page(doc_id,next_page=True,total=2):
    link='<div class="item_pgn4"><a href="javascript:__doPostBack(\'next\',\'\')">Next</a></div>' if next_page else ''
    return (f'<div class="refind__result-export__title">{total} hujjat</div><a class="lx_link" href="/docs/{doc_id}">Title</a>'
            f'<form id="Form1" action="?lang=3"><input type="hidden" name="__VIEWSTATE" value="state-{doc_id}">{link}</form>').encode()
class FakeClient:
    def __init__(self,store):self.store=store;self.session=requests.Session();self.calls=[]
    def check_robots(self):pass
    def request(self,url,task_id=0,method='GET',data=None,conditional=False):
        self.calls.append((url,method,data))
        if url==p.HOME:raw=HOME
        elif '/search/' in url:raw=mini_page('8299357') if method=='GET' else mini_page('8300887',False)
        else:raw=fixture('document')
        digest=self.store.response(task_id,method,url,200,{'Content-Type':'text/html'},raw)
        return Response(raw,url,200,{'Content-Type':'text/html'},digest)

class EngineTests(unittest.TestCase):
    def options(self):return {**DEFAULTS,**{x:False for x in ('metadata','pdf','word','assets','history','variants','references')}}
    def test_homepage_resume_and_second_page_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp);client=FakeClient(s);engine=Engine(s,client,self.options())
            with self.assertRaises(StopRun):engine.run(max_documents=1)
            self.assertEqual(engine.documents_this_run,1);s.close()
            s=Store(tmp);client2=FakeClient(s);engine2=Engine(s,client2,self.options());engine2.run()
            self.assertEqual(engine2.documents_this_run,1)
            posts=[c for c in client2.calls if c[1]=='POST'];self.assertEqual(len(posts),1)
            self.assertEqual(dict(posts[0][2])['__VIEWSTATE'],'state-8299357')
            self.assertTrue(s.report()['discovered_search_totals_reconciled'])
            self.assertEqual(s.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='document' AND status='done'").fetchone()[0],2);s.close()
    def test_partition_mismatch_falls_back_to_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp);parent=s.enqueue('https://lex.uz/search/nat?lang=3','listing',40);child=s.enqueue('https://lex.uz/search/nat?lang=3&fyear=2026','listing',41)
            s.update(parent,status='split',state={'total':2,'page':1});s.update(child,status='done')
            s.db.execute('INSERT INTO query_children VALUES(?,?)',(parent,child));s.db.execute('INSERT INTO query_ids VALUES(?,?)',(child,'1'));s.db.commit()
            Engine(s,FakeClient(s),self.options()).reconcile();row=s.task(parent)
            self.assertEqual(row['status'],'pending');self.assertTrue(json.loads(row['state'])['force_paging']);s.close()
    def test_repeated_page_restarts_from_fresh_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp);tid=s.enqueue('https://lex.uz/search/nat?lang=3','listing',40)
            client=FakeClient(s);engine=Engine(s,client,self.options());engine.search(s.task(tid));original=client.request
            client.request=lambda url,task_id=0,**kw:original(url,task_id,method='GET')
            engine.search(s.task(tid));self.assertEqual(json.loads(s.task(tid)['state'])['restarts'],1)
            self.assertEqual(s.query_count(tid),0);s.close()

class FakeResponse:
    def __init__(self,status=200,headers=None,body=b'OK'):self.status_code=status;self.headers=headers or {};self.body=body
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_content(self,size):yield self.body
class HttpTests(unittest.TestCase):
    def client(self,folder):
        s=Store(folder);return s,Client(s,{**DEFAULTS,'delay':0})
    def test_retry_after_numeric_and_http_date(self):
        self.assertEqual(retry_seconds('120'),120)
        seconds=retry_seconds(format_datetime(datetime.now(timezone.utc)+timedelta(seconds=60)))
        self.assertTrue(58<=seconds<=61)
    def test_429_saves_global_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,c=self.client(tmp)
            with patch.object(c.session,'request',return_value=FakeResponse(429,{'Retry-After':'120'})):
                with self.assertRaises(StopRun):c.request('https://lex.uz/docs/1')
            self.assertGreater(s.get_meta('not_before'),0);s.close()
    def test_403_and_external_redirect_stop(self):
        for response in (FakeResponse(403),FakeResponse(302,{'Location':'https://example.com/docs/1'})):
            with tempfile.TemporaryDirectory() as tmp:
                s,c=self.client(tmp)
                with patch.object(c.session,'request',return_value=response):
                    with self.assertRaises(Blocked):c.request('https://lex.uz/docs/1')
                s.close()
    def test_transport_retry_keeps_tls_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,c=self.client(tmp)
            with patch.object(c.session,'request',side_effect=[requests.ConnectionError('offline'),FakeResponse()]) as mock,patch('lex_crawler.http.time.sleep'):
                self.assertEqual(c.request('https://lex.uz/docs/1').raw,b'OK');self.assertEqual(mock.call_count,2)
                self.assertNotIn('verify',mock.call_args.kwargs)
            s.close()
    def test_304_restores_body_and_mime_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,c=self.client(tmp);tid=s.enqueue('https://lex.uz/docs/1?type=doc','word',10)
            digest=s.response(tid,'GET','https://lex.uz/docs/1?type=doc',200,{'Content-Type':'application/msword','ETag':'abc'},b'DOC');s.update(tid,source_hash=digest)
            with patch.object(c.session,'request',return_value=FakeResponse(304,{},b'')):
                result=c.request('https://lex.uz/docs/1?type=doc',tid,conditional=True)
            self.assertEqual(result.raw,b'DOC');self.assertEqual(result.headers['content-type'],'application/msword');s.close()

if __name__=='__main__':unittest.main()
