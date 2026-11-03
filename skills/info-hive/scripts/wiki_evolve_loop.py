#!/usr/bin/env python3
"""範囲を限った WikiSkill のサイクル。学習、統合、提案、検証を行い、最後に1回だけテストする。

実行: python wiki_evolve_loop.py --wiki WIKI --id RUN --skill SKILL --suite SUITE --config CONFIG
ランナーはすべて信頼されたローカルの実行ファイルである。実行によってインストール済みのスキルが変わることはない。
"""
import argparse
from decimal import Decimal, ROUND_DOWN
import fnmatch
import json
import re
import random
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

from wiki_evolve import (atomic, copy_tree, digest, encode, event, finite, inside, locked,
                         propose, read_json, require, run_runner, slug, tree, capture)
from wiki_text import localize_argparse, quote_in_text


def contract(suite, corpus):
    require(suite.get('version') == 2, '評価ループにはスイートの version 2 が必要である')
    tasks = suite.get('tasks')
    require(isinstance(tasks, list) and tasks, '課題がない')
    ids, questions, splits = set(), set(), set()
    for task in tasks:
        slug(task.get('id'))
        require(task['id'] not in ids, '課題の ID が重複している')
        ids.add(task['id'])
        require(task.get('split') in ('train', 'validation', 'test'), 'split には train、validation、test のどれかを使う')
        splits.add(task['split'])
        question = task.get('question')
        require(isinstance(question, str) and question.strip(), '課題の質問がない')
        require(question.strip().casefold() not in questions, '同じ質問が複数の課題にある')
        questions.add(question.strip().casefold())
        require(isinstance(task.get('rubric'), str) and task['rubric'].strip(), '採点の基準（rubric）がない')
        require(type(task.get('abstain')) is bool, 'abstain の期待値を真偽値で書く')
        require(isinstance(task.get('sources'), list), '期待するソース（sources）のリストがない')
        for name in task['sources']:
            require(inside(corpus, name).is_file(), '採点の基準が参照するソースがない')
        require(task['abstain'] or task['sources'] or task.get('artifacts'), '根拠のソースか成果物の検査が必要である')
        for pattern in task.get('allow_write', []):
            require(isinstance(pattern, str) and pattern and not pattern.startswith(('/', '.'))
                    and '..' not in Path(pattern).parts, '安全でない書き込みのパターンである')
        for check in task.get('artifacts', []):
            inside(corpus, check['path'])
            require(check.get('kind') in ('text', 'json', 'contains', 'json_subset', 'jsonl_subset', 'absent'), '成果物の検査の種類が不明である')
            if check['kind'] != 'absent':
                require('equals' in check, '成果物の期待値（equals）がない')
            if check['kind'] == 'contains':
                require(isinstance(check['equals'], list) and check['equals']
                        and all(isinstance(s, str) and s for s in check['equals']), '必須の文字列の断片は空でない文字列のリストにする')
        for command in task.get('required_commands', []):
            require(isinstance(command, list) and command and all(isinstance(s, str) and s for s in command),
                    '必須のコマンドの断片は空でない文字列のリストにする')
    require(splits == {'train', 'validation', 'test'}, '学習用、検証用、最終テスト用の 3 種類の課題がすべて必要である')
    calibration = suite.get('calibration')
    require(isinstance(calibration, list) and len(calibration) >= 2, '判定役の較正には、合格の例と不合格の例が必要である')
    require({c.get('passed') for c in calibration} == {True, False}, '較正の例には、合格と不合格の両方の判定が必要である')
    for case in calibration:
        require(type(case.get('passed')) is bool and type(case.get('abstain')) is bool, '較正の例の passed と abstain は真偽値にする')
        for key in ('question', 'rubric', 'answer'):
            require(isinstance(case.get(key), str), '較正の例の question、rubric、answer は文字列にする')
        require(isinstance(case.get('sources'), list) and isinstance(case.get('citations'), list), '較正の例の sources と citations はリストにする')
        for name in case['sources']:
            require(inside(corpus, name).is_file(), '較正の例が参照するソースがない')
    return tasks


def grounded_verdict(judge, sources, needs_evidence):
    require(type(judge.get('passed')) is bool and isinstance(judge.get('reason'), str), '判定役の判定（passed、reason）がない')
    evidence = judge.get('evidence')
    require(isinstance(evidence, list), '判定役の根拠（evidence）がない')
    # 引用は D2 と同じ規則で照合する（wiki_text.quote_in_text）。日本語の文は行をまたいで
    # 改行が入っていても一致し、英数字の間の空白の有無は区別する。
    valid = all(isinstance(e, dict) and isinstance(e.get('quote'), str) and e['quote']
                and e.get('source') in sources and quote_in_text(e['quote'], sources[e['source']]) for e in evidence)
    return bool(valid and (not needs_evidence or evidence))


def calibrate(suite, corpus, calls, work):
    results = []
    for case in suite['calibration']:
        request = {k: v for k, v in case.items() if k != 'passed'}
        request['sources'] = {p: inside(corpus, p).read_text(encoding='utf-8-sig') for p in case['sources']}
        result = calls.invoke('judge', request, work)
        require(grounded_verdict(result, request['sources'], case['passed'] and bool(request['sources']))
                and result['passed'] == case['passed'],
                '判定役が較正に失敗した。評価ループを始める前に、採点の基準か判定役を見直す')
        results.append(result)
    return results


