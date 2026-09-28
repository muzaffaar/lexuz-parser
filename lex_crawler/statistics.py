"""Parser for lex.uz's own statistics page (https://lex.uz/uz/statistic).

The page states how many documents the database holds, by document kind, form, language and legal status. Each
*language variant* is counted as a separate document, exactly like one lex.uz document id. The parser checks the
page's own arithmetic, so a layout change or a mis-read row fails loudly instead of storing wrong numbers:

  * per kind: the forms add up to the kind's "JAMI" (total) row;
  * per total row: languages add up to the total, and statuses add up to the total;
  * the kinds add up to the grand total row and to the sentence "Bugun bazada N ta hujjat".
"""
import re

from bs4 import BeautifulSoup

from .parsing import LayoutError

STATISTICS_URL = 'https://lex.uz/uz/statistic'
COLUMNS = ('total', 'ru', 'uz_cyrl', 'uz_latn', 'en', 'in_force', 'new_edition', 'lost_force')
_HEADER_SENTENCE = re.compile(r"Bugun\s+bazada\s+([\d\s ]+?)\s*ta\s+hujjat", re.I)


def _number(text):
    text = text.replace(' ', ' ').strip()
    if text in ('', '---', '-', '—'):
        return 0
    if not re.fullmatch(r'\d[\d\s]*', text):
        raise LayoutError(f'Statistics table cell is not a number: {text!r}')
    return int(text.replace(' ', ''))


def _grid(table):
    """Table as a list of equal-width rows with row/column spans expanded."""
    grid, pending = [], {}
    for tr in table.select('tr'):
        row, col = [], 0
        cells = iter(tr.find_all(['td', 'th'], recursive=False))
        while True:
            while pending.get(col, [None, 0])[1] > 0:
                row.append(pending[col][0]); pending[col][1] -= 1; col += 1
            cell = next(cells, None)
            if cell is None:
                break
            text = ' '.join(cell.get_text(' ', strip=True).split())
            rowspan = int(cell.get('rowspan', 1) or 1); colspan = int(cell.get('colspan', 1) or 1)
            for _ in range(colspan):
                row.append(text)
                if rowspan > 1:
                    pending[col] = [text, rowspan - 1]
                col += 1
        while pending.get(col, [None, 0])[1] > 0:
            row.append(pending[col][0]); pending[col][1] -= 1; col += 1
        grid.append(row)
    return grid


def parse_statistics(raw):
    soup = BeautifulSoup(raw, 'lxml')
    table = soup.select_one('table')
    if table is None:
        raise LayoutError('Statistics page has no table; response saved for review')
    grid = _grid(table)
    if not grid or any(len(r) != 11 for r in grid):
        raise LayoutError('Statistics table is not the expected 11 columns wide')

    rows, grand = [], None
    for cells in grid[1:]:  # row 0 is the header
        _, category, form, *values = cells
        numbers = dict(zip(COLUMNS, (_number(v) for v in values)))
        if not category and form == 'JAMI':
            grand = numbers  # the unlabeled final row: all kinds together
            continue
        rows.append({'category': category, 'form': form, 'is_total': form == 'JAMI', **numbers})
    if grand is None or not rows:
        raise LayoutError('Statistics table has no grand-total row')

    problems = []
    sentence = _HEADER_SENTENCE.search(soup.get_text(' ', strip=True))
    header_total = int(re.sub(r'\D', '', sentence.group(1))) if sentence else None
    if header_total is None:
        problems.append('page sentence "Bugun bazada N ta hujjat" not found')
    elif header_total != grand['total']:
        problems.append(f'header says {header_total} documents but the grand-total row says {grand["total"]}')

    categories = list(dict.fromkeys(r['category'] for r in rows))
    kind_totals = {}
    for category in categories:
        parts = [r for r in rows if r['category'] == category]
        total_rows = [r for r in parts if r['is_total']]
        if len(total_rows) != 1:
            problems.append(f'{category!r}: expected one JAMI row, found {len(total_rows)}')
            continue
        total = total_rows[0]
        kind_totals[category] = total['total']
        for column in COLUMNS:
            if sum(r[column] for r in parts if not r['is_total']) != total[column]:
                problems.append(f'{category!r}: forms do not add up to JAMI for {column}')
        if sum(total[c] for c in ('ru', 'uz_cyrl', 'uz_latn', 'en')) != total['total']:
            problems.append(f'{category!r}: language columns do not add up to the total')
        if sum(total[c] for c in ('in_force', 'new_edition', 'lost_force')) != total['total']:
            problems.append(f'{category!r}: status columns do not add up to the total')
    if sum(kind_totals.values()) != grand['total']:
        problems.append(f'kinds add up to {sum(kind_totals.values())}, grand total row says {grand["total"]}')

    return {
        'url': STATISTICS_URL,
        'total_documents': grand['total'],
        'header_total': header_total,
        'grand_total': grand,
        'categories': [{'category': c, 'total': kind_totals.get(c)} for c in categories],
        'rows': rows,
        'consistent': not problems,
        'problems': problems,
    }
