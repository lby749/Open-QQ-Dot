"""Repair tool for string literals that the English port damaged.

openqqdot-en was produced from Open-QQ-Dot by a translation pass that, for many
literals, deleted the CJK characters (``'无法联网'`` -> ``''``) or replaced them
with word-substitution noise (``'同一字段不能同时填写中文和英文名称。'`` ->
``'charsmedium. '``). Because ``'' in text`` is always True, keyword checks,
refusal detection, routing tables and prompt labels silently broke.

The tool aligns every string token of a ported file with the original file
through the surrounding code tokens (difflib over a skeleton where strings are
placeholders), then decides per aligned pair:

  restore   the ported text lost the content: it carries fewer than
            ``RATIO`` new ASCII letters per original CJK character
  bilingual the ported text is a genuine translation inside a string-only
            tuple/list/set (a keyword vocabulary): keep the English and add
            the original back, so Chinese and English messages both match
  keep      a genuine translation anywhere else, or a docstring

Damaged UI/log strings (logger/print/raise/GUI widget calls) are listed under
``ui_log`` so they can be given English translations through
``--translations`` (JSON: original literal source -> replacement literal
source); without one they are restored verbatim.

Usage:
  python tools/restore_literals.py --original ../Open-QQ-Dot --report out.json
  python tools/restore_literals.py --original ../Open-QQ-Dot --apply [--translations t.json]
"""
from __future__ import annotations

import argparse
import ast
import difflib
import io
import json
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RATIO = 1.2
CJK = re.compile(r'[぀-ヿ㐀-䶿一-鿿豈-﫿]')
# Fullwidth / CJK punctuation. Turning '，' into ', ' is a fine translation in a message, but in a
# regex character class it changes what matches, so punctuation-only damage is restored in regexes only.
CJK_PUNCT = re.compile(r'[　-〿＀-￯‘-”…「-』【】]')
LETTER = re.compile(r'[A-Za-z]')
UI_LOG = re.compile(r'\b(logger|logging|print|warnings\.warn|messagebox|QMessageBox|QFileDialog|InfoBar|'
                    r'set(Text|ToolTip|PlaceholderText|WindowTitle|Title|StatusTip)|addItems?|insert|'
                    r'QLabel|QPushButton|QCheckBox|QGroupBox|QAction|Card|hint|Label|Button|'
                    r'raise\s+\w*(Error|Exception))\b')
SKIP_PARTS = {'.venv', '__pycache__', 'build', 'dist', '_backup_before_dsh_focus', 'node_modules'}
# Display-only modules: their damaged text needs an English translation pass, not a CJK restore.
GUI_MODULES = re.compile(r'^(house_qt/|gui\.pyw$|.*_gui\.py$|house_status\.py$|house_web\.py$|launcher\.py$)')
SKIP_TYPES = {tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
              tokenize.ENDMARKER, tokenize.TYPE_COMMENT if hasattr(tokenize, 'TYPE_COMMENT') else -1}
FSTART = getattr(tokenize, 'FSTRING_START', None)
FEND = getattr(tokenize, 'FSTRING_END', None)


class Tok:
    __slots__ = ('kind', 'text', 'start', 'end')

    def __init__(self, kind, text, start, end):
        self.kind, self.text, self.start, self.end = kind, text, start, end


def _offsets(source: str) -> list[int]:
    out, pos = [0], 0
    for line in source.splitlines(keepends=True):
        pos += len(line)
        out.append(pos)
    return out


def tokens(source: str) -> list[Tok]:
    """Code tokens with every (f-)string literal collapsed into one STR token."""
    offs = _offsets(source)
    at = lambda rc: offs[rc[0] - 1] + rc[1]
    out: list[Tok] = []
    depth, fstart = 0, None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if FSTART is not None and tok.type == FSTART:
            if depth == 0:
                fstart = tok.start
            depth += 1
            continue
        if FEND is not None and tok.type == FEND:
            depth -= 1
            if depth == 0:
                s, e = at(fstart), at(tok.end)
                out.append(Tok('STR', source[s:e], s, e))
            continue
        if depth or tok.type in SKIP_TYPES:
            continue
        if tok.type == tokenize.STRING:
            s, e = at(tok.start), at(tok.end)
            out.append(Tok('STR', source[s:e], s, e))
        else:
            out.append(Tok(tok.string, tok.string, at(tok.start), at(tok.end)))
    return out