class Calls:
    """推論、統合、提案、ジャッジの呼び出しを、失敗も含めて記録する1つの台帳。"""
    def __init__(self, config, archive):
        self.config, self.archive = config, archive
        self.runtime_files = {p: digest(p.read_bytes()) for p in Path(__file__).parent.glob('wiki_evolve*.py')}
        self.started = time.monotonic()
        self.count, self.tokens, self.cost = 0, 0, Decimal(0)
        self.cost_known = True
        self.tokens_known = True
        self.last_proposal = None
        runtime = config.get('runtime_python')
        if runtime is not None:
            require(isinstance(runtime, str) and Path(runtime).is_absolute() and Path(runtime).is_file(),
                    'runtime_python には、存在するインタープリターの絶対パスを指定する')
        budget = config['budget']
        for key in ('max_calls', 'max_seconds', 'timeout'):
            require(finite(budget[key], key) > 0, f'{key} は正の値にする')
        require(type(budget['max_calls']) is int, '呼び出しの回数の上限（max_calls）は整数にする')
        if budget.get('max_usd') is not None:
            require(finite(budget['max_usd'], 'max_usd') > 0, 'USD の上限（max_usd）は正の値にする')
        runners = [config[role] for role in ('inference', 'maintainer', 'proposer', 'judge')] + config.get('transfer', [])
        for item in runners:
            require(isinstance(item.get('argv'), list) and item['argv']
                    and all(isinstance(v, str) and v for v in item['argv']), 'ランナーの argv がない')
            require(isinstance(item.get('model'), str) and item['model'], 'ロールごとにモデルを指定する')
            if budget.get('max_usd') is not None:
                api = Path(__file__).with_name('wiki_evolve_api.py').resolve()
                require(len(item['argv']) == 2 and Path(item['argv'][1]).resolve() == api,
                        'USD の厳密な予算を使うには、すべてのロールと転用評価のランナーで wiki_evolve_api.py を使う')
                from wiki_evolve_api import reservation
                reservation(item['model'])

    def invoke(self, role, request, cwd):
        require(all(p.is_file() and digest(p.read_bytes()) == sha for p, sha in self.runtime_files.items()),
                '実行中に評価ループのランタイムが変わった。新しく試し直す')
        budget = self.config['budget']
        remaining = budget['max_seconds'] - (time.monotonic() - self.started)
        require(self.count < budget['max_calls'] and remaining > 0, '呼び出しの回数か時間の予算を使い切った')
        usd = Decimal(str(budget['max_usd'])) if budget.get('max_usd') is not None else None
        require(usd is None or (self.cost_known and self.cost < usd), 'USD の予算を使い切ったか、費用が分からない')
        self.count += 1
        item = self.config[role]
        request = dict(request, version=2, role=role, model=item['model'],
                       max_usd=None if usd is None else float((usd-self.cost).quantize(Decimal('.000001'), rounding=ROUND_DOWN)),
                       runner_options=item.get('options', {}),
                       trace_path=str((self.archive / f'trace-{self.count:05d}.jsonl').resolve()))
        runner_files = {v: digest(Path(v).read_bytes()) for v in item['argv'] if Path(v).is_file()}
        record = {'number': self.count, 'role': role, 'request': request, 'runner': item,
                  'runner_files': runner_files}
        event(self.archive, 'call-start', record)
        try:
            output = run_runner(item['argv'], request, cwd, min(remaining, budget['timeout']))
            require(all(p.is_file() and digest(p.read_bytes()) == sha for p, sha in self.runtime_files.items()),
                    '呼び出しの間に評価ループのランタイムが変わった。この試行の証拠は無効である')
            require(isinstance(output, dict), 'ランナーの出力は JSON のオブジェクトでなければならない')
            if role == 'proposer':
                self.last_proposal = output
            require(type(output.get('tool_calls')) is int and output['tool_calls'] >= 0, 'ツールの呼び出しの回数（tool_calls）がない')
            require(role != 'judge' or output['tool_calls'] == 0, '判定役がツールを使った。盲検の比較として無効である')
            require(isinstance(output.get('events'), list), '観測できるイベントの記録（events）がない')
            usage = output.get('usage', {})
            for key in ('input_tokens', 'output_tokens'):
                require(key in usage and (usage[key] is None or type(usage[key]) is int and usage[key] >= 0),
                        'トークンの使用量は、測った値か明示的な null にする')
            if any(usage[key] is None for key in ('input_tokens', 'output_tokens')):
                self.tokens_known = False
            self.tokens += sum(v for k, v in usage.items() if k in ('input_tokens', 'output_tokens') and v is not None)
            amount = output.get('cost')
            if amount is None:
                self.cost_known = False
            else:
                finite(amount, 'ランナーの cost')
                self.cost += Decimal(str(amount))
            require(usd is None or (self.cost_known and self.cost <= usd), 'ランナーが USD の予算を超えたか、費用を返さなかった')
            event(self.archive, 'call-result', dict(record, output=output))
            return output
        except BaseException as exc:
            self.cost_known = self.tokens_known = False
            trace = Path(request['trace_path'])
            event(self.archive, 'call-error', dict(record, error=type(exc).__name__,
                  message=re.sub(r'(?i)(?:sk-[\w-]+|bearer\s+[\w.\-]+)', '[redacted]', str(exc)[-4000:]),
                  trace_sha256=digest(trace.read_bytes()) if trace.exists() else None))
            raise

    def summary(self):
        return {'calls': self.count, 'tokens': self.tokens if self.tokens_known else None,
                'cost_usd': float(self.cost) if self.cost_known else None,
                'elapsed_seconds': time.monotonic()-self.started}


