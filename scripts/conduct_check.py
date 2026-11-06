#!/usr/bin/env python3
"""conduct-policy check (constitution Article 2 and 3).

Runs on every pull request in every lei-commons repo, as a required status
check. It is a static backstop: the egress proxy and runner sandbox are what
actually stop prohibited traffic. This check catches code that tries anyway.

Exit codes: 0 clean, 1 violations found, 2 usage error.

Usage:
  conduct_check.py [--root DIR] [--base REF] [--json]

  --base REF  also report protected paths changed since REF (for the guardian)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

CODE_SUFFIXES = {".py", ".sh", ".js", ".ts", ".mjs", ".tf", ".yaml", ".yml", ".toml", ".json", ".dockerfile", ""}
SKIP_DIRS = {".git", ".pytest_cache", ".ruff_cache", "node_modules", ".venv", "venv", "__pycache__", ".terraform", "dist", "build"}
# The checker and its own tests necessarily contain the patterns they look for.
SELF = {"scripts/conduct_check.py", "scripts/tests/test_conduct_check.py"}

PROTECTED = [
    "constitution.md",
    "config/fields.yaml",
    "config/labels.yaml",
    "config/models.yaml",
    "config/egress-allowlist.yaml",
    "scripts/conduct_check.py",
    ".github/",
    "infra/",
    "bootstrap/",
    "packages/lc_core/lc_core/control.py",
    "packages/lc_core/lc_core/events.py",
]

# Article 3.2: these must always stay in fields.yaml `forbidden`.
REQUIRED_FORBIDDEN = {"person.phone", "person.email", "person.address", "person.residentialAddress"}


@dataclass
class Rule:
    id: str
    article: str
    pattern: re.Pattern
    message: str


def R(id_, article, pattern, message, flags=re.IGNORECASE):
    return Rule(id_, article, re.compile(pattern, flags), message)


RULES = [
    # 2.5 no spam or unsolicited contact: no sending capability at all.
    R("no-email-send", "2.5", r"\b(import\s+smtplib|from\s+smtplib|sendgrid|mailgun|postmark|amazon\s*ses|ses\.send_email|gmail\.users\.messages\.send)\b",
      "Sending email is not allowed (Article 2.5)."),
    R("no-messaging", "2.5", r"\b(twilio|whatsapp|telegram\.bot|slack_sdk|discord\.py|tweepy|messagebird|vonage)\b",
      "Messaging and social posting are not allowed (Article 2.5)."),
    # 2.1/2.2 no logins, no access-controlled pages.
    R("no-login-automation", "2.2", r"(\.fill\([^)]*(password|passwd|pwd)|send_keys\([^)]*(password|passwd)|login_form|submit_login|\bmechanize\b|\brobobrowser\b)",
      "Automated login is not allowed (Article 2.2)."),
    R("no-headless-browser", "2.1", r"\b(from\s+selenium|import\s+selenium|from\s+playwright|import\s+playwright|puppeteer|undetected_chromedriver)\b",
      "Headless browsers are not allowed in agents; the crawler fetches public pages over HTTP (Article 2.1)."),
    R("no-captcha-bypass", "2.1", r"\b(2captcha|anti-?captcha|capsolver|deathbycaptcha|captcha_?solver|hcaptcha_?solve)\b",
      "CAPTCHA solving is bypassing an access control (Article 2.1)."),
    R("no-cookie-replay", "2.2", r"(browser_cookie3|cookiejar\.load|load_cookies\(|session\.cookies\.update)",
      "Replaying cookies or sessions is logging in by other means (Article 2.2)."),
    # 2.3 no intrusion.
    R("no-intrusion-tools", "2.3", r"\b(nmap|masscan|sqlmap|hydra|metasploit|nikto|gobuster|dirbuster|wfuzz|ffuf|zmap|shodan)\b",
      "Scanning and exploitation tools are not allowed (Article 2.3)."),
    R("no-tls-bypass", "2.3", r"(verify\s*=\s*False|CURL_SSL_VERIFYPEER\s*,\s*0|--insecure\b|\bcurl\s+-k\b|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0)",
      "Disabling TLS verification is not allowed."),
    # 2.4 no harm to others' systems.
    R("no-robots-bypass", "2.4", r"(ROBOTSTXT_OBEY\s*=\s*False|ignore[_-]?robots|robots_?txt\s*=\s*False|obey_robots\s*=\s*False)",
      "robots.txt must be honoured (Article 2.4)."),
    R("no-ua-spoofing", "2.4", r"['\"]Mozilla/5\.0",
      "Do not impersonate browsers; use the lei-commons crawler user agent (Article 2.4)."),
    R("no-proxy-rotation", "2.4", r"(?<![A-Za-z])(rotating[_-]?prox|proxy[_-]?pool|residential[_-]?prox|brightdata|oxylabs|smartproxy|scraperapi|zyte[_-]?proxy)",
      "Proxy rotation evades rate limits and blocks (Article 2.4)."),
    # Secrets never in code; they live in Secret Manager.
    R("no-private-key", "secrets", r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----",
      "Private key in repository.", flags=0),
    R("no-cloud-key", "secrets", r"(AIza[0-9A-Za-z_\-]{35}|AKIA[0-9A-Z]{16}|ghp_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{40,}|sk-ant-[0-9A-Za-z\-_]{20,}|\"private_key_id\"\s*:)",
      "Credential in repository.", flags=0),
]


@dataclass
class Finding:
    rule: str
    article: str
    path: str
    line: int
    text: str
    message: str


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        rel = p.relative_to(root).as_posix()
        if rel in SELF:
            continue
        suffix = p.suffix.lower() if p.name != "Dockerfile" else ".dockerfile"
        if suffix in CODE_SUFFIXES and p.stat().st_size < 2_000_000:
            yield rel, p


def scan(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel, p in iter_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                if rule.pattern.search(line):
                    findings.append(Finding(rule.id, rule.article, rel, n, line.strip()[:160], rule.message))
    findings += check_fields(root)
    return findings


def check_fields(root: Path) -> list[Finding]:
    path = root / "config" / "fields.yaml"
    if not path.exists():
        return []
    import yaml

    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    forbidden = set(cfg.get("forbidden", []))
    out = []
    missing = REQUIRED_FORBIDDEN - forbidden
    if missing:
        out.append(Finding("fields-forbidden-removed", "3.2", "config/fields.yaml", 0, ", ".join(sorted(missing)),
                           "Personal phone, email and address must stay forbidden (Article 3.2)."))
    allowed = {f"person.{i['name']}" for i in cfg.get("person_role", [])}
    clash = forbidden & allowed
    if clash:
        out.append(Finding("fields-forbidden-allowed", "3.2", "config/fields.yaml", 0, ", ".join(sorted(clash)),
                           "A forbidden field is also allowed (Article 3.2)."))
    return out


def protected_changes(root: Path, base: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", f"{base}...HEAD"],
                         capture_output=True, text=True, check=True).stdout.split()
    return sorted(f for f in out if any(f == p or (p.endswith("/") and f.startswith(p)) for p in PROTECTED))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--base")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    findings = scan(root)
    protected = protected_changes(root, a.base) if a.base else []

    if a.json:
        print(json.dumps({"findings": [asdict(f) for f in findings], "protected_paths_changed": protected}, indent=2))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: [{f.rule}] {f.message}\n    {f.text}")
        if protected:
            print("Protected paths changed (Tier 3 review and 7-day cooldown required):")
            for p in protected:
                print(f"  {p}")
        print(f"conduct-policy: {len(findings)} violation(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
