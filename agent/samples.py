"""Refresh samples/: sanitized copies of the latest JSON artifacts for "Load last results".

Run from the project root:  python -m agent.samples

The GCP project ID is replaced with a placeholder, and the copies are scanned for secret
.env values (API keys, passwords, tokens), the project ID, the home directory and
Google API key patterns. If anything is found, samples/ is left unchanged.
"""
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

from agent.compare import FILES, ROOT, SAMPLE_NAMES, SAMPLES_DIR

PROJECT_PLACEHOLDER = "your-gcp-project"
SECRET_KEY_WORDS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
LEAK_PATTERNS = [re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"-----BEGIN [A-Z ]+-----")]


def forbidden_values(env: dict) -> dict[str, str]:
    """name -> value that must never appear in a sample file."""
    out = {k: v for k, v in env.items()
           if v and any(w in k.upper() for w in SECRET_KEY_WORDS)}
    # a default password equal to the public user name (e.g. Docker's postgres/postgres)
    # is not a secret, and it also appears as the database name in profile.json
    if out.get("PG_PASSWORD") and out["PG_PASSWORD"] == env.get("PG_USER"):
        out.pop("PG_PASSWORD")
    if env.get("GCP_PROJECT"):
        out["GCP_PROJECT"] = env["GCP_PROJECT"]
    out["home directory"] = str(Path.home())
    return out


def sanitize(text: str, env: dict) -> str:
    project = env.get("GCP_PROJECT")
    return text.replace(project, PROJECT_PLACEHOLDER) if project else text


def leaks(text: str, forbidden: dict[str, str]) -> list[str]:
    """Names of forbidden values / patterns found in text (values are never printed)."""
    found = [name for name, value in forbidden.items() if value in text]
    found += [p.pattern for p in LEAK_PATTERNS if p.search(text)]
    return found


def build_samples() -> dict[str, str]:
    env = dotenv_values(ROOT / ".env")
    forbidden = forbidden_values(env)
    out, problems = {}, []
    for name in SAMPLE_NAMES:
        src = FILES[name]
        if not src.exists():
            problems.append(f"{src.name}: missing (run the pipeline first)")
            continue
        text = sanitize(src.read_text(encoding="utf-8"), env)
        found = leaks(text, forbidden)
        if found:
            problems.append(f"{src.name}: contains {', '.join(found)}")
        out[src.name] = text
    if problems:
        raise SystemExit("samples/ not updated:\n- " + "\n- ".join(problems))
    return out


def main() -> None:
    files = build_samples()
    SAMPLES_DIR.mkdir(exist_ok=True)
    for name, text in files.items():
        (SAMPLES_DIR / name).write_text(text, encoding="utf-8")
        print(f"samples/{name}: {len(text):,} bytes")
    print(f"GCP project ID replaced with '{PROJECT_PLACEHOLDER}'; no secrets found.")


if __name__ == "__main__":
    sys.exit(main())