def skill_text(root):
    # 手順全体を注入するので、スキルの検索結果の違いが比較に混ざることはない。
    return {name: (root / name).read_text(encoding='utf-8') for name in tree(root)}


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(subset(e, a) for a in actual) for e in expected)
    return type(expected) is type(actual) and expected == actual


def command_observations(events):
    commands = {}
    for number, e in enumerate(events):
        identity = (e.get('item', {}).get('id') or e.get('id') or e.get('tool_use_id') or
                    e.get('toolCallId') or e.get('tool_id') or e.get('call_id') or e.get('part', {}).get('callID') or str(number))
        row = commands.setdefault(identity, {})
        # 調べるのは実際のツールの入力だけで、繰り返し出力されたソースの文章やツールの結果は見ない。
        for args in (e.get('item', {}), e.get('input', {}), e.get('args', {}), e.get('parameters', {}),
                     e.get('rawInput', {}), e.get('part', {}).get('state', {}).get('input', {})):
            if isinstance(args, dict) and isinstance(args.get('command'), str):
                row['command'] = args['command']
        for call in e.get('tool_call', {}).values():
            if isinstance(call, dict) and isinstance(call.get('args', {}).get('command'), str):
                row['command'] = call['args']['command']
                if 'result' in call:
                    row['output'] = call['result']
        if 'aggregated_output' in e.get('item', {}):
            row['output'] = e['item']['aggregated_output']
        elif e.get('type') == 'tool_result':
            row['output'] = e.get('content', e.get('output'))
        elif e.get('type') == 'tool_execution_end':
            row['output'] = e.get('result')
        elif 'output' in e.get('part', {}).get('state', {}):
            row['output'] = e['part']['state']['output']
        elif 'rawOutput' in e:
            row['output'] = e['rawOutput']
    return [row for row in commands.values() if 'command' in row]


def command_checks(task, events):
    return [any(all(fragment in row['command'] for fragment in fragments) for row in command_observations(events))
            for fragments in task.get('required_commands', [])]


def artifact_checks(task, wiki):
    results = []
    for check in task.get('artifacts', []):
        path = inside(wiki, check['path'])
        if check['kind'] == 'absent':
            passed = not path.exists()
        elif not path.is_file():
            passed = False
        elif check['kind'] == 'text':
            passed = path.read_text(encoding='utf-8') == check['equals']
        elif check['kind'] == 'contains':
            passed = all(s in path.read_text(encoding='utf-8') for s in check['equals'])
        else:
            try:
                value = ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
                         if check['kind'] == 'jsonl_subset' else read_json(path))
                passed = value == check['equals'] if check['kind'] == 'json' else subset(check['equals'], value)
            except ValueError:
                passed = False
        results.append(passed)
    return results


