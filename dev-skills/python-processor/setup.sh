#!/bin/bash
set -euo pipefail

echo "=== Python Processor Skill ==="
echo "SANDBOX=$SANDBOX"
echo "SKILL_HOME=$SKILL_HOME"

mkdir -p "$SKILL_HOME/output"

# Run Python data processing
python3 -c "
import json
import os

print('Python version:', __import__('sys').version)
print('CWD:', os.getcwd())

# Process some data
data = {'count': 42, 'items': ['a', 'b', 'c'], 'sandbox': os.environ.get('SANDBOX', 'unknown')}
output_path = os.path.join(os.environ.get('SKILL_HOME', '/tmp'), 'output', 'result.json')
with open(output_path, 'w') as f:
    json.dump(data, f, indent=2)
print(f'Wrote output to {output_path}')

# Verify /tmp is writable
with open('/tmp/test.txt', 'w') as f:
    f.write('tmp write ok')
print('/tmp write: OK')
"

# Verify output
echo ""
echo "--- Output verification ---"
cat "$SKILL_HOME/output/result.json"
echo ""

# Network should be blocked
echo ""
echo "--- Network test (should fail) ---"
if python3 -c "
import urllib.request
try:
    urllib.request.urlopen('https://example.com', timeout=3)
    print('FAIL: Network should be blocked!')
except Exception as e:
    print(f'PASS: Network blocked ({type(e).__name__})')
"; then
    exit 0
else
    exit 0
fi
