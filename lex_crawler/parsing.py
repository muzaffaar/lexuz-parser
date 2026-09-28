"""Pure parsers. Never evaluate downloaded JavaScript."""
import calendar
import re
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup

HOME = 'https://lex.uz/'
CATEGORIES = ('nat', 'loc', 'int', 'tech', 'court', 'oliy')
DOC_PATH = re.compile(r'^/(?:uz/|ru/|en/)?(docs|pdfs|pdffile|doc-passport)/(-?\d+)/?$')
POSTBACK = re.compile(r"__doPostBack\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)")
PATH_LITERAL = re.compile(r"['\"](/(?:(?:uz|ru|en)/)?(?:docs|pdfs|pdffile|doc-passport|actinfo)/[^'\"\s<>]+(?: [^'\"<>]*)?)['\"]")

class LayoutError(ValueError):
    pass


def soup_of(raw):
    return BeautifulSoup(raw, 'lxml')


def text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def safe_url(value, base=HOME):
    u = urlsplit(urljoin(base, value.strip()))
    if u.scheme not in ('https', 'http') or u.hostname not in ('lex.uz', 'www.lex.uz') or u.username or u.password or u.port not in (None, 80, 443):
        return None
    path = re.sub(r'^/(?:uz|ru|en)/(?=(?:docs|pdfs|pdffile|doc-passport|actinfo|search)/)', '/', u.path)
    return urlunsplit(('https', 'lex.uz', path or '/', urlencode(sorted(parse_qsl(u.query, keep_blank_values=True))), ''))


def document_url(value, base=HOME):
    u = safe_url(value, base)
    if not u:
        return None
    parts = urlsplit(u); match = DOC_PATH.fullmatch(parts.path)
    if not match or match[1] != 'docs':
        return None
    q = {k.lower(): v for k, v in parse_qsl(parts.query)}
    if any(k in q for k in ('type', 'action', 'ondate2', 'otherlang')):
        return None
    edition = q.get('ondate')
    if edition:
        # The site emits both dd.mm.yyyy and dd.mm.yyyy 00 for the same day.
        edition = re.sub(r'\s+00(?::00(?::00)?)?$', '', edition.strip())
    return HOME.rstrip('/') + '/docs/' + match[2] + ('?' + urlencode({'ONDATE': edition}) if edition else '')


def identity(value):
    m = DOC_PATH.fullmatch(urlsplit(value).path)
    return m[2] if m else None


def with_query(url, **updates):
    p = urlsplit(url); q = dict(parse_qsl(p.query))
    for k, v in updates.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = str(v)
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(sorted(q.items())), ''))


def extract_links(soup, base):
    out = []; seen = set()
    def add(value, label='', kind='href'):
        full = urljoin(base, value)
        if urlsplit(full).scheme not in ('http', 'https'):
            return
        key = (full, kind)
        if key not in seen:
            seen.add(key); out.append({'url': full, 'text': label, 'kind': kind})
    for n in soup.select('a[href]'):
        add(n['href'], text(n))
    for n in soup.select('[onclick]'):
        for value in PATH_LITERAL.findall(n['onclick']):
            add(value, text(n), 'onclick')
    for n in soup.select('script:not([src])'):
        for value in PATH_LITERAL.findall(n.get_text()):
            add(value, '', 'script')
    return out


def homepage(raw, url=HOME):
    s = soup_of(raw)
    source = '\n'.join(n.get_text() for n in s.select('script:not([src])'))
    links = extract_links(s, url)
    paths = {urlsplit(safe_url(x['url']) or '').path for x in links}
    for match in re.findall(r"['\"](/(?:(?:uz|ru|en)/)?search/[a-z]+)['\"]", source):
        paths.add(urlsplit(safe_url(match)).path)
    categories = [x for x in CATEGORIES if '/search/' + x in paths]
    languages = sorted({o.get('value') for n in s.select('select[id^="lang_"]')
                        for o in n.select('option[value]') if o['value'] in ('1', '2', '3', '4')})
    if not categories or not languages:
        raise LayoutError('Homepage category/language controls changed; no global search scope was inferred')
    seeds = [HOME.rstrip('/') + '/search/' + cat + '?lang=' + lang
             for cat in categories for lang in sorted(languages, key=lambda x: (x != '3', x))]
    # Include broader all-document search when its route appears in the homepage code.
    if '/search/all' in paths:
        seeds.extend(HOME.rstrip('/') + '/search/all?lang=' + lang for lang in languages)
    return {'categories': categories, 'languages': languages, 'seeds': seeds, 'links': links}