def rollout(task, skill, corpus, calls, archive, label):
    with tempfile.TemporaryDirectory(prefix='wiki-rollout-') as tmp:
        work = Path(tmp)
        before = copy_tree(corpus, work / 'wiki')
        frozen_skill = copy_tree(skill, work / 'skill')
        original_workspace = tree(work)
        supplied = skill_text(work / 'skill')
        request = {'question': task['question'], 'wiki_root': str(work / 'wiki'),
                   'skill_root': str(work / 'skill'), 'skill_files': supplied,
                   'skill_sha256': digest(encode(supplied).encode()),
                   'allow_write': task.get('allow_write', [])}
        if calls.config.get('runtime_python'):
            request['runtime_python'] = calls.config['runtime_python']
        output = calls.invoke('inference', request, work)
        require(output.get('skill_sha256') == request['skill_sha256'], 'ランナーが、渡したスキルを使ったことを示さなかった（skill_sha256 が一致しない）')
        skill_unchanged = tree(work / 'skill') == frozen_skill
        final_workspace = tree(work)
        outside = [name for name in set(original_workspace) | set(final_workspace)
                   if not name.startswith('wiki/') and original_workspace.get(name) != final_workspace.get(name)]
        if outside:
            event(archive, 'workspace-violation', {'label': label, 'task': task['id'], 'paths': sorted(outside),
                  'before': original_workspace, 'after': final_workspace})
        after = tree(work / 'wiki')
        changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        allowed = task.get('allow_write', [])
        protected = sorted(p for p in changed if not any(fnmatch.fnmatchcase(p, pattern) for pattern in allowed))
        if protected:
            event(archive, 'corpus-violation', {'label': label, 'task': task['id'], 'paths': protected,
                  'before': before, 'after': after})
        integrity = {'protected_files_unchanged': not protected, 'skill_unchanged': skill_unchanged,
                     'workspace_unchanged': not outside, 'permitted_changes': sorted(changed-set(protected))}
        boundary_ok = not outside and not protected and skill_unchanged
        require(isinstance(output.get('answer'), str) and type(output.get('abstain')) is bool,
                '回答（answer）と abstain がない')
        citations = output.get('citations')
        require(isinstance(citations, list) and all(isinstance(p, str) for p in citations), '引用（citations）がない')
        valid = True
        for name in citations:
            try:
                valid = valid and inside(work / 'wiki', name).is_file()
            except ValueError:
                valid = False
        if task['sources'] and not task['abstain']:
            valid = valid and bool(citations)
        artifacts = artifact_checks(task, work / 'wiki')
        commands = command_checks(task, output['events'])
        if boundary_ok:
            observations = {}
            checked = {check['path']: passed_check for check, passed_check in zip(task.get('artifacts', []), artifacts)}
            for name in sorted(set(checked) | changed):
                path = inside(work / 'wiki', name)
                try:
                    content = path.read_text(encoding='utf-8') if path.is_file() else None
                except UnicodeDecodeError:
                    content = None
                observations[name] = {'check_passed': checked.get(name), 'exists': path.is_file(), 'content': content}
            source_paths = set(task['sources']) | (set(citations) if valid else set())
            sources = {p: inside(work / 'wiki', p).read_text(encoding='utf-8') for p in source_paths}
            # ジャッジには、モデル、バリアント、履歴、スキルの指示を渡さない。
            judge = calls.invoke('judge', {'question': task['question'], 'rubric': task['rubric'],
                                  'answer': output['answer'], 'abstain': output['abstain'],
                                  'citations': citations, 'sources': sources, 'artifacts': observations,
                                  'integrity': integrity,
                                  'execution': [row for row in command_observations(output['events'])
                                      if any(all(s in row['command'] for s in fragments) for fragments in task.get('required_commands', []))]}, work)
            # 保存した成果物は観測した変更を証明できるが、事実情報のソースの代わりにはならない。
            evidence_text = sources | {p: o['content'] for p, o in observations.items() if o['content'] is not None}
            grounded = grounded_verdict(judge, evidence_text, not task['abstain'] and bool(sources))
        else:
            # 完了したタスクでも、境界に違反していればスコアは 0 である。その使い捨ての
            # コピーは破棄するので、変更された証拠でモデルのジャッジを通すことはできない。
            judge = {'passed': False, 'reason': 'Task write boundary violated: ' +
                     ', '.join(sorted(outside)+['wiki/'+p for p in protected]), 'evidence': [],
                     'verifier': 'workspace-hashes'}
            grounded = False
        passed = (boundary_ok and valid and all(artifacts) and all(commands) and output['abstain'] == task['abstain']
                  and judge['passed'] and grounded)
        row = {'task': task['id'], 'split': task['split'], 'label': label, 'passed': bool(passed),
               'question': task['question'], 'output': output, 'judge': judge,
               'model': calls.config['inference']['model'], 'runner': calls.config['inference']['argv'],
               'checks': {'citations': valid, 'artifacts': artifacts, 'commands': commands, 'grounding': bool(grounded),
                          'integrity': integrity},
               'changed': sorted(changed)}
        # 成功したという主張だけでなく、成果物を証拠として残す。
        artifact_dir = archive / ('artifacts-' + slug(label) + '-' + task['id'])
        artifact_dir.mkdir()
        for p in changed:
            if p in after:
                target = artifact_dir / p  # パスは、シンボリックリンクを含まないツリーの走査から得たものである。
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(work / 'wiki' / p, target)
        event(archive, 'rollout', row)
        return row


def evaluate(tasks, skill, corpus, calls, archive, label, repeats):
    return [rollout(task, skill, corpus, calls, archive, f'{label}-{repeat}')
            for repeat in range(repeats) for task in tasks]


def compare(tasks, skills, corpus, calls, archive, label, repeats):
    scores = {name: [] for name in skills}
    for repeat in range(repeats):
        order = list(skills) if repeat % 2 == 0 else list(reversed(skills))
        for variant in order:
            scores[variant] += evaluate(tasks, skills[variant], corpus, calls, archive,
                                        f'{label}-{repeat}-{variant}', 1)
    return scores


