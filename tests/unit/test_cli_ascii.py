"""Verify CLI files contain no non-ASCII characters that could crash on Windows."""
from pathlib import Path


CLI_DIR = Path(__file__).resolve().parent.parent.parent / "agenttrader" / "cli"


def test_no_non_ascii_in_cli_output():
    """All CLI .py files should use only ASCII characters."""
    violations = []
    for py_file in sorted(CLI_DIR.glob("*.py")):
        content = py_file.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for ch in line:
                if ord(ch) > 127:
                    violations.append(f"{py_file.name}:{i}: U+{ord(ch):04X} '{ch}'")
                    break
    assert violations == [], f"Non-ASCII characters found:\n" + "\n".join(violations)
