#!/usr/bin/env bash
set -euo pipefail

# Install Ghidra, GoReSym, and uv without root privileges.
# Ghidra  -> ./ghidra/
# GoReSym -> ./goresym/ (symlinked to ~/bin/goresym)
# uv      -> ~/.local/bin/uv (via official installer)

GHIDRA_VERSION="12.0.4"
GHIDRA_DATE="20260303"
GHIDRA_URL="https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_${GHIDRA_VERSION}_build/ghidra_${GHIDRA_VERSION}_PUBLIC_${GHIDRA_DATE}.zip"

GORESYM_VERSION="3.3"
GORESYM_URL="https://github.com/mandiant/GoReSym/releases/download/v${GORESYM_VERSION}/GoReSym-linux.zip"

echo "=== Installing Ghidra ${GHIDRA_VERSION} ==="
curl -fSL -o ghidra.zip "$GHIDRA_URL"
unzip -q ghidra.zip
rm ghidra.zip
mv ghidra_${GHIDRA_VERSION}_PUBLIC ghidra
echo "Ghidra installed to $(pwd)/ghidra"

echo ""
echo "=== Installing GoReSym ${GORESYM_VERSION} ==="
mkdir -p goresym
curl -fSL -o goresym.zip "$GORESYM_URL"
unzip -q -o goresym.zip -d goresym
rm goresym.zip
chmod +x goresym/GoReSym
mkdir -p ~/bin
ln -sf "$(pwd)/goresym/GoReSym" ~/bin/goresym
echo "GoReSym installed, symlinked to ~/bin/goresym"

echo ""
echo "=== Installing uv ==="
curl -LsSf https://astral.sh/uv/install.sh | sh

echo ""
echo "=== Done ==="
echo "Add to your shell profile:"
echo 'export PATH=\"$PATH:$HOME/bin\"'
echo 'export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64/'
echo 'export PATH=$JAVA_HOME/bin:$PATH'
