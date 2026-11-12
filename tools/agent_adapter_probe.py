"""Read-only installation snapshot for supported native agent tools."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_tool_adapters import available_adapters

if __name__ == '__main__':
    print(json.dumps({'adapters': available_adapters(),
                      'note':'Executable discovery only; not a login check or live model test.'},
                     ensure_ascii=False,indent=2))