def _string_collection(node) -> bool:
    return isinstance(node, (ast.Tuple, ast.List, ast.Set)) and len(node.elts) >= 2 and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)


def _matching_names(tree) -> set[str]:
    """Names used as the haystack of ``in``, ``startswith``/``endswith`` or iteration."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, (ast.Name, ast.Attribute)):
                    names.add(right.id if isinstance(right, ast.Name) else right.attr)
        elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.iter, (ast.Name, ast.Attribute)):
            names.add(node.iter.id if isinstance(node.iter, ast.Name) else node.iter.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                'startswith', 'endswith') and node.args and isinstance(node.args[0], (ast.Name, ast.Attribute)):
            arg = node.args[0]
            names.add(arg.id if isinstance(arg, ast.Name) else arg.attr)
    return names


def ast_roles(source: str) -> tuple[set[int], dict[int, tuple[int, int, list[int]]]]:
    """Docstring offsets, and matching-vocabulary member offset -> (span start, span end, member offsets).

    A vocabulary is a string-only tuple/list/set that is searched, never unpacked:
    the right side of ``in``, a ``startswith`` argument, the iterable of a loop or
    comprehension, a value bound to a name that is used that way, or a value of a
    dict whose values are all such collections (a keyword map).
    """
    offs = _offsets(source)
    lines = source.splitlines(keepends=True)
    # ast columns are UTF-8 byte offsets; convert them to character offsets.
    char = lambda line, col: offs[line - 1] + len(lines[line - 1].encode('utf-8')[:col].decode('utf-8', 'ignore'))
    at = lambda node: char(node.lineno, node.col_offset)
    end = lambda node: char(node.end_lineno, node.end_col_offset)
    docs, vocab = set(), {}
    tree = ast.parse(source)
    used = _matching_names(tree)
    collections = []
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.add(at(first.value))
        if isinstance(node, ast.Compare):
            collections += [r for op, r in zip(node.ops, node.comparators)
                            if isinstance(op, (ast.In, ast.NotIn)) and _string_collection(r)]
        elif isinstance(node, (ast.For, ast.comprehension)) and _string_collection(node.iter):
            collections.append(node.iter)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                'startswith', 'endswith') and node.args and _string_collection(node.args[0]):
            collections.append(node.args[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound = {t.id if isinstance(t, ast.Name) else getattr(t, 'attr', None) for t in targets}
            if _string_collection(node.value) and bound & used:
                collections.append(node.value)
        # A keyword map's values are searched; uniform 2-tuples are (a, b) records that get unpacked.
        if isinstance(node, ast.Dict) and node.values and all(_string_collection(v) for v in node.values) \
                and not all(isinstance(v, ast.Tuple) and len(v.elts) == 2 for v in node.values):
            collections += node.values
    for coll in collections:
        members = tuple(at(e) for e in coll.elts)
        span = (members[0], end(coll.elts[-1]), members)
        vocab.update((m, span) for m in members)
    return docs, vocab


def _value(literal: str):
    try:
        return ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        return None


def merge_vocabulary(port_src: str, port_text: dict, orig_text: dict, port_span, orig_span) -> str | None:
    """Union of the port's meaningful entries and every original entry, or None if nothing changes."""
    p_items = [port_text.get(m, '') for m in port_span[2]]
    o_items = [orig_text.get(m, '') for m in orig_span[2]]
    merged, seen = [], set()
    for text in p_items + o_items:
        value = _value(text)
        if not isinstance(value, str):
            return None  # f-strings or implicit concatenation: leave for review
        # A ported entry with no letters or CJK left is translation debris ('' or ', ').
        if value in seen or not (LETTER.search(value) or CJK.search(value) or text in o_items):
            continue
        seen.add(value)
        merged.append(text)
    if [_value(t) for t in merged] == [_value(t) for t in p_items]:
        return None
    multiline = '\n' in port_src[port_span[0]:port_span[1]]
    if multiline:
        line_start = port_src.rfind('\n', 0, port_span[0]) + 1
        indent = re.match(r'\s*', port_src[line_start:port_span[0]]).group(0)
        return (',\n' + indent).join(merged)
    return ', '.join(merged)


