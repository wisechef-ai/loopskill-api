#!/bin/bash
set -euo pipefail

echo "=== Fetch GitHub Stats Skill ==="
echo "SANDBOX=$SANDBOX"
echo "SKILL_HOME=$SKILL_HOME"
echo "PWD=$(pwd)"

# Should be able to reach api.github.com
echo ""
echo "--- Fetching GitHub API (allowed domain) ---"
HTTP_CODE=$(curl -s -o /tmp/github-response.json -w "%{http_code}" --connect-timeout 5 https://api.github.com 2>&1 || echo "curl_failed")
echo "HTTP status: $HTTP_CODE"

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ] || [ "$HTTP_CODE" = "302" ]; then
    echo "PASS: GitHub API reachable"
    cat /tmp/github-response.json | head -c 200
    echo ""
else
    echo "WARN: GitHub API returned $HTTP_CODE (may be rate-limited)"
fi

# Should NOT be able to reach other domains
echo ""
echo "--- Testing blocked domain (should fail) ---"
if curl -s --connect-timeout 3 https://evil.example.com 2>&1; then
    echo "FAIL: Should not be able to reach arbitrary domains!"
    exit 1
else
    echo "PASS: Arbitrary domain blocked"
fi

echo ""
echo "=== Skill Complete ==="
exit 0
