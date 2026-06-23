#!/bin/bash
echo "Hello from sandbox!"
echo "SANDBOX=$SANDBOX"
echo "SKILL_HOME=$SKILL_HOME"
echo "WHOAMI=$(whoami)"
echo "PWD=$(pwd)"
echo "Network test (should fail):"
curl -s --connect-timeout 2 https://example.com 2>&1 && echo "ERROR: Network should be blocked!" || echo "PASS: Network blocked as expected"
echo "Write test:"
echo "test-write" > /tmp/sandbox-write-test.txt && echo "PASS: Can write to /tmp" || echo "FAIL: Cannot write to /tmp"
cat /tmp/sandbox-write-test.txt
exit 0