def new_letters(ported: str, original: str) -> int:
    return len(LETTER.findall(ported)) - len(LETTER.findall(CJK.sub('', original)))


# Word-substitution debris glues dictionary words together ("itemsSend", "okmedium", "modelchars").
GLUED = re.compile(r'[a-z][A-Z]|\b(ok|items|medium|days|chars|months)[a-z]|[a-z](items|medium|chars|days)\b')
WORD = re.compile(r'[A-Za-z]{3,}')


def is_damaged(ported: str, original: str) -> bool:
    """The port lost the literal's content instead of translating it."""
    cjk = len(CJK.findall(original))
    ratio = new_letters(ported, original) / cjk
    if ratio >= RATIO:
        return False
    # A compact but genuine translation: several new real words, no glued debris.
    new_words = set(WORD.findall(ported)) - set(WORD.findall(original))
    return not (ratio >= 0.6 and len(new_words) >= 4 and not GLUED.search(ported))


EDGE = 2  # aligned code tokens required on each side before a single string is auto-applied
CONTEXT = 6  # neighbouring tokens whose text must match (CJK-bearing strings compare as STR)


def _same_token(o: Tok, p: Tok) -> bool:
    if o.kind != p.kind:
        return False
    if o.kind != 'STR' or o.text == p.text:
        return o.text == p.text
    return bool(CJK.search(o.text))  # a CJK literal the port translated or blanked


def context_matches(o_toks, p_toks, o_first, o_last, p_first, p_last) -> bool:
    """The code around a literal (or collection) reads the same in both trees, not just has the same shape."""
    window = lambda ts, a, b: ts[max(0, a):max(0, b)]
    before = zip(window(o_toks, o_first - CONTEXT, o_first), window(p_toks, p_first - CONTEXT, p_first))
    after = zip(o_toks[o_last + 1:o_last + 1 + CONTEXT], p_toks[p_last + 1:p_last + 1 + CONTEXT])
    return all(_same_token(o, p) for o, p in before) and all(_same_token(o, p) for o, p in after)


def analyse(orig_src: str, port_src: str) -> dict:
    o_toks, p_toks = tokens(orig_src), tokens(port_src)
    docs, vocab = ast_roles(port_src)
    _, orig_vocab = ast_roles(orig_src)
    port_text = {t.start: t.text for t in p_toks if t.kind == 'STR'}
    orig_text = {t.start: t.text for t in o_toks if t.kind == 'STR'}
    p_index = {t.start: i for i, t in enumerate(p_toks)}
    o_index = {t.start: i for i, t in enumerate(o_toks)}
    skeleton = lambda ts: [t.kind for t in ts]
    matcher = difflib.SequenceMatcher(None, skeleton(o_toks), skeleton(p_toks), autojunk=False)
    lines = port_src.splitlines()
    line_of = _offsets(port_src)
    find_line = lambda off: next(i for i in range(len(line_of)) if line_of[i + 1] > off)
    actions, merged_spans = [], set()
    aligned_orig = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'equal':
            continue
        for k in range(i2 - i1):
            o, p = o_toks[i1 + k], p_toks[j1 + k]
            if o.kind != 'STR':
                continue
            aligned_orig.add(i1 + k)
            if p.start in vocab and o.start in orig_vocab:
                span = vocab[p.start]
                if span[0] in merged_spans:
                    continue
                merged_spans.add(span[0])
                o_span = orig_vocab[o.start]
                repl = merge_vocabulary(port_src, port_text, orig_text, span, o_span)
                if repl is not None:
                    anchored = context_matches(o_toks, p_toks, o_index[o_span[2][0]], o_index[o_span[2][-1]],
                                               p_index[span[2][0]], p_index[span[2][-1]])
                    actions.append({'line': find_line(span[0]) + 1, 'kind': 'vocabulary' if anchored else 'review',
                                    'start': span[0], 'end': span[1], 'ported': port_src[span[0]:span[1]],
                                    'original': repl})
                continue
            if o.text == p.text or p.start in docs:
                continue
            line_no = find_line(p.start)
            if not CJK.search(o.text):
                in_regex = o.text[:1] in 'rR' or 're.' in lines[line_no] or 'regex' in lines[line_no].lower()
                if CJK_PUNCT.search(o.text) and in_regex and CJK_PUNCT.sub('', o.text) != p.text:
                    actions.append({'line': line_no + 1, 'kind': 'restore', 'start': p.start, 'end': p.end,
                                    'ported': p.text, 'original': o.text})
                continue
            if not is_damaged(p.text, o.text):
                continue  # a genuine translation
            if min(k, i2 - i1 - 1 - k) < EDGE or not context_matches(o_toks, p_toks, i1 + k, i1 + k, j1 + k, j1 + k):
                kind = 'review'
            elif UI_LOG.search(lines[line_no]):
                kind = 'ui_log'
            else:
                kind = 'restore'
            actions.append({'line': line_no + 1, 'kind': kind, 'start': p.start, 'end': p.end,
                            'ported': p.text, 'original': o.text})
    unaligned = sum(1 for i, t in enumerate(o_toks) if t.kind == 'STR' and i not in aligned_orig and CJK.search(t.text))
    return {'actions': actions, 'unaligned_cjk_strings': unaligned}


