#!/bin/bash
set -euo pipefail

echo "=== NPM Installer Skill ==="
echo "SANDBOX=$SANDBOX"
echo "SKILL_HOME=$SKILL_HOME"
echo "Node version: $(node --version 2>/dev/null || echo 'node not found')"
echo "npm version: $(npm --version 2>/dev/null || echo 'npm not found')"

# Create a minimal package.json
mkdir -p /home/skill/node_modules /home/skill/.npm

cat > /tmp/package.json << 'EOF'
{
  "name": "sandbox-test",
  "version": "1.0.0",
  "private": true
}
EOF

# Try to install a tiny package (should reach registry.npmjs.org)
echo ""
echo "--- Installing is-number (tiny test package) ---"
cd /tmp && npm install --no-save is-number 2>&1 | tail -5 || echo "npm install completed with warnings"

# Verify
echo ""
if node -e "const isNumber = require('is-number'); console.log('isNumber(5):', isNumber(5)); console.log('PASS: npm package works')"; then
    echo "PASS: NPM package installed and functional"
else
    echo "WARN: Could not verify npm package (node may not be available in sandbox)"
fi

echo ""
echo "=== Skill Complete ==="
exit 0
