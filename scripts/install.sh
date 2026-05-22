#!/usr/bin/env bash
# Install skillflow in a venv and register CLI commands.
#
# Usage:
#   scripts/install.sh                     # install from current repo
#   curl -sSL https://.../install.sh | bash  # clone + install (future)
#
# Creates a venv at ~/.local/share/skillflow/venv/ and registers these
# commands in ~/.local/bin/:
#   skillflow-lint     — validate pipeline YAML files
#   skillflow-run      — stateless pipeline runner (agent calls via CLI)
#   skillflow-convert  — skill description → pipeline YAML

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${HOME}/.local/share/skillflow/venv"
BIN_DIR="${HOME}/.local/bin"

echo "=== SkillFlow Install ==="
echo "Repo:  $REPO_DIR"
echo "Venv:  $VENV_DIR"
echo "Bin:   $BIN_DIR"
echo ""

# 1. Create venv (idempotent)
if [ -d "$VENV_DIR" ]; then
    echo "→ Venv already exists, upgrading..."
else
    echo "→ Creating venv..."
    python3 -m venv "$VENV_DIR"
fi

# 2. Install skillflow into the venv
echo "→ Installing skillflow..."
"$VENV_DIR/bin/pip" install -e "$REPO_DIR" --quiet
echo "  ✓ skillflow installed"

# 3. Create bin directory
mkdir -p "$BIN_DIR"

# 4. Create wrapper scripts pointing at the venv
cat > "$BIN_DIR/skillflow-lint" << SCRIPT
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/python3" "${REPO_DIR}/src/skillflow/plugins/linter/cli.py" "\$@"
SCRIPT
chmod +x "$BIN_DIR/skillflow-lint"
echo "  ✓ skillflow-lint     → $BIN_DIR/skillflow-lint"

cat > "$BIN_DIR/skillflow-run" << SCRIPT
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/python3" "${REPO_DIR}/scripts/skill_run.py" "\$@"
SCRIPT
chmod +x "$BIN_DIR/skillflow-run"
echo "  ✓ skillflow-run      → $BIN_DIR/skillflow-run"

cat > "$BIN_DIR/skillflow-convert" << SCRIPT
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/python3" "${REPO_DIR}/scripts/skill_convert.py" "\$@"
SCRIPT
chmod +x "$BIN_DIR/skillflow-convert"
echo "  ✓ skillflow-convert  → $BIN_DIR/skillflow-convert"

# 5. Check PATH
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
echo "Try: skillflow-lint --help"
echo "     skillflow-run --graph pipeline.yaml --action next"
echo "     skillflow-convert <description.md> -o pipeline.yaml"
