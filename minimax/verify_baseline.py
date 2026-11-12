"""Run the pre-MiniMax checks against real seekdb and PowerContext."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
RUN = ROOT / 'run'
CORPUS = 'orders-' + uuid4().hex[:12]


def call(command, *args):
    result = subprocess.check_output([sys.executable, 'sdd.py', '--corpus', CORPUS, *args, command],
                                     cwd=ROOT, text=True, encoding='utf-8')
    return json.loads(result)


def main():
    RUN.mkdir(exist_ok=True)
    imported = call('ingest')
    empty = call('context')
    assert empty['powercontext']['checkpoint'] is None
    sources = ['sample/specs/orders.md', 'sample/orders.py', 'sample/test_orders.py']
    assert [row['source'] for row in empty['seekdb']['materials']] == sources
    before = call('baseline')
    restored = call('context')
    assert restored['powercontext']['checkpoint']['state'] == before['powercontext_write']['state']
    assert restored['powercontext']['status'] == 'ready'
    assert before['regression']['exit_code'] == 0 and before['feature']['exit_code'] == 1

    revision = RUN / ('revision-' + CORPUS)
    shutil.copytree(ROOT / 'sample', revision, ignore=shutil.ignore_patterns('__pycache__'))
    spec = revision / 'specs' / 'orders.md'
    spec.write_text(spec.read_text(encoding='utf-8').replace('1–9 ASCII decimal digits', '1–6 ASCII decimal digits'), encoding='utf-8')
    original_hash = restored['seekdb']['materials'][0]['revision']
    try:
        call('ingest', '--source', str(revision))
        updated = call('context')
        assert updated['seekdb']['materials'][0]['revision'] != original_hash
        assert '1–6 ASCII decimal digits' in updated['seekdb']['materials'][0]['body']
    finally:
        call('ingest')
        assert call('context')['seekdb']['materials'][0]['revision'] == original_hash
    records = {'ingest': imported, 'empty_context': empty, 'baseline': before, 'restarted_context': restored,
               'updated_spec_context': updated}
    for name, result in records.items():
        (RUN / (name + '.json')).write_text(json.dumps({'corpus': CORPUS, 'result': result}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (RUN / 'corpus.txt').write_text(CORPUS, encoding='utf-8')
    print('PASS: real seekdb sources, baseline failure and regression, restarted PowerContext checkpoint, updated specification and restoration')
    print(CORPUS)


if __name__ == '__main__':
    main()