def apply_actions(port_src: str, actions: list[dict], translations: dict, accepted: set[int] = frozenset()) -> tuple[str, int]:
    translated = 0
    for act in sorted(actions, key=lambda a: a['start'], reverse=True):
        if act['kind'] == 'review' and act['line'] not in accepted:
            continue
        if act['kind'] == 'ui_log' and act['original'] in translations:
            repl = translations[act['original']]
            translated += 1
        else:
            repl = act['original']
        port_src = port_src[:act['start']] + repl + port_src[act['end']:]
    return port_src, translated


def iter_pairs(original_root: Path):
    for ported in sorted(set(list(ROOT.rglob('*.py')) + list(ROOT.rglob('*.pyw')))):
        rel = ported.relative_to(ROOT)
        if SKIP_PARTS.intersection(rel.parts) or rel == Path('tools/restore_literals.py'):
            continue
        original = original_root / rel
        if original.exists():
            yield rel, original, ported


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--translations', type=Path)
    parser.add_argument('--only', nargs='*', help='limit to these relative paths')
    parser.add_argument('--scope', choices=('logic', 'gui', 'all'), default='logic',
                        help='logic (default) skips display-only GUI modules')
    parser.add_argument('--accept', nargs='*', default=[], metavar='PATH:LINE',
                        help='review items checked by hand to apply as proposed')
    args = parser.parse_args(argv)
    accepted: dict[str, set[int]] = {}
    for item in args.accept:
        path, _, line = item.rpartition(':')
        accepted.setdefault(path, set()).add(int(line))
    translations = json.loads(args.translations.read_text(encoding='utf-8')) if args.translations else {}
    report = {}
    totals = {'restore': 0, 'ui_log': 0, 'vocabulary': 0, 'review': 0, 'translated': 0, 'unaligned_cjk_strings': 0}
    for rel, original, ported in iter_pairs(args.original.resolve()):
        key = str(rel).replace('\\', '/')
        if args.only and key not in args.only:
            continue
        is_gui = bool(GUI_MODULES.match(key))
        if (args.scope == 'logic' and is_gui) or (args.scope == 'gui' and not is_gui):
            continue
        raw = ported.read_bytes().decode('utf-8')
        # Analyse on LF text so offsets match tokenize, then restore the file's own line endings.
        crlf = '\r\n' in raw
        port_src = raw.replace('\r\n', '\n')
        orig_src = original.read_bytes().decode('utf-8').replace('\r\n', '\n')
        try:
            result = analyse(orig_src, port_src)
        except (SyntaxError, tokenize.TokenError) as exc:
            report[key] = {'error': str(exc)}
            continue
        if not result['actions'] and not result['unaligned_cjk_strings']:
            continue
        report[key] = {'unaligned_cjk_strings': result['unaligned_cjk_strings'],
                       'actions': [{k: v for k, v in a.items() if k not in ('start', 'end')} for a in result['actions']]}
        totals['unaligned_cjk_strings'] += result['unaligned_cjk_strings']
        for act in result['actions']:
            totals[act['kind']] += 1
        if args.apply and result['actions']:
            fixed, n = apply_actions(port_src, result['actions'], translations, accepted.get(key, set()))
            totals['translated'] += n
            compile(fixed, key, 'exec')  # never write a file that no longer parses
            ported.write_bytes((fixed.replace('\n', '\r\n') if crlf else fixed).encode('utf-8'))
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'files': len(report), **totals}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
