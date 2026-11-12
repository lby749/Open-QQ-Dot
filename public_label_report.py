"""Write build/persona-label-report.html: 9B style tags of public units, with the vector cross-check.

Run:  .venv\\Scripts\\python.exe public_label_report.py   (opens the page in the default browser)"""
from collections import Counter
import html
import json
from pathlib import Path
import random
import sqlite3
import webbrowser

ROOT = Path(__file__).resolve().parent
LABELS = ROOT / 'data' / 'datasets' / 'persona_labels.sqlite3'
ANNOTATIONS = ROOT / 'data' / 'datasets' / 'annotations.sqlite3'
OUTPUT = ROOT / 'build' / 'persona-label-report.html'
PER_TAG = 4


def main():
    labels = sqlite3.connect(LABELS.as_uri() + '?mode=ro', uri=True)
    rows = labels.execute('SELECT l.data_source, l.conversation_id, l.speaker_id, l.tags_json, l.usable, '
                          'c.knn_tags_json, c.agree FROM unit_labels l LEFT JOIN unit_checks c '
                          'USING(data_source, conversation_id, speaker_id)').fetchall()
    labels.close()
    source = sqlite3.connect(ANNOTATIONS.as_uri() + '?mode=ro', uri=True)
    lines = lambda s, c, p: [r[0] for r in source.execute(
        'SELECT text FROM annotations WHERE data_source=? AND conversation_id=? AND speaker_id=? '
        'AND length(text) BETWEEN 2 AND 80 ORDER BY message_seq LIMIT 8', (s, c, p))]
    tag_count, tag_agree = Counter(), Counter()
    for *_, tags, _, _, agree in rows:
        for tag in json.loads(tags):
            tag_count[tag] += 1
            tag_agree[tag] += agree == 1
    agree = Counter(r[6] for r in rows)
    out = ['<!doctype html><meta charset="utf-8"><title>公开数据风格标签</title><style>'
           'body{font:14px "Microsoft YaHei UI",sans-serif;margin:24px;background:#FFF5EB;color:#47362E;max-width:1100px}'
           'table{border-collapse:collapse;margin:8px 0 20px}td,th{border:1px solid #E8CBB7;padding:5px 9px;vertical-align:top}'
           'th{background:#F8E7D8}.tag{display:inline-block;background:#F0C2A3;border-radius:9px;padding:1px 8px;margin:1px}'
           '.knn{background:#DCE8DF}.bad{color:#C7856B;font-weight:bold}.ok{color:#648F78}</style>',
           '<h1>公开数据风格标签（9B 补标 + 向量核对）</h1>',
           f'<p>对话单位（一段对话里一个角色说的话，至少 3 句）共 <b>{len(rows)}</b> 个；'
           f'9B 判为适合当示例 <b>{sum(r[4] for r in rows)}</b> 个。向量核对：一致 <b class="ok">{agree[1]}</b>，'
           f'待复核 <b class="bad">{agree[0]}</b>，无向量未核对 {agree[None]}。</p>',
           '<h2>标签分布</h2><table><tr><th>标签</th><th>单位数</th><th>其中向量一致</th></tr>']
    out += [f'<tr><td>{t}</td><td>{n}</td><td>{tag_agree[t] / n:.0%}</td></tr>' for t, n in tag_count.most_common()]
    out.append('</table><h2>每个标签随机抽样</h2><p>橙色 = 9B 标签；绿色 = 向量近邻投票的前 3 个标签。</p>')
    rng = random.Random(1)
    for tag, _ in tag_count.most_common():
        picked = rng.sample([r for r in rows if tag in json.loads(r[3])], PER_TAG)
        out.append(f'<h3>{tag}</h3><table><tr><th>来源</th><th>角色</th><th>台词（最多 8 句）</th>'
                   '<th>9B 标签</th><th>向量投票</th><th>核对</th></tr>')
        for s, c, p, tags, usable, knn, ok in picked:
            text = '<br>'.join(html.escape(x) for x in lines(s, c, p))
            mark = {1: '<span class="ok">一致</span>', 0: '<span class="bad">待复核</span>'}.get(ok, '无向量')
            knn_tags = ''.join(f"<span class='tag knn'>{t}</span>" for t in json.loads(knn or '[]'))
            out.append(f'<tr><td>{html.escape(s)}<br>{html.escape(c)}</td><td>{html.escape(p)}</td><td>{text}</td>'
                       f'<td>{"".join(f"<span class=tag>{t}</span>" for t in json.loads(tags))}</td>'
                       f'<td>{knn_tags}</td>'
                       f'<td>{mark}{"" if usable else "<br>不适合当示例"}</td></tr>')
        out.append('</table>')
    source.close()
    OUTPUT.write_text('\n'.join(out), 'utf-8')
    print('已生成', OUTPUT)
    webbrowser.open(OUTPUT.as_uri())


if __name__ == '__main__':
    main()
