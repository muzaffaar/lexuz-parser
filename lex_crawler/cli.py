import argparse
import json
import sys
import time
from pathlib import Path
from filelock import FileLock, Timeout
from .engine import DEFAULTS, Engine
from .http import Blocked, Client, StopRun, Transient
from . import parsing
from .storage import Store, write_json


def build_parser():
    parser = argparse.ArgumentParser(description='Start at https://lex.uz/ and archive exposed public documents sequentially.')
    parser.add_argument('--data', type=Path, default=Path('data'), help='Persistent archive folder (default: ./data)')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='Start or resume the same saved crawl')
    run.add_argument('--max-requests',type=int,default=0,help='Network request limit for this invocation; 0 is unlimited')
    run.add_argument('--max-documents',type=int,default=0,help='Successful document limit for this invocation; 0 is unlimited')
    run.add_argument('--delay',type=float,default=None)
    run.add_argument('--user-agent',default=None)
    run.add_argument('--max-response-mb',type=int,default=None)
    run.add_argument('--pdf-timeout',type=int,default=None)
    run.add_argument('--pdf-max-pages',type=int,default=None)
    run.add_argument('--partition-threshold',type=int,default=None)
    run.add_argument('--max-query-pages',type=int,default=None)
    for name in ('metadata','pdf','word','assets','images','history','variants','references','pdf-text'):
        run.add_argument('--'+name,action=argparse.BooleanOptionalAction,default=None)
    sub.add_parser('status',help='Read current counts and unresolved issues, including while a crawl runs')
    retry = sub.add_parser('retry',help='Requeue failed or previously blocked work after addressing the cause')
    retry.add_argument('--status',choices=('failed','blocked','missing','excluded'),default='failed')
    seed = sub.add_parser('seed',help='Queue specific lex.uz document URLs (e.g. https://lex.uz/docs/5875370) so the next run fetches them first')
    seed.add_argument('urls',nargs='+')
    sub.add_parser('stats',help="Fetch lex.uz's own statistics page (ONE request) into <data>/source_stats/ for comparison with the archive")
    sub.add_parser('refresh',help='Recheck saved documents and restart completed searches on the next run')
    sub.add_parser('audit',help='Verify saved response checksums without any network requests')
    return parser