def paired_report(scores, alpha=.05):
    """タスクのクラスター単位のブートストラップと、対応のあるランダム化検定。繰り返しは新しいタスクとして扱わない。"""
    baseline, selected = scores['baseline'], scores['selected']
    ids = sorted({r['task'] for r in baseline})
    require(ids and {r['task'] for r in selected} == set(ids), '最終テストの課題が対になっていない')
    differences, metrics = [], {}
    for tid in ids:
        a, b = [r for r in baseline if r['task'] == tid], [r for r in selected if r['task'] == tid]
        require(len(a) == len(b), '最終テストの繰り返しが対になっていない')
        differences.append(sum(r['passed'] for r in b)/len(b) - sum(r['passed'] for r in a)/len(a))
    mean = sum(differences)/len(ids)
    rng = random.Random(0)
    # ponytail: タスクのクラスターから 10,000 回抽出する。独立に学習した多数のスキルを
    # まとめて比べる必要があるなら、事前登録した階層モデルを使う。
    draws = sorted(sum(rng.choice(differences) for _ in ids)/len(ids) for _ in range(10000))
    interval = [draws[int(alpha/2*len(draws))], draws[min(len(draws)-1, int((1-alpha/2)*len(draws)))]]
    if len(ids) <= 16:
        randomized = [sum(d * (1 if mask & (1 << i) else -1) for i, d in enumerate(differences))/len(ids)
                      for mask in range(1 << len(ids))]
        p = sum(abs(v) >= abs(mean)-1e-12 for v in randomized)/len(randomized)
    else:
        p = (1 + sum(abs(sum(d*rng.choice((-1,1)) for d in differences)/len(ids)) >= abs(mean)-1e-12
                     for _ in range(10000)))/10001
    for metric in ('tool_calls', 'cost'):
        values = {name: [r['output'].get(metric) for r in rows] for name, rows in scores.items()}
        metrics[metric] = {name: sum(vs)/len(vs) if all(v is not None for v in vs) else None
                           for name, vs in values.items()}
    return {'independent_tasks': len(ids), 'paired_executions': len(baseline), 'accuracy_delta': mean,
            'confidence_level': 1-alpha, 'task_bootstrap_interval': interval, 'paired_randomization_p': p,
            'alpha': alpha, 'positive_evidence': mean > 0 and interval[0] > 0 and p < alpha,
            'mean_inference_metrics': metrics}


def consolidate(rows, patterns, history, calls, workspace, iteration):
    output = calls.invoke('maintainer', {'experiences': rows, 'patterns': patterns, 'history': history}, workspace)
    updates = output.get('patterns')
    require(isinstance(updates, list), 'メンテナーが patterns を返さなかった')
    available = {r['task'] for r in rows}
    for pattern in updates:
        slug(pattern.get('id'))
        require(all(isinstance(pattern.get(k), str) and pattern[k].strip()
                    for k in ('claim', 'scope', 'counterexamples')), 'パターンには claim、scope、counterexamples が必要である')
        require(isinstance(pattern.get('evidence'), list) and pattern['evidence']
                and set(pattern['evidence']) <= available, 'パターンは、今回観測した課題を根拠に挙げなければならない')
        prior = patterns.get(pattern['id'], {})
        patterns[pattern['id']] = dict(pattern, iteration=iteration, sources=prior.get('sources', []))
    return patterns


def publish_learning(args, rows, patterns, iteration):
    """観測できる学習の raw の証拠と、検索できる通常のソースとコンセプトのページ。"""
    raw = getattr(args, 'raw', None) or args.wiki.parent / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    today = time.strftime('%Y-%m-%d')
    source_names = []
    for row in rows:
        name = 'evolution-' + digest(f"{args.id}:{iteration}:{row['task']}".encode())[:20]
        record = {'id': name, 'task': row['question'], 'outcome': 'success' if row['passed'] else 'failure',
                  'actions': [encode(e).strip() for e in row['output']['events']] or ['No tool events observed'],
                  'verification': encode({'judge': row['judge'], 'checks': row['checks']}),
                  'model': row['model'], 'tools': encode(row['runner']).strip(),
                  'scope': 'training observation; not a causal generalization', 'observable': row}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'experience.json'
            path.write_text(encode(record), encoding='utf-8')
            receipt = capture(SimpleNamespace(raw=raw, record=path))
        source = f'sources/{name}.md'
        target = inside(args.wiki, source)
        target.parent.mkdir(exist_ok=True)
        body = (f'---\ntitle: "学習時の観察 {name}"\ntype: source\ntags: [skill-evolution]\n'
                f'created: {today}\nupdated: {today}\nauthors: ["評価ループ"]\n'
                f'raw: {json.dumps(receipt["raw"], ensure_ascii=False)}\ningested: {today}\n---\n\n'
                f"課題: {row['question']}\n\n結果: {record['outcome']}\n\n"
                + row['judge']['reason'] + '\n')
        body = body.replace('—', '-')
        require(not target.exists() or target.read_text(encoding='utf-8') == body, '学習時の観察のソースが、別の内容ですでにある')
        if not target.exists():
            target.write_text(body, encoding='utf-8')
        source_names.append(source)
    for key, pattern in patterns.items():
        if pattern.get('run') != args.id or pattern['iteration'] != iteration:
            continue
        pattern['sources'] = list(dict.fromkeys(pattern.get('sources', []) + source_names))
        pattern['page'] = 'concepts/evolution-pattern-' + key + '.md'
        target = args.wiki / 'concepts' / ('evolution-pattern-' + key + '.md')
        target.parent.mkdir(exist_ok=True)
        body = (f'---\ntitle: {json.dumps(key.replace("-", " "), ensure_ascii=False)}\ntype: concept\ntags: [skill-evolution]\n'
                f'created: {today}\nupdated: {today}\nsources: {json.dumps(pattern["sources"], ensure_ascii=False)}\n---\n\n'
                f"{pattern['claim']}\n\n適用範囲: {pattern['scope']}\n\n反例: {pattern['counterexamples']}\n\n"
                + '\n'.join(f'- [[{name[:-3]}]]' for name in source_names) + '\n')
        if target.exists():
            previous = target.read_text(encoding='utf-8')
            previous = re.sub(r'^updated:.*$', 'updated: ' + today, previous, count=1, flags=re.M)
            # 置換文字列にバックスラッシュが入っても re がエスケープとして解釈しないよう、関数で渡す。
            sources_line = 'sources: ' + json.dumps(pattern['sources'], ensure_ascii=False)
            previous = re.sub(r'^sources:.*$', lambda _: sources_line, previous, count=1, flags=re.M)
            body = previous.rstrip() + f'\n\n## {args.id} の {iteration} 回目の観察\n\n' + body.split('---', 2)[2].lstrip()
        atomic(target, body.replace('—', '-').encode())
    index = args.wiki / 'index.md'
    old = index.read_text(encoding='utf-8-sig') if index.exists() else '# Wiki のインデックス\n'
    links = source_names + [p['page'] for p in patterns.values() if p.get('page')]
    for name in links:
        line = f'- [[{name[:-3]}]]'
        if line not in old:
            old = old.rstrip() + '\n' + line + '\n'
    atomic(index, old.encode())
    log = args.wiki / 'log.md'
    old = log.read_text(encoding='utf-8-sig') if log.exists() else '# Wiki のログ\n'
    line = f'- {today}: 評価ループ {args.id} の {iteration} 回目の学習の記録を取り込んだ。'
    if line not in old:
        atomic(log, (old.rstrip() + '\n' + line + '\n').encode())



