#!/bin/bash
set -euo pipefail

echo "=== File Transformer Skill ==="
echo "SANDBOX=$SANDBOX"
echo "SKILL_HOME=$SKILL_HOME"

mkdir -p /home/skill/output

# Create test input data
cat > /tmp/input.csv << 'EOF'
name,score,level
alice,95,expert
bob,72,intermediate
charlie,88,advanced
diana,65,beginner
eve,91,expert
EOF

echo "Input:"
cat /tmp/input.csv

# Transform: filter scores >= 80, sort descending
echo ""
echo "--- Processing ---"
awk -F',' 'NR>1 && $2 >= 80 {print $1, $2, $3}' /tmp/input.csv | sort -t' ' -k2 -rn > /home/skill/output/high-scorers.txt

echo "High scorers (score >= 80):"
cat /home/skill/output/high-scorers.txt

# Count lines
LINE_COUNT=$(wc -l < /home/skill/output/high-scorers.txt)
echo ""
echo "Lines in output: $LINE_COUNT"

# Verify write works
echo "test-verify" > /tmp/verify-write.txt
if [ "$(cat /tmp/verify-write.txt)" = "test-verify" ]; then
    echo "PASS: /tmp write verified"
else
    echo "FAIL: /tmp write check failed"
    exit 1
fi

# Network must be blocked
echo ""
echo "--- Network test (should fail) ---"
if curl -s --connect-timeout 2 https://example.com 2>&1; then
    echo "FAIL: Network should be blocked!"
    exit 1
else
    echo "PASS: Network blocked as expected"
fi

echo ""
echo "=== Skill Complete ==="
exit 0