def form_fields(form):
    result = []
    for n in form.select('input[name],select[name],textarea[name]'):
        if n.has_attr('disabled'):
            continue
        typ = n.get('type', '').lower()
        if typ in ('submit', 'button', 'reset', 'file', 'image'):
            continue
        if typ in ('checkbox', 'radio') and not n.has_attr('checked'):
            continue
        if n.name == 'select':
            chosen = n.select('option[selected]') or n.select('option')[:1]
            result.extend((n['name'], x.get('value', text(x))) for x in chosen)
        else:
            result.append((n['name'], n.get('value', '') if n.name == 'input' else n.get_text()))
    return result


def listing(raw, url):
    s = soup_of(raw)
    total_node = s.select_one('.refind__result-export__title')
    m = re.search(r'[\d][\d\s\u00a0,]*', text(total_node))
    total = int(re.sub(r'\D', '', m[0])) if m else None
    result_links = []
    for a in s.select('a.lx_link[href]'):
        u = safe_url(a['href'], url)
        if u and DOC_PATH.fullmatch(urlsplit(u).path):
            result_links.append({'url': u, 'text': text(a), 'kind': 'href'})
    ids = sorted({identity(x['url']) for x in result_links
                  if DOC_PATH.fullmatch(urlsplit(x['url']).path)[1] in ('docs', 'pdfs', 'pdffile')})
    form = s.select_one('form#Form1')
    if not ids and total != 0:
        raise LayoutError('No result IDs and no explicit zero total: refusing to treat this page as empty')
    if form is None and total != 0:
        raise LayoutError('Search form#Form1 is missing')
    nxt = s.select_one('.item_pgn4 a[href]')
    if nxt is None:
        nxt = s.select_one('a[id$="_LinkButton1"][href]')
    next_request = None
    if nxt is not None:
        match = POSTBACK.search(nxt['href'])
        if not match:
            raise LayoutError('Unrecognized next-page action')
        fields = [(k, v) for k, v in form_fields(form) if k not in ('__EVENTTARGET', '__EVENTARGUMENT')]
        fields.extend([('__EVENTTARGET', match[1]), ('__EVENTARGUMENT', match[2])])
        next_request = {'url': safe_url(form.get('action', ''), url), 'data': fields}
        if not next_request['url']:
            raise LayoutError('Search form action left the allowed origin')
    years = [int(o['value']) for o in s.select('#f_by_year option[value]') if re.fullmatch(r'[12]\d{3}', o['value'])]
    months = [int(o['value']) for o in s.select('#f_by_month option[value]') if o['value'].isdigit() and 1 <= int(o['value']) <= 12]
    return {'ids': ids, 'links': result_links, 'total': total, 'next': next_request,
            'years': years, 'months': months}


