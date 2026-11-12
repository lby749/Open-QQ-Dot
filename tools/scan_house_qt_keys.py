import glob
import re

for fn in glob.glob('house_qt/*.py'):
    with open(fn, 'r', encoding='utf-8') as f:
        text = f.read()
    for idx, line in enumerate(text.splitlines(), 1):
        matches = re.findall(r'(?:buttons|actions)\[([\'"][^\'"]+[\'"])\]', line)
        for key in matches:
            if any('\u4e00' <= ch <= '\u9fff' for ch in key):
                print(f"{fn}:{idx}: {ascii(key)}")