def summary(report):
    print(json.dumps({k:report[k] for k in ('tasks','unfinished_tasks','queue_exhausted',
                                          'all_discovered_resources_saved','discovered_search_totals_reconciled')},indent=2))
    print('Whole-site completeness is not proven; see report.json for per-query counts and issues.')


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.data.mkdir(parents=True,exist_ok=True)
    store = Store(args.data)
    lock = FileLock(str(args.data / '.crawler.lock'))
    acquired = False
    try:
        if args.command == 'status':
            summary(store.report(persist=False)); return 0
        try:
            lock.acquire(timeout=0); acquired = True
        except Timeout:
            print('Another crawler is using this data directory. Use status, or wait for it to finish.',file=sys.stderr)
            return 4
        if args.command == 'retry':
            rows = list(store.db.execute('SELECT id,kind FROM tasks WHERE status=?',(args.status,)))
            for row in rows:
                if row['kind'] == 'listing':
                    store.reset_query(row['id']); store.update(row['id'],state={})
                store.update(row['id'],status='pending',error=None)
            store.event('info',f'Requeued {len(rows)} {args.status} tasks. Run again to continue.')
            store.report(); return 0
        if args.command == 'seed':
            queued, rejected = 0, []
            for value in args.urls:
                url = parsing.document_url(value)
                if not url:
                    rejected.append(value); continue
                store.enqueue(url,'document',5,None)  # right after the homepage, before listings and other backlog
                queued += 1
            store.event('info',f'Seeded {queued} document URL(s). Run again to fetch them.')
            for value in rejected:
                print(f'Not a lex.uz document URL, ignored: {value}',file=sys.stderr)
            return 0 if queued else 2
        if args.command == 'stats':
            from datetime import datetime, timezone
            from .parsing import LayoutError
            from .statistics import STATISTICS_URL, parse_statistics
            options = dict(DEFAULTS); options.update(store.get_meta('options',{}))
            client = Client(store,options,3)
            try:
                client.check_robots()
                response = client.request(STATISTICS_URL,0)
                result = parse_statistics(response.raw)
            except LayoutError as exc:
                print(f'Statistics page layout changed; nothing saved (response is archived): {exc}',file=sys.stderr); return 2
            except Blocked as exc:
                print(f'Blocked: {exc}',file=sys.stderr); return 3
            except (StopRun,Transient) as exc:
                print(f'Could not fetch statistics now: {exc}',file=sys.stderr); return 2
            now = datetime.now(timezone.utc)
            result.update(captured_at=now.isoformat(),source_sha256=response.digest)
            folder = args.data / 'source_stats'
            write_json(folder / (now.strftime('%Y%m%dT%H%M%SZ')+'.json'),result)
            write_json(folder / 'latest.json',result)
            print(f"lex.uz reports {result['total_documents']:,} documents ({'internally consistent' if result['consistent'] else 'INCONSISTENT: '+'; '.join(result['problems'])}). Saved to {folder}")
            return 0 if result['consistent'] else 2
        if args.command == 'refresh':
            rows = list(store.db.execute("SELECT id,kind FROM tasks WHERE status IN ('done','covered','split')"))
            for row in rows:
                if row['kind'] == 'listing':
                    store.reset_query(row['id']); store.update(row['id'],state={})
                store.update(row['id'],status='pending',error=None)
            store.event('info',f'Requeued {len(rows)} tasks. Original response blobs and document snapshots remain saved.')
            store.report(); return 0
        if args.command == 'audit':
            hashes = [r[0] for r in store.db.execute('SELECT DISTINCT source_hash FROM responses')]
            failures = []
            for digest in hashes:
                try:
                    store.read_blob(digest)
                except Exception as exc:
                    failures.append({'sha256':digest,'error':str(exc)})
            result = {'checked_response_blobs':len(hashes),'failures':failures}
            write_json(store.root / 'audit.json',result)
            print(json.dumps(result,indent=2)); return 2 if failures else 0
        options = dict(DEFAULTS); options.update(store.get_meta('options',{}))
        for key in DEFAULTS:
            value = getattr(args,key,None)
            if value is not None:
                options[key] = value
        if options['delay'] < 0.5 or options['max_response_mb'] < 1 or options['partition_threshold'] < 20 or options['max_query_pages'] < 1 or options['pdf_timeout'] < 1 or options['pdf_max_pages'] < 1:
            print('Require delay >= 0.5, max-response-mb >= 1, partition-threshold >= 20, max-query-pages >= 1.',file=sys.stderr)
            return 2
        if args.max_requests < 0 or args.max_documents < 0:
            print('Limits must be nonnegative.',file=sys.stderr); return 2
        store.set_meta('options',options)
        outstanding_block = store.db.execute("SELECT url,error FROM tasks WHERE status='blocked' LIMIT 1").fetchone()
        if outstanding_block:
            print(f'Blocked task remains: {outstanding_block["url"]}\n{outstanding_block["error"]}\nResolve it, then use retry --status blocked.',file=sys.stderr)
            return 3
        client = Client(store,options,args.max_requests)
        engine = Engine(store,client,options)
        code = 0
        try:
            engine.run(args.max_documents)
        except (StopRun, Transient) as exc:
            store.event('paused',str(exc))
        except Blocked as exc:
            store.event('blocked',str(exc)); code = 3
        except KeyboardInterrupt:
            store.event('paused','Interrupted. Run the identical command to resume.')
        except (OSError,MemoryError) as exc:
            print(f'Local storage/memory error: {exc}. Resolve it and run again.',file=sys.stderr); code = 2
        finally:
            store.recover()
            report = store.report()
            summary(report)
            print('Archive:',store.root)
        if code:
            return code
        if report['queue_exhausted'] and report['issues']:
            return 2
        return 0
    finally:
        store.close()
        if acquired:
            lock.release()


if __name__ == '__main__':
    sys.exit(main())