def partitions(url, page):
    """Use observed year/month facets; bisect a known date interval if necessary."""
    q = dict(parse_qsl(urlsplit(url).query))
    if 'from' in q and 'to' in q:
        def dt(x):
            d, m, y = map(int, x.split('.')); return date(y, m, d)
        lo, hi = dt(q['from']), dt(q['to'])
    elif 'fyear' not in q and len(page['years']) > 1:
        return [with_query(url, fyear=y) for y in sorted(page['years'], reverse=True)]
    elif 'fyear' in q and 'fmonth' not in q and len(page['months']) > 1:
        return [with_query(url, fmonth=m) for m in sorted(page['months'], reverse=True)]
    elif 'fyear' in q:
        y = int(q['fyear']); month = int(q.get('fmonth', 1))
        lo = date(y, month, 1)
        hi = date(y, month, calendar.monthrange(y, month)[1]) if 'fmonth' in q else date(y, 12, 31)
    else:
        return []
    if lo >= hi:
        return []
    mid = lo + (hi - lo) // 2
    return [with_query(url, **{'from': a.strftime('%d.%m.%Y'), 'to': b.strftime('%d.%m.%Y')})
            for a, b in ((lo, mid), (mid + timedelta(days=1), hi))]


def tables(root):
    result = []
    for t in root.select('table'):
        rows = []
        for tr in t.select('tr'):
            if tr.find_parent('table') is not t:
                continue
            rows.append([{'text': text(c), 'rowspan': c.get('rowspan', '1'), 'colspan': c.get('colspan', '1'),
                          'header': c.name == 'th'} for c in tr.find_all(['th', 'td'], recursive=False)])
        result.append({'rows': rows, 'html': str(t)})
    return result


def document(raw, url):
    s = soup_of(raw); original = s.select_one('#divCont')
    links = extract_links(s, url)
    if original is None:
        pdfs = sorted({safe_url(x['url'],url) for x in links if re.search(r'/pdffile/-?\d+',x['url']) and safe_url(x['url'],url)})
        if s.select_one('#pdfBody') is not None and pdfs:
            return {'url':url,'document_id':identity(url),'title':text(s.title),
                    'representation':'pdf_only','primary_pdf_urls':pdfs,'blocks':[],
                    'content_block_count':0,'tables':[],'links':links,'assets':[],
                    'content_html':str(s.select_one('#pdfBody')),'text':'','annotations':[]}
        raise LayoutError('Document #divCont missing and no recognized primary PDF; response saved for review')
    root = soup_of(str(original)).select_one('#divCont')
    for n in root.select('.lx_elem2,script,style'):
        n.decompose()
    blocks = []
    for n in root.find_all(recursive=False):
        numeric = n if re.fullmatch(r'-?\d+', n.get('id', '')) else n.find(id=re.compile(r'^-?\d+$'))
        blocks.append({'order': len(blocks), 'id': numeric.get('id') if numeric else n.get('id'),
                       'classes': n.get('class', []), 'is_content': 'lx_elem' in n.get('class', []),
                       'text': text(n), 'html': str(n), 'links': extract_links(n, url)})
    if not blocks or not any(x['text'] for x in blocks):
        raise LayoutError('Document container is empty')
    content_ids = [x['id'] for x in blocks if x['is_content'] and x['id']]
    if len(content_ids) != len(set(content_ids)):
        raise LayoutError('Duplicate content IDs: possibly a comparison or changed layout')
    assets = set()
    for n in root.select('img[src],source[src],audio[src],video[src]'):
        u = safe_url(n['src'], url)
        if u:
            assets.add(u)
    for a in root.select('a[href]'):
        if re.search(r'\.(?:pdf|docx?|xlsx?|rtf|odt|zip|png|jpe?g|svg)(?:\?|$)', a['href'], re.I):
            u = safe_url(a['href'], url)
            if u:
                assets.add(u)
    return {'url': url, 'document_id': identity(url), 'title': text(s.title), 'representation':'html',
            'blocks': blocks, 'content_block_count': sum(x['is_content'] for x in blocks),
            'tables': tables(root), 'links': links, 'assets': sorted(assets),
            'content_html': str(root), 'text': '\n\n'.join(x['text'] for x in blocks if x['is_content'] and x['text']),
            'annotations': [x for x in blocks if not x['is_content']]}


def supplemental(raw, url):
    s = soup_of(raw)
    for n in s.select('script,style'):
        n.decompose()
    return {'url': url, 'title': text(s.title), 'text': text(s.body or s),
            'tables': tables(s), 'links': extract_links(s, url)}