def fingerprint(skill):
    return digest(encode({p: v for p, v in tree(skill).items() if p != 'PURPOSE.md'}).encode())


def run(args, state):
    archive = state / slug(args.id)
    require(not archive.exists(), 'この実行の ID はすでにある。最終テストを使ったあとの実行は、完了したものも中断したものも再開できない')
    require(not args.skill.is_symlink() and not args.skill.resolve().is_relative_to(state.resolve()),
            'スキルには、実験の記録の外にある作業用のディレクトリを指定する')
    require(not args.skill.exists() or (args.skill / 'SKILL.md').is_file() or not tree(args.skill),
            'スキルのディレクトリには SKILL.md が必要である（空のディレクトリならよい）')
    suite_path = args.suite.resolve()
    suite = read_json(suite_path)
    corpus = inside(suite_path.parent, suite['corpus'])
    tasks = contract(suite, corpus)
    consumed_path = state / 'consumed-tests.json'
    consumed = read_json(consumed_path) if consumed_path.exists() else {}
    test_keys = [digest(encode({'question': t['question'], 'rubric': t['rubric'],
                  'sources': {name: inside(corpus, name).read_text(encoding='utf-8-sig') for name in t['sources']}}).encode())
                 for t in tasks if t['split'] == 'test']
    require(not any(key in consumed for key in test_keys), '最終テストの課題はすでに使われた。新しい最終テスト用の課題を用意する')
    config = read_json(args.config)
    slug(args.id + '-selected')
    require(type(config.get('iterations')) is int and config['iterations'] > 0, 'iterations は正の整数にする')
    require(type(config.get('repeats')) is int and config['repeats'] > 0, 'repeats は正の整数にする')
    archive.mkdir()
    calls = Calls(config, archive)
    atomic(archive / 'run-inputs.json', encode({'suite': suite, 'config': config, 'skill': str(args.skill)}).encode())
    runtime = archive / 'runtime'
    runtime.mkdir()
    for script in Path(__file__).parent.glob('wiki_evolve*.py'):
        shutil.copy2(script, runtime / script.name)
    atomic(archive / 'runtime-hashes.json', encode(tree(runtime)).encode())
    corpus_hash = copy_tree(corpus, archive / 'corpus')
    initial = archive / 'baseline'
    if args.skill.exists():
        copy_tree(args.skill, initial)
    else:
        initial.mkdir()
    copy_tree(initial, archive / 'best')
    learning_path = state / 'learning.json'
    learning = read_json(learning_path) if learning_path.exists() else {'patterns': {}, 'history': []}
    patterns, history = learning['patterns'], []
    prior_history = learning['history']
    seen = {fingerprint(initial)} | {h['fingerprint'] for h in prior_history if h['status'] == 'rejected' and h.get('baseline') == fingerprint(initial)}
    split = {s: [t for t in tasks if t['split'] == s] for s in ('train', 'validation', 'test')}
    try:
        with tempfile.TemporaryDirectory(prefix='wiki-evolution-') as tmp:
            work = Path(tmp)
            best = archive / 'best'
            calibrate(suite, archive / 'corpus', calls, work)
            for iteration in range(config['iterations']):
                rows = evaluate(split['train'], best, archive / 'corpus', calls, archive,
                                f'train-{iteration}', 1)
                publish_learning(args, rows, {}, iteration)
                patterns = consolidate(rows, patterns, prior_history + history, calls, work, iteration)
                for pattern in patterns.values():
                    if pattern.get('iteration') == iteration and not pattern.get('run'):
                        pattern['run'] = args.id
                publish_learning(args, rows, patterns, iteration)
                if not patterns:
                    event(archive, 'iteration', {'iteration': iteration, 'status': 'no-patterns'})
                    continue
                atomic(archive / 'patterns.json', encode(patterns).encode())
                proposal = calls.invoke('proposer', {'skill_files': skill_text(best), 'patterns': patterns,
                                        'experiences': rows, 'history': prior_history + history}, work)
                candidate = work / f'candidate-{iteration}'
                copy_tree(best, candidate)
                try:
                    changes = proposal.get('changes')
                    if 'files' in proposal:
                        files = proposal['files']
                        require(isinstance(files, list) and all(isinstance(f, dict) and isinstance(f.get('path'), str) for f in files),
                                '提案の files には、文字列の path を書く')
                        require(len({f['path'] for f in files}) == len(files), '提案のファイルのパスが重複している')
                        changes = {f['path']: f['content'] for f in files}
                    require(isinstance(changes, dict) and changes, 'プロポーザーが、ファイルの変更を返さなかった')
                    require(isinstance(proposal.get('reason'), str) and proposal['reason'].strip(), '提案の理由（reason）がない')
                    evidence = proposal.get('evidence')
                    require(isinstance(evidence, list) and evidence and set(evidence) <= set(patterns),
                            '提案は、既存のパターンを根拠に挙げなければならない')
                    for name, body in changes.items():
                        require(isinstance(name, str), '提案のファイルのパスは文字列にする')
                        path = inside(candidate, name)
                        require(body is None or isinstance(body, str), '変更するファイルの中身は、文字列か null にする')
                        if body is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(body, encoding='utf-8')
                    require((candidate / 'SKILL.md').is_file() and (candidate / 'SKILL.md').read_text(encoding='utf-8-sig').strip(), '提案のあとにも、空でない SKILL.md が必要である')
                    purpose = '# 目的\n\n' + proposal['reason'] + '\n\n'
                    for key in evidence:
                        item = patterns[key]
                        purpose += f"## {key}\n\n{item['claim']}\n\n適用範囲: {item['scope']}\n\n反例: {item['counterexamples']}\n\n根拠: 実行 {item.get('run', args.id)}、{item['iteration']} 回目、課題 {'、'.join(item['evidence'])}。\n\n"
                    (candidate / 'PURPOSE.md').write_text(purpose, encoding='utf-8')
                    candidate_hash = fingerprint(candidate)
                except (ValueError, TypeError, KeyError, FileExistsError, IsADirectoryError, NotADirectoryError) as exc:
                    # モデルが書いた無効なパッチは却下された提案として扱う。残りのサイクルを失ったり、
                    # 固定した最終テストを飛ばしたりする理由にはしない。
                    entry = {'iteration': iteration, 'status': 'rejected', 'invalid_reason': str(exc),
                             'reason': proposal.get('reason', 'Invalid proposal'), 'changes': proposal.get('changes', {}),
                             'files': proposal.get('files'),
                             'evidence': proposal.get('evidence', []), 'fingerprint': fingerprint(candidate),
                             'baseline': fingerprint(best), 'run': args.id}
                    copy_tree(candidate, archive / f'invalid-candidate-{iteration}')
                    history.append(entry)
                    event(archive, 'iteration', entry)
                    atomic(learning_path, encode({'patterns': patterns, 'history': prior_history + history}).encode())
                    continue
                entry = {'iteration': iteration, 'reason': proposal['reason'], 'changes': changes,
                         'evidence': evidence, 'fingerprint': candidate_hash, 'baseline': fingerprint(best), 'run': args.id}
                if candidate_hash in seen:
                    entry['status'] = 'duplicate'
                else:
                    seen.add(candidate_hash)
                    copy_tree(candidate, archive / f'candidate-{iteration}')
                    # 繰り返しを対にして順序を交互にする。候補を選ぶのは検証だけである。
                    scores = compare(split['validation'], {'best': best, 'candidate': candidate},
                                     archive / 'corpus', calls, archive, f'validation-{iteration}', config['repeats'])
                    totals = {v: sum(r['passed'] for r in rs) for v, rs in scores.items()}
                    entry['validation'] = totals
                    limits = config.get('selection', {})
                    metrics = {}
                    for metric, setting in (('tool_calls', 'max_tool_ratio'), ('cost', 'max_cost_ratio')):
                        amounts = {v: [r['output'].get(metric) for r in rs] for v, rs in scores.items()}
                        limit = limits.get(setting)
                        if limit is not None:
                            finite(limit, setting)
                            require(all(x is not None for xs in amounts.values() for x in xs),
                                    '選定に使う指標が測れていない。その比率の上限を設定から外す')
                        sums = {v: sum(xs) if all(x is not None for x in xs) else None for v, xs in amounts.items()}
                        metrics[metric] = dict(sums, limit=limit, passed=limit is None or sums['candidate'] <= sums['best'] * limit)
                    critical = [t['id'] for t in split['validation'] if t.get('critical')]
                    regressions = [tid for tid in critical if
                        sum(r['passed'] for r in scores['candidate'] if r['task'] == tid) <
                        sum(r['passed'] for r in scores['best'] if r['task'] == tid)]
                    entry['metrics'], entry['regressions'] = metrics, regressions
                    entry['status'] = 'accepted' if (totals['candidate'] > totals['best']
                        and all(m['passed'] for m in metrics.values()) and not regressions) else 'rejected'
                    if entry['status'] == 'accepted':
                        shutil.rmtree(best)
                        copy_tree(candidate, best)
                history.append(entry)
                event(archive, 'iteration', entry)
                atomic(learning_path, encode({'patterns': patterns, 'history': prior_history + history}).encode())
            # 最終テストの前に選定を固定する。テストの結果は、パターンにもプロポーザーの履歴にも入れない。
            atomic(archive / 'selection.json', encode({'skill': tree(best), 'history': history}).encode())
            consumed.update({key: args.id for key in test_keys})
            atomic(consumed_path, encode(consumed).encode())
            final = compare(split['test'], {'baseline': initial, 'selected': best},
                            archive / 'corpus', calls, archive, 'test', config['repeats'])
            transfer = []
            original_inference = calls.config['inference']
            try:
                for number, runner in enumerate(config.get('transfer', [])):
                    calls.config['inference'] = runner
                    rows = compare(split['test'], {'baseline': initial, 'selected': best},
                                   archive / 'corpus', calls, archive, f'transfer-{number}', config['repeats'])
                    measured = {v: {'passed': sum(r['passed'] for r in rs), 'total': len(rs)} for v, rs in rows.items()}
                    transfer.append({'runner': runner, 'test': measured,
                                     'paired_analysis': paired_report(rows, .05/(1+len(config.get('transfer', []))))})
            finally:
                calls.config['inference'] = original_inference
            require(tree(archive / 'corpus') == corpus_hash, '記録したコーパスが変わった')
            report = {'status': 'completed', 'history': history,
                      'test': {v: {'passed': sum(r['passed'] for r in rows), 'total': len(rows)}
                               for v, rows in final.items()}, 'budget': calls.summary(),
                      'selected': tree(best), 'corpus': corpus_hash, 'transfer': transfer}
            report['paired_analysis'] = paired_report(final, .05/(1+len(transfer)))
            report['skill_changed'] = tree(best) != tree(initial)
            report['benefit_demonstrated'] = report['skill_changed'] and report['paired_analysis']['positive_evidence']
            # 採用には、既存のレビュー済みの提案とロールバックのワークフローを使う。
            if tree(best) != tree(initial):
                evidence_dir = args.wiki / 'concepts'
                evidence_dir.mkdir(exist_ok=True)
                evidence_name = f'concepts/evolution-{args.id}.md'
                evidence_path = inside(args.wiki, evidence_name)
                require(not evidence_path.exists(), '評価ループの根拠のページがすでにある')
                today = time.strftime('%Y-%m-%d')
                selected_sources = list(dict.fromkeys(p for pattern in patterns.values() for p in pattern.get('sources', [])))
                body = (f'---\ntitle: "評価ループ {args.id}"\ntype: concept\ntags: [skill-evolution]\n'
                        f'created: {today}\nupdated: {today}\nsources: {json.dumps(selected_sources, ensure_ascii=False)}\n---\n\n'
                        + (best / 'PURPOSE.md').read_text(encoding='utf-8') + '\n'.join(f'- [[{p[:-3]}]]' for p in selected_sources) + '\n選定の根拠: 検証用の課題で成績がはっきり上がった。最終の結果は実験の記録に残している。\n')
                evidence_path.write_text(body.replace('—', '-'), encoding='utf-8')
                index = args.wiki / 'index.md'
                old = index.read_text(encoding='utf-8-sig') if index.exists() else '# Wiki のインデックス\n'
                atomic(index, (old.rstrip() + f'\n- [[concepts/evolution-{args.id}]]\n').encode())
                proposal_id = slug(args.id + '-selected')
                propose(SimpleNamespace(id=proposal_id, skill=args.skill, target=None, candidate=best,
                                        wiki=args.wiki, evidence=[evidence_name], reason='上限を決めた評価ループの検証で選ばれた'), state)
                exp = state / proposal_id
                copy_tree(archive / 'corpus', exp / 'corpus')
                # テストは報告であり、もう1つの選定のゲートではない。適用するかはレビュアーが決める。
                event(exp, 'evaluation', {'status': 'passed', 'selection': str(archive / 'selection.json'),
                      'final_test': report['test'], 'corpus': corpus_hash,
                      'proposal_sha256': digest((exp / 'proposal.json').read_bytes())})
                report['proposal'] = proposal_id
            event(archive, 'run', report)
            return report
    except BaseException as exc:
        # 失敗した提案の文脈は残すが、最終テストの結果は学習に使わない。
        if not (archive / 'selection.json').exists() and calls.last_proposal is not None:
            failure = {'run': args.id, 'status': 'error', 'error': type(exc).__name__,
                       'proposal': calls.last_proposal}
            atomic(learning_path, encode({'patterns': patterns, 'history': prior_history + history + [failure]}).encode())
        event(archive, 'run', {'status': 'error', 'error': type(exc).__name__,
              'message': re.sub(r'(?i)(?:sk-[\w-]+|bearer\s+[\w.\-]+)', '[redacted]', str(exc)[-4000:]),
              'budget': calls.summary()})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('wiki', 'skill', 'suite', 'config'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--raw', type=Path, help='変更できない経験の記録を置くディレクトリ（既定: wiki と同じ階層の raw）')
    args = localize_argparse(parser).parse_args()
    args.wiki, args.skill = args.wiki.resolve(), args.skill.absolute()
    try:
        with locked(args.wiki) as state:
            print(encode(run(args, state)), end='')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
