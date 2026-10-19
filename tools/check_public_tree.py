"""Check candidate/tracked files without echoing sensitive matched values.
This targeted check complements manual review; it is not a complete secret scanner.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"data", "secrets", "certs", "chat-records", ".release-local", "node_modules", ".venv"}
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider-key": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{24,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    "personal-windows-path": re.compile(r"[A-Za-z]:[\\/]Users[\\/](?!Public|Default|YOUR_USER|<)[^\\/\s]+", re.I),
    "live-tunnel": re.compile(r"https?://[a-f0-9]{8,}\.r[0-9]+\.cpolar\.(?:cn|top)"),
}

def main():
    proc = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True)
    files = sorted(set(x for x in proc.stdout.decode("utf-8").split("\0") if x))
    issues = []
    for name in files:
        rel = Path(name)
        if any(part in FORBIDDEN for part in rel.parts) or (rel.name.startswith(".env") and rel.name != ".env.example") or rel.suffix in {".key", ".pfx", ".p12", ".bak", ".sql", ".db", ".sqlite"}:
            issues.append((name, "forbidden-file"))
        file = ROOT / rel
        if not file.is_file(): continue
        try: text = file.read_text(encoding="utf-8")
        except UnicodeError:
            issues.append((name, "binary-requires-review")); continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text): issues.append((name, label))
    for name, label in issues: print(f"{label}: {name}")
    print(f"Checked {len(files)} files; {len(issues)} findings. Matched values are never printed.")
    return bool(issues)

if __name__ == "__main__": sys.exit(main())
