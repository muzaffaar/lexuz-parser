"""Extract embedded PDF text locally; never remove encryption or invent OCR text."""
import argparse
from pathlib import Path
from pypdf import PdfReader
from .storage import atomic_write, write_json


def extract(path, max_pages=2000):
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        return {'status':'encrypted','pages':[], 'note':'Source PDF preserved; password-protected text was not extracted.'}
    result = []; errors=[]
    total = len(reader.pages)
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            break
        try:
            value = page.extract_text() or ''
            result.append({'page':i+1,'text':value,'characters':len(value.strip())})
        except Exception as exc:
            errors.append({'page':i+1,'error':str(exc)})
            result.append({'page':i+1,'text':'','characters':0})
    low_text=[x['page'] for x in result if x['characters'] < 40]
    status='text_extracted'
    if errors:status='partial_errors'
    elif total>max_pages:status='page_limit'
    elif low_text:status='review_low_text_pages'
    return {'status':status,'total_pages':total,'pages_processed':len(result),'pages':result,
            'low_text_pages':low_text,'errors':errors,
            'note':'Low text can indicate a scan, a blank page, or an extraction limitation. Check the source before choosing OCR.'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('pdf',type=Path);parser.add_argument('--max-pages',type=int,default=2000)
    args=parser.parse_args()
    try:
        result=extract(args.pdf,args.max_pages)
    except Exception as exc:
        result={'status':'extraction_failed','error':str(exc),'pages':[]}
    write_json(args.pdf.with_name('text.json'),result)
    atomic_write(args.pdf.with_name('text.txt'),'\n\n'.join(x['text'] for x in result.get('pages',[])).encode('utf-8'))

if __name__=='__main__':main()
