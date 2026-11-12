"""Bounded, read-only checks on delivered source; never a claim of a full security audit."""
from __future__ import annotations

import ast
from html.parser import HTMLParser
from pathlib import Path
import re


SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.vue', '.svelte'}
OTHER_SOURCE_SUFFIXES = {'.c', '.h', '.cpp', '.hpp', '.go', '.rs', '.java', '.cs', '.php', '.rb',
                         '.sh', '.ps1', '.swift', '.kt', '.dart', '.sql'}
REVIEW_INSTRUCTION = (
    '【交付前自审】完成代码或项目后，重新阅读实际修改及相关调用，检查逻辑缺陷、异常与边界、'
    '输入校验、权限隔离、注入、凭据泄露及敏感数据暴露；补做相关测试并在原授权范围内修复。'
    '网站还需检查页面标题、描述、可抓取链接、索引策略与框架生成的元数据；内部页面的 noindex '
    '可能有意设置，不要擅自开放收录。不要修改无关文件、扩大权限、部署或执行破坏性扫描。'
    '最终用中文交代已修问题、实际验证、未解决问题及未检查范围；没有证据不得声称安全无漏洞。\n\n【本次任务】\n'
)


def review_instruction(category: str) -> str:
    return REVIEW_INSTRUCTION if category in ('code', 'file') else ''


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.in_title, self.description, self.noindex = '', False, False, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title':
            self.in_title = True
        if tag == 'meta':
            name, content = (attrs.get('name') or '').lower(), attrs.get('content') or ''
            if name == 'description' and content.strip():
                self.description = True
            if name in ('robots', 'googlebot') and 'noindex' in content.lower():
                self.noindex = self.getpos()[0]

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def review_sources(workspace: Path, names: list[str]) -> dict:
    """Review changed/target source only, with fixed file/byte limits and no execution or network."""
    root = Path(workspace).resolve()
    report = {'scope': '本次修改和指定目标源码', 'files_checked': [], 'findings': [], 'limitations': [],
              'seo_status': '不适用', 'status': 'not_applicable'}

    def issue(name, line, kind, message):
        if len(report['findings']) < 200:
            report['findings'].append({'file': name, 'line': line, 'kind': kind, 'message': message})
        elif '问题超过 200 项，报告仅保留前 200 项。' not in report['limitations']:
            report['limitations'].append('问题超过 200 项，报告仅保留前 200 项。')

    candidates = sorted({str(name) for name in names if Path(str(name)).suffix.lower() in
                         SOURCE_SUFFIXES | OTHER_SOURCE_SUFFIXES})
    if len(candidates) > 100:
        report['limitations'].append('源码超过 100 个，本轮只检查前 100 个。')
    total = 0
    for name in candidates[:100]:
        path = (root / name).resolve()
        # Never follow a symlink outside the selected project, including in standalone verification.
        if not path.is_relative_to(root):
            report['limitations'].append('有源码路径超出项目，未读取。')
            continue
        relative = path.relative_to(root).as_posix()
        try:
            if not path.is_file():
                report['limitations'].append(f'{relative}：源码已删除或不是文件，未进行内容检查。')
                continue
            size = path.stat().st_size
            if size > 256_000 or total + size > 2_000_000:
                report['limitations'].append(f'{relative}：超过本轮读取上限，未检查。')
                continue
            with path.open('rb') as stream:
                raw = stream.read(256_001)
            if len(raw) > 256_000 or total + len(raw) > 2_000_000:
                report['limitations'].append(f'{relative}：读取时超过上限，未检查。')
                continue
            source = raw.decode('utf-8-sig')
        except (OSError, UnicodeError):
            report['limitations'].append(f'{relative}：源码无法读取。')
            continue
        total += len(raw)
        report['files_checked'].append(relative)
        suffix = path.suffix.lower()
        if suffix in OTHER_SOURCE_SUFFIXES:
            report['limitations'].append(f'{relative}：尚无对应语言的语法和安全分析器，需要补充检查。')
        if suffix == '.py':
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                issue(relative, exc.lineno or 1, 'bug', 'Python 语法错误，需修复后重新验证。')
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(isinstance(v, (ast.List, ast.Dict, ast.Set)) for v in
                           node.args.defaults + [v for v in node.args.kw_defaults if v is not None]):
                        issue(relative, node.lineno, 'bug', '可变默认参数会跨调用共享状态，请核对并改用独立对象。')
                if not isinstance(node, ast.Call):
                    continue
                func = ast.unparse(node.func)
                if func in ('eval', 'exec', 'pickle.load', 'pickle.loads', 'marshal.loads'):
                    issue(relative, node.lineno, 'security', '动态执行或反序列化入口，需核对输入信任边界。')
                if func == 'yaml.load' and not any(
                        k.arg == 'Loader' and ast.unparse(k.value) in ('yaml.SafeLoader', 'SafeLoader')
                        for k in node.keywords):
                    issue(relative, node.lineno, 'security', 'YAML 加载器未明确使用安全 Loader。')
                if any(k.arg == 'verify' and isinstance(k.value, ast.Constant) and k.value.value is False
                       for k in node.keywords):
                    issue(relative, node.lineno, 'security', '显式设置 verify=False，请核对是否关闭了证书校验。')
                if func.startswith('subprocess.') and any(
                        k.arg == 'shell' and isinstance(k.value, ast.Constant) and k.value.value is True
                        for k in node.keywords):
                    issue(relative, node.lineno, 'security', '使用 shell 执行命令，需核对参数是否可被外部输入控制。')
        if suffix in ('.js', '.mjs', '.cjs', '.ts', '.jsx', '.tsx', '.vue', '.svelte', '.html', '.htm'):
            for line, text in enumerate(source.splitlines(), 1):
                if re.search(r'\b(?:eval\s*\(|new\s+Function\s*\(|innerHTML\s*=|dangerouslySetInnerHTML\s*=)', text):
                    issue(relative, line, 'security', '发现动态代码或 HTML 注入入口，需确认输入已可信处理。')
        if suffix in ('.html', '.htm') and re.search(r'<(?:html|head)\b', source, re.I):
            page = _Page()
            page.feed(source)
            report['seo_status'] = '已检查静态页面；未检查线上响应、渲染与收录'
            if not page.title.strip():
                issue(relative, 1, 'seo', '静态页面缺少有效标题；若由框架生成，需验证实际渲染结果。')
            if not page.description:
                issue(relative, 1, 'seo', '静态页面缺少描述；需核对是否应收录及实际生成的元数据。')
            if page.noindex:
                issue(relative, page.noindex, 'seo', '页面禁止索引；内部或隐私页面可保留，公开页面需核对。')
        elif suffix in ('.tsx', '.jsx', '.vue', '.svelte'):
            report['seo_status'] = '需要验证框架渲染与页面元数据，未作静态合格结论'
            report['limitations'].append(f'{relative}：组件源码不能证明实际页面的 SEO，需补渲染检查。')
        for line, text in enumerate(source.splitlines(), 1):
            if re.search(r'''(?i)\b(?:api_key|api_secret|access_token|password)\s*[:=]\s*["'][^"'\s]{16,}["']''', text):
                issue(relative, line, 'security', '疑似硬编码凭据，请核实并移至安全配置；报告不回显原值。')
    if candidates:
        report['status'] = 'needs_review' if report['findings'] or report['limitations'] else 'checked'
        report['limitations'].append('仅有限静态检查；不覆盖所有语言、依赖漏洞、业务逻辑和线上 SEO。')
    return report


def describe_review(report: dict) -> str:
    if not report or report.get('status') == 'not_applicable':
        return ''
    lines = [f"自动审查：已检查 {len(report['files_checked'])} 个源码文件；发现 {len(report['findings'])} 个待核对项。",
             '检查范围：' + report['scope'], 'SEO：' + report['seo_status']]
    labels = {'bug': '缺陷', 'security': '安全', 'seo': 'SEO'}
    for item in report['findings']:
        lines.append(f"{item['file']}:{item['line']} [{labels[item['kind']]}] {item['message']}")
    lines += report['limitations']
    return '\n'.join(lines)
