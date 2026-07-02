import json
from pathlib import Path
from graphify.extract import collect_files, extract

root = Path('/home/adam/repos/loopskill-api/app')
files = [p for p in collect_files(root) if p.suffix == '.py' and '__pycache__' not in str(p)]
print(f"collected {len(files)} py files")
result = extract(files)
Path('/home/adam/repos/loopskill-api/.graphify_ast.json').write_text(json.dumps(result))
print(f"AST: {len(result['nodes'])} nodes, {len(result['edges'])} edges")

# quick god-node peek: nodes by in-degree
from collections import Counter
indeg = Counter()
for e in result['edges']:
    indeg[e.get('target')] += 1
print("top 15 by in-degree:")
for nid, c in indeg.most_common(15):
    lbl = next((n.get('label') or n.get('name') or nid for n in result['nodes'] if n.get('id')==nid), nid)
    print(f"  {c:4d}  {lbl}")
