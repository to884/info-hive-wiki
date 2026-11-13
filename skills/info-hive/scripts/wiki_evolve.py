#!/usr/bin/env python3
"""経験を記録し、証拠にもとづくスキルの改善を検証する。

標準ライブラリのみ。候補はコピーの中で実行し、インストール済みのスキルが変わるのは
`apply` のときだけである。`rollback` は、採用した実験の元のファイルを復元し、
同時に行われた編集があれば拒否する。ローカルのランナーは信頼された実行ファイルであり、サンドボックスではない。

例:
  python wiki_evolve.py --wiki ~/info-hive/wiki capture --raw ~/info-hive/raw --record experience.json
  python wiki_evolve.py --wiki ~/info-hive/wiki propose --id query-v1 --skill ./my-skill \
    --target references/query.md --candidate /tmp/query.md \
    --evidence concepts/stale-decisions.md --reason '置き換わった決定を見分ける'
  python wiki_evolve.py --wiki ~/info-hive/wiki evaluate query-v1 --suite suite.json \
    --runner runner.json --model model-version --tools tool-versions
  python wiki_evolve.py --wiki ~/info-hive/wiki apply query-v1
  python wiki_evolve.py --wiki ~/info-hive/wiki rollback query-v1
  python wiki_evolve.py --wiki ~/info-hive/wiki history
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import localize_argparse, normalize_quote  # noqa: E402
import wiki_initialized  # noqa: E402
import wiki_workspace  # noqa: E402

IGNORED = {'.git', '__pycache__', '.wiki-cache', '.evolution', 'node_modules', '.wiki-evolve-lock'}
# wiki を使わない操作。ほかの操作はどれも、振り分けの前に wiki の判定を通る。
WIKI_FREE_ACTIONS = frozenset({'verify-bundle'})


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def encode(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def slug(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', value),
            'ID に使えるのは英小文字、数字、ハイフンだけで、長さは 1～64 文字である')
    return value


def relative(value):
    path = Path(value)
    require(value and not path.is_absolute() and '..' not in path.parts
            and not any(p.startswith('.') for p in path.parts), '安全でない相対パスである（絶対パス、..、ドットで始まる名前は使えない）')
    return path


def inside(root, value):
    path = root / relative(value)
    require(path.resolve().is_relative_to(root.resolve()), 'パスがルートの外を指している')
    require(not any((root / Path(*path.relative_to(root).parts[:i])).is_symlink()
                    for i in range(1, len(path.relative_to(root).parts) + 1)),
            'シンボリックリンクを含むパスは扱えない')
    return path


def atomic(path, data, mode=None):
    fd, name = tempfile.mkstemp(prefix='.evolve-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def tree(root):
    require(root.is_dir() and not root.is_symlink(), f'通常のディレクトリではない: {root}')
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        for name in dirs + sorted(files):
            path = Path(directory) / name
            require(not path.is_symlink(), f'シンボリックリンクは使えない: {path}')
        for name in sorted(files):
            path = Path(directory) / name
            require(path.is_file(), f'通常のファイルではない: {path}')
            result[path.relative_to(root).as_posix()] = {
                'sha256': digest(path.read_bytes()), 'mode': path.stat().st_mode & 0o777,
            }
    return result


def copy_tree(source, dest):
    files = tree(source)
    dest.mkdir(parents=True, exist_ok=True)
    for name in files:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    return files


@contextmanager
def locked(wiki):
    # SCHEMA.md のないディレクトリに .evolution/ を作らない（ADR-0019）。NotInitialized は ValueError である。
    wiki_initialized.require(wiki)
    state = wiki / '.evolution'
    require(not state.is_symlink(), '.evolution のディレクトリをシンボリックリンクにはできない')
    state.mkdir(exist_ok=True)
    lock = state / 'lock'
    # ponytail: wiki 全体で1つのロックを使う。同時実行が問題になるなら、実験ごとのロックにする。
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'評価ループが実行中である。そのプロセスが終了しているなら、確かめてから {lock} を削除する')
    try:
        yield state
    finally:
        lock.rmdir()


def event(exp, kind, payload):
    record = {'event': kind, 'at': datetime.now(timezone.utc).isoformat(), **payload}
    path = exp / f'{kind}-{uuid.uuid4().hex}.json'
    with path.open('x', encoding='utf-8') as f:
        f.write(encode(record))
    return record


def manifest(exp):
    value = read_json(exp / 'proposal.json')
    require(tree(exp / 'baseline') == value['baseline'], 'ベースラインのスナップショットが変わった')
    require(tree(exp / 'candidate') == value['candidate'], '候補のスナップショットが変わった')
    return value


def capture(args):
    record = read_json(args.record)
    slug(record.get('id'))
    for key in ('task', 'verification', 'model', 'tools', 'scope'):
        require(isinstance(record.get(key), str) and record[key].strip(), f'{key} がない')
    require(record.get('outcome') in ('success', 'failure'), 'outcome は success か failure にする')
    require(isinstance(record.get('actions'), list) and record['actions']
            and all(isinstance(x, str) and x.strip() for x in record['actions']), 'actions がない')
    for key in ('hypothesis', 'counterexamples'):
        require(isinstance(record.get(key, ''), str), f'{key} は文字列にする')
    raw = args.raw.resolve()
    # raw のディレクトリは作らない。初期化（init_wiki.py）が作る。
    require(raw.is_dir(), f'raw のディレクトリがない: {raw}。--raw が正しいかを確かめる。'
                          '初期化した wiki の raw をなくしたときは、/wiki:upgrade で作り直せる')
    folder = raw / 'experiences'
    require(not folder.is_symlink(), 'experiences のディレクトリをシンボリックリンクにはできない')
    folder.mkdir(exist_ok=True)
    path = folder / (record['id'] + '.json')
    body = encode(record)
    if path.exists():
        require(not path.is_symlink() and path.read_text(encoding='utf-8') == body, 'この ID の経験はすでに記録されていて、変更できない')
    else:
        with path.open('x', encoding='utf-8') as f:
            f.write(body)
    return {'raw': str(path), 'sha256': digest(body.encode()), 'next': 'Ingest as a source; consolidate supported patterns'}


def propose(args, state):
    exp = state / slug(args.id)
    require(not exp.exists(), 'この実験の ID はすでにある。新しい ID を使う')
    source = args.skill.resolve()
    directory = args.candidate.is_dir()
    require(directory or args.target, 'ファイル単位の提案には --target が必要である')
    require(not args.skill.is_symlink(), 'スキルをシンボリックリンクにはできない')
    require(source.is_dir() or directory, '新しいスキルには候補のディレクトリが必要である')
    require(not source.is_relative_to(state.resolve()), 'インストール先を実験の記録の中にはできない')
    if source.exists():
        require((source / 'SKILL.md').is_file() or not tree(source), 'スキルのディレクトリには SKILL.md が必要である（空のディレクトリならよい）')
    if directory:
        require((args.candidate / 'SKILL.md').is_file(), '候補に SKILL.md がない')
    else:
        target = inside(source, args.target)
        require(target.is_file(), '--target には、このスキルにある既存のファイルを指定する')
        after = args.candidate.read_text(encoding='utf-8')
        require(after.strip(), '候補が空である')
    require(args.reason.strip(), '提案の理由（--reason）が空である')
    evidence = {}
    for name in args.evidence:
        path = inside(args.wiki, name)
        require(path.suffix == '.md' and path.is_file(), '根拠には、wiki にある既存の Markdown のページを指定する')
        evidence[name] = path.read_text(encoding='utf-8')
    require(evidence, '根拠のページが少なくとも 1 つ必要である')
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=state) as tmp:
        staging = Path(tmp) / 'experiment'
        (staging / 'baseline').mkdir(parents=True)
        baseline = copy_tree(source, staging / 'baseline') if source.exists() else {}
        copy_tree(args.candidate if directory else source, staging / 'candidate')
        if not directory:
            (staging / 'candidate' / args.target).write_text(after, encoding='utf-8')
        require(tree(staging / 'candidate') != baseline, '候補がベースラインと同じである')
        value = {'id': args.id, 'skill': str(source), 'target': args.target if not directory else None, 'new_skill': not source.exists(),
                 'reason': args.reason, 'evidence': evidence, 'baseline': baseline,
                 'candidate': tree(staging / 'candidate')}
        (staging / 'proposal.json').write_text(encode(value), encoding='utf-8')
        diff = ''
        for name in sorted(set(baseline) | set(value['candidate'])):
            old, new = staging / 'baseline' / name, staging / 'candidate' / name
            before = old.read_text(encoding='utf-8') if old.exists() else ''
            after = new.read_text(encoding='utf-8') if new.exists() else ''
            diff += ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile='baseline/' + name, tofile='candidate/' + name))
        (staging / 'change.diff').write_text(diff, encoding='utf-8')
        staging.rename(exp)
    return {'id': args.id, 'status': 'proposed', 'diff': str(exp / 'change.diff')}


def finite(value, label):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            f'{label} は有限の 0 以上の数でなければならない')
    return value


def suite_contract(suite, corpus):
    require(suite.get('version') == 1, 'スイートの version は 1 でなければならない')
    tasks = suite.get('tasks')
    require(isinstance(tasks, list) and tasks, 'スイートに課題がない')
    seen, splits = set(), set()
    for task in tasks:
        slug(task.get('id'))
        require(task['id'] not in seen, '課題の ID が重複している')
        seen.add(task['id'])
        require(task.get('split') in ('validation', 'holdout'), 'split には validation か holdout を使う')
        splits.add(task['split'])
        require(isinstance(task.get('question'), str) and task['question'].strip(), '課題の質問がない')
        require(type(task.get('abstain')) is bool, 'abstain の期待値は真偽値にする')
        for key in ('answer_all', 'answer_none', 'citations'):
            require(isinstance(task.get(key), list) and all(isinstance(s, str) and s for s in task[key]),
                    f'{key} は空でない文字列のリストにする')
        require(task['abstain'] or (task['answer_all'] and task['citations']),
                '答えのある課題には、答えの照合（answer_all）と裏づけの引用（citations）が必要である')
        for name in task['citations']:
            require(inside(corpus, name).is_file(), f'期待する引用先のファイルがない: {name}')
    require(splits == {'validation', 'holdout'}, '検証用と最終テスト用の課題の両方が必要である')
    for key in ('max_cost_ratio', 'max_tool_ratio'):
        finite(suite.get(key, 1.25), key)
    return tasks


def normalize(text):
    """答えの照合に使う形。D2 と同じ規則（wiki_text.normalize_quote）にそろえる。

    日本語の文字どうしの間の改行と空白は取り除き、英数字の間の空白は残す。
    """
    return normalize_quote(text.casefold())


def score(task, output, corpus):
    require(isinstance(output, dict) and isinstance(output.get('answer'), str), 'ランナーが answer を返さなかった')
    require(type(output.get('abstain')) is bool, 'ランナーの abstain は真偽値でなければならない')
    calls = output.get('tool_calls')
    require(type(calls) is int and calls >= 0, 'ランナーの tool_calls は 0 以上の整数でなければならない')
    finite(output.get('cost'), 'ランナーの cost')
    citations = output.get('citations')
    require(isinstance(citations, list) and all(isinstance(c, str) for c in citations), 'ランナーの citations は文字列のリストでなければならない')
    valid = True
    for name in citations:
        try:
            valid = valid and inside(corpus, name).is_file()
        except ValueError:
            valid = False
    answer = normalize(output['answer'])
    checks = {
        'answer': all(normalize(s) in answer for s in task['answer_all']),
        'forbidden': not any(normalize(s) in answer for s in task['answer_none']),
        'citations': valid and set(task['citations']).issubset(citations),
        'abstain': output['abstain'] == task['abstain'],
    }
    return {'passed': all(checks.values()), 'checks': checks,
            'tool_calls': calls, 'cost': output['cost']}


def run_runner(argv, request, cwd, timeout):
    # 子の Python（アダプター）も UTF-8 モードで動かす。アダプターが起動するエージェントにも引き継がれる。
    env = {**os.environ, "PYTHONUTF8": "1"}
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=cwd, env=env,
                               start_new_session=os.name == 'posix')
    try:
        stdout, stderr = process.communicate(encode(request), timeout=timeout)
        if process.returncode:
            raise ValueError(f'ランナーが終了コード {process.returncode} で終わった。スキルは適用していない。{stderr[-4000:]}')
        return json.loads(stdout)
    except BaseException:
        if os.name == 'posix':
            # ネイティブな CLI は、シェルのツールを別のプロセスグループに切り離すことがある。
            # 親を kill する前に子孫プロセスのスナップショットを取る。そうしないと、
            # 特定できない孤児プロセスになる。ホストはプロセスの調査を許可している必要がある。
            try:
                rows = subprocess.check_output(['ps', '-eo', 'pid=,ppid='], text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL)
                parents = {int(pid): int(ppid) for pid, ppid in (line.split() for line in rows.splitlines())}
                descendants, frontier = [], [process.pid]
                while frontier:
                    frontier = [pid for pid, parent in parents.items() if parent in frontier and pid not in descendants]
                    descendants.extend(frontier)
                for pid in reversed(descendants):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            process.kill()
        process.communicate()
        raise


def evaluate(args, exp):
    value = manifest(exp)
    require(not list(exp.glob('evaluation-*.json')) and not list(exp.glob('inputs-*.json')),
            '評価はすでに記録されたか、始まっている。新しく試すときは新しい提案を作る')
    require(not (exp / 'transition.json').exists(), '適用した実験は評価できない')
    suite_path = args.suite.resolve()
    suite = read_json(suite_path)
    corpus = (suite_path.parent / relative(suite['corpus'])).resolve()
    runner = read_json(args.runner)
    require(isinstance(runner, list) and runner and all(isinstance(x, str) and x for x in runner),
            'ランナーのファイルには、空でない argv の JSON 配列を書く（シェルは使わない）')
    finite(args.timeout, 'timeout')
    require(args.repeats >= 1 and args.timeout > 0, '--repeats と --timeout には正の値を指定する')
    require(args.model.strip() and args.tools.strip(), 'モデルとツールのバージョン（--model、--tools）が空である')
    suite_contract(suite, corpus)
    # ランナーを呼び出す前に入力を保存する。失敗した試行や中断した試行も対象にする。
    event(exp, 'inputs', {'suite': suite, 'model': args.model, 'tools': args.tools,
                          'runner': runner, 'repeats': args.repeats, 'timeout': args.timeout,
                          'proposal_sha256': digest((exp / 'proposal.json').read_bytes())})
    corpus_hash = copy_tree(corpus, exp / 'corpus')
    # 実行のたびに、アーカイブした入力の新しいコピーを渡す。
    with tempfile.TemporaryDirectory(prefix='wiki-evaluation-') as tmp:
        workspace = Path(tmp)
        require(copy_tree(exp / 'corpus', workspace / 'corpus') == corpus_hash,
                '準備中に、記録したコーパスが変わった')
        tasks = suite_contract(suite, workspace / 'corpus')
        rows = []
        try:
            for repeat in range(args.repeats):
                for task in tasks:
                    # ウォームアップや実行順による系統的な影響を減らすため、順序を交互にする。
                    variants = ('baseline', 'candidate') if repeat % 2 == 0 else ('candidate', 'baseline')
                    for variant in variants:
                        with tempfile.TemporaryDirectory(dir=workspace) as run_dir:
                            run = Path(run_dir)
                            copy_tree(exp / variant, run / 'skill')
                            copy_tree(workspace / 'corpus', run / 'wiki')
                            request = {'version': 1, 'question': task['question'],
                                       'skill_root': str(run / 'skill'), 'wiki_root': str(run / 'wiki'),
                                       'model': args.model, 'tools': args.tools}
                            output = run_runner(runner, request, run, args.timeout)
                            result = score(task, output, run / 'wiki')
                            # クエリのランナーは、証拠や手順を変更してはならない。
                            require(tree(run / 'wiki') == corpus_hash, 'ランナーが評価用のコーパスを変更した')
                            require(tree(run / 'skill') == value[variant], 'ランナーがスキルを変更した')
                            rows.append({'task': task['id'], 'split': task['split'], 'repeat': repeat,
                                         'variant': variant, **result, 'output': output})
            manifest(exp)
            require(tree(exp / 'corpus') == corpus_hash, '評価中に、記録したコーパスが変わった')
            totals = {}
            for split in ('validation', 'holdout'):
                totals[split] = {variant: sum(r['passed'] for r in rows if r['split'] == split and r['variant'] == variant)
                                 for variant in ('baseline', 'candidate')}
            regressions = []
            paired = {(r['task'], r['repeat'], r['variant']): r for r in rows}
            for row in rows:
                if row['variant'] == 'baseline' and row['passed'] and not paired[(row['task'], row['repeat'], 'candidate')]['passed']:
                    regressions.append({'task': row['task'], 'repeat': row['repeat']})
            budgets = {}
            for metric, config in (('cost', 'max_cost_ratio'), ('tool_calls', 'max_tool_ratio')):
                amounts = {v: sum(r[metric] for r in rows if r['variant'] == v) for v in ('baseline', 'candidate')}
                budgets[metric] = {**amounts, 'ratio_limit': suite.get(config, 1.25),
                                   'passed': amounts['candidate'] <= amounts['baseline'] * suite.get(config, 1.25)}
            accepted = (totals['validation']['candidate'] > totals['validation']['baseline']
                        and totals['holdout']['candidate'] >= totals['holdout']['baseline']
                        and not regressions and all(b['passed'] for b in budgets.values()))
            report = {'status': 'passed' if accepted else 'rejected', 'totals': totals,
                      'regressions': regressions, 'budgets': budgets, 'rows': rows,
                      'model': args.model, 'tools': args.tools, 'runner': runner,
                      'suite': suite, 'corpus': corpus_hash, 'repeats': args.repeats,
                      'proposal_sha256': digest((exp / 'proposal.json').read_bytes())}
            event(exp, 'evaluation', report)
            return {k: v for k, v in report.items() if k not in ('rows', 'suite', 'corpus')}
        except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            event(exp, 'evaluation', {'status': 'error', 'error': type(exc).__name__,
                                     'rows': rows, 'model': args.model, 'tools': args.tools})
            raise


def transition(exp, rollback=False):
    value = manifest(exp)
    source = Path(value['skill'])
    source.mkdir(parents=True, exist_ok=True)
    lock = source / '.wiki-evolve-lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'スキルを別の処理が使用中である。そのプロセスが終了しているなら、確かめてから {lock} を削除する')
    try:
        return promote(exp, value, rollback)
    finally:
        lock.rmdir()


def promote(exp, value, rollback):
    source = Path(value['skill'])
    records = list(exp.glob('evaluation-*.json'))
    require(len(records) == 1, '完了した評価が必要である')
    report = read_json(records[0])
    require(report['status'] == 'passed', '評価に通らなかった。インストール済みのスキルは変えていない')
    require(tree(exp / 'corpus') == report['corpus'], '評価のあとで、記録したコーパスが変わった')
    require(report['proposal_sha256'] == digest((exp / 'proposal.json').read_bytes()), '評価のあとで提案が変わった')
    journal = exp / 'transition.json'
    current = read_json(journal) if journal.exists() else {'status': 'proposed'}
    if rollback:
        require(current['status'] in ('accepted', 'rolling_back', 'rolled_back'), 'ロールバックできるのは採用した変更だけである')
        before, after, pending, done = 'candidate', 'baseline', 'rolling_back', 'rolled_back'
    else:
        require(current['status'] in ('proposed', 'applying', 'accepted'), 'ロールバックした実験は再び適用できない')
        before, after, pending, done = 'baseline', 'candidate', 'applying', 'accepted'
    actual = tree(source)
    if current['status'] in (pending, done) and actual == value[after]:
        atomic(journal, encode({'status': done}).encode())
        return {'id': value['id'], 'status': done}
    if current['status'] == pending:
        # クラッシュすると、2つのスナップショットが混ざった状態が残ることがある。復旧できるのは、それらとまったく同じファイルの状態だけである。
        require(set(actual) <= set(value[before]) | set(value[after]), '復旧中に想定外のファイルが見つかった')
        for name in set(value[before]) | set(value[after]):
            require(actual.get(name) in (value[before].get(name), value[after].get(name)),
                    '復旧中に、インストール済みのスキルが変わった')
    else:
        require(actual == value[before], 'インストール済みのスキルが変わっている。同時に行われた編集を上書きしないよう中止した')
    require(current['status'] != done, 'インストール済みのスキルが、完了した切り替えの状態と一致しない')
    atomic(journal, encode({'status': pending}).encode())
    for name in sorted(set(value[before]) | set(value[after])):
        target = inside(source, name)
        if name in value[after]:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic(target, (exp / after / name).read_bytes(), value[after][name]['mode'])
        else:
            target.unlink(missing_ok=True)
    require(tree(source) == value[after], '切り替えたあとのスキルがスナップショットと一致しない')
    atomic(journal, encode({'status': done}).encode())
    event(exp, done, {'skill': str(source)})
    return {'id': value['id'], 'status': done}


def run_records(exp):
    return [read_json(p) for p in exp.glob('run-*.json') if p.name != 'run-inputs.json']


def history(state):
    rows = []
    for exp in sorted(state.iterdir()):
        if not exp.is_dir() or exp.is_symlink():
            continue
        if (exp / 'run-inputs.json').is_file():
            records = run_records(exp)
            rows.append({'id': exp.name, 'kind': 'run', 'status': records[-1]['status'] if records else 'incomplete',
                         'iterations': [read_json(p) for p in exp.glob('iteration-*.json')]})
            continue
        if not (exp / 'proposal.json').is_file():
            continue
        value = read_json(exp / 'proposal.json')
        evaluations = [read_json(p) for p in exp.glob('evaluation-*.json')]
        status = evaluations[0]['status'] if evaluations else ('incomplete' if list(exp.glob('inputs-*.json')) else 'proposed')
        if (exp / 'transition.json').exists():
            status = read_json(exp / 'transition.json')['status']
        rows.append({'id': value['id'], 'reason': value['reason'], 'target': value['target'],
                     'status': status, 'evidence': list(value['evidence'])})
    return rows


def export_bundle(exp, destination):
    """実行できるスキルと、持ち運べる監査用の証拠を、別々のディレクトリに置く。"""
    records = run_records(exp)
    require((exp / 'selection.json').is_file() and records and records[-1].get('status') == 'completed',
            '書き出すには、完了した評価ループの実行が必要である')
    require(tree(exp/'corpus') == records[-1]['corpus'], '記録したコーパスが変わった')
    require(tree(exp/'runtime') == read_json(exp/'runtime-hashes.json'), '記録したランタイムが変わった')
    destination = destination.absolute()
    require(not destination.exists() and not destination.is_symlink(), '書き出し先がすでにある。新しいパスを指定する')
    require(not destination.resolve().is_relative_to(exp.resolve()), '書き出し先を実験の記録の中にはできない')
    require(tree(exp/'best') == read_json(exp/'selection.json')['skill'], '選定のあとで、選んだスキルが変わった')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.export-', dir=destination.parent) as tmp:
        staging = Path(tmp)/'bundle'
        copy_tree(exp/'best', staging/'skill')
        copy_tree(exp, staging/'evidence')
        hashes = {name: value['sha256'] for name, value in tree(staging).items()}
        atomic(staging/'bundle.json', encode({'version':1,'skill':'skill','evidence':'evidence','sha256':hashes}).encode())
        staging.rename(destination)
    return {'bundle':str(destination),'files':len(hashes),'skill':str(destination/'skill')}


def verify_bundle(path):
    manifest = read_json(path/'bundle.json')
    require(manifest.get('version') == 1, '対応していないバンドルのバージョンである')
    hashes = {name:value['sha256'] for name,value in tree(path).items() if name != 'bundle.json'}
    require(hashes == manifest.get('sha256'), 'バンドルの証拠かスキルが変わった')
    return {'status':'verified','files':len(hashes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--wiki', type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    sub = parser.add_subparsers(dest='action', required=True)
    capture_parser = sub.add_parser('capture', help='確かめた経験を、変更できない raw の JSON として保存する')
    capture_parser.add_argument('--raw', type=Path, required=True)
    capture_parser.add_argument('--record', type=Path, required=True)
    p = sub.add_parser('propose', help='スキルを 1 つスナップショットし、根拠のページと結びつけた編集を用意する')
    p.add_argument('--target', help='変更する既存のファイル 1 つ。候補がディレクトリ全体なら省く')
    for flag in ('id', 'reason'):
        p.add_argument('--' + flag, required=True)
    p.add_argument('--skill', type=Path, required=True)
    p.add_argument('--candidate', type=Path, required=True)
    p.add_argument('--evidence', action='append', required=True)
    p = sub.add_parser('evaluate', help='信頼できるローカルのランナーで、2 つのスナップショットを比べる')
    p.add_argument('id')
    p.add_argument('--suite', type=Path, required=True)
    p.add_argument('--runner', type=Path, required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--tools', required=True)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--timeout', type=float, default=120)
    for action in ('apply', 'rollback', 'show'):
        sub.add_parser(action).add_argument('id')
    sub.add_parser('history')
    p = sub.add_parser('export', help='完了した評価ループのスキルを、持ち運べる証拠の記録と一緒に書き出す')
    p.add_argument('id')
    p.add_argument('--destination', type=Path, required=True)
    sub.add_parser('verify-bundle', help='書き出した証拠とスキルのファイルをすべて検証する').add_argument('path', type=Path)
    args = localize_argparse(parser).parse_args()
    # wiki を使わない操作（verify-bundle）だけを名前で除く。新しい操作は、既定で wiki の判定を通る。
    uses_wiki = args.action not in WIKI_FREE_ACTIONS
    if args.wiki is None and uses_wiki:
        args.wiki = wiki_workspace.default_wiki_or_exit()
    try:
        if uses_wiki:
            # 振り分けより前の 1 か所で、判定してから同じパスを解決する（ADR-0019）。capture の記録も、あとで
            # この wiki にソースとして取り込むので、SCHEMA.md のない wiki には書かない。
            args.wiki = wiki_initialized.resolve(args.wiki)
        if args.action == 'verify-bundle':
            result = verify_bundle(args.path)
        elif args.action == 'capture':
            result = capture(args)
        else:
            with locked(args.wiki) as state:
                if args.action == 'propose':
                    result = propose(args, state)
                elif args.action == 'history':
                    result = history(state)
                else:
                    exp = inside(state, slug(args.id))
                    if args.action == 'evaluate':
                        result = evaluate(args, exp)
                    elif args.action == 'export':
                        result = export_bundle(exp, args.destination)
                    elif args.action == 'show' and (exp / 'run-inputs.json').exists():
                        result = {'inputs': read_json(exp / 'run-inputs.json'), 'results': run_records(exp),
                                  'iterations': [read_json(p) for p in exp.glob('iteration-*.json')],
                                  'selection': read_json(exp / 'selection.json') if (exp / 'selection.json').exists() else None,
                                  'trace_directory': str(exp)}
                    elif args.action == 'show':
                        result = {'proposal': manifest(exp), 'inputs': [read_json(p) for p in exp.glob('inputs-*.json')], 'diff': (exp / 'change.diff').read_text(encoding='utf-8'),
                                  'evaluations': [read_json(p) for p in exp.glob('evaluation-*.json')]}
                    else:
                        result = transition(exp, rollback=args.action == 'rollback')
        print(encode(result), end='')
        if args.action == 'evaluate' and result.get('status') == 'rejected':
            return 2
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
