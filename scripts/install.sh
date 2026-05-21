#!/usr/bin/env bash
# Install stepflow from the repository and register CLI commands.
#
# Usage:
#   scripts/install.sh                     # install from current repo
#   curl -sSL https://.../install.sh | bash  # clone + install (future)
#
# Registers these commands in ~/.local/bin/:
#   stepflow-lint     — validate pipeline YAML files
#   stepflow-run      — interactive pipeline runner
#   stepflow-convert  — skill description → pipeline YAML

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"

echo "=== Stepflow Install ==="
echo "Repo:  $REPO_DIR"
echo "Bin:   $BIN_DIR"
echo ""

# 1. Install the Python package (editable)
echo "→ Installing stepflow package..."
if pip install -e "$REPO_DIR" 2>/dev/null; then
    echo "  ✓ stepflow installed"
elif pip install -e "$REPO_DIR" --break-system-packages 2>/dev/null; then
    echo "  ✓ stepflow installed (--break-system-packages)"
else
    echo "  ⚠ pip install failed — try manually:"
    echo "    pip install -e \"$REPO_DIR\""
    echo "    (or use --break-system-packages, pipx, or a venv)"
fi

# 2. Create bin directory
mkdir -p "$BIN_DIR"

# 3. Create wrapper scripts
cat > "$BIN_DIR/stepflow-lint" << SCRIPT
#!/usr/bin/env bash
exec python3 "${REPO_DIR}/src/stepflow/plugins/linter/cli.py" "\$@"
SCRIPT
chmod +x "$BIN_DIR/stepflow-lint"
echo "  ✓ stepflow-lint → $BIN_DIR/stepflow-lint"

cat > "$BIN_DIR/stepflow-run" << SCRIPT
#!/usr/bin/env bash
exec python3 "${REPO_DIR}/scripts/skill_repl.py" "\$@"
SCRIPT
chmod +x "$BIN_DIR/stepflow-run"
echo "  ✓ stepflow-run     → $BIN_DIR/stepflow-run"

cat > "$BIN_DIR/stepflow-convert" << SCRIPT
#!/usr/bin/env bash
exec python3 "${REPO_DIR}/scripts/skill_convert.py" "\$@"
SCRIPT
chmod +x "$BIN_DIR/stepflow-convert"
echo "  ✓ stepflow-convert → $BIN_DIR/stepflow-convert"

# 4. Check PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "⚠  Add $BIN_DIR to your PATH:"
    echo ""
    echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    echo "    source ~/.bashrc"
    echo ""
else
    echo ""
    echo "✓ $BIN_DIR is already in PATH"
fi

echo "=== Done ==="
echo ""
echo "Try: stepflow-lint --help"
echo "     stepflow-run <graph.yaml>"
echo "     stepflow-convert <description.md> -o pipeline.yaml"
