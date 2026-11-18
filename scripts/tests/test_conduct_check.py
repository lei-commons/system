import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import conduct_check as cc  # noqa: E402

BAD = {
    "no-email-send": "import smtplib\n",
    "no-messaging": "from twilio.rest import Client\n",
    "no-login-automation": "page.fill('#password', pw)\n",
    "no-headless-browser": "from playwright.sync_api import sync_playwright\n",
    "no-captcha-bypass": "solver = TwoCaptcha(key)  # 2captcha\n",
    "no-intrusion-tools": "subprocess.run(['nmap', host])\n",
    "no-tls-bypass": "requests.get(url, verify=False)\n",
    "no-robots-bypass": "ROBOTSTXT_OBEY = False\n",
    "no-ua-spoofing": "UA = 'Mozilla/5.0 (Windows NT 10.0)'\n",
    "no-proxy-rotation": "use_rotating_proxies(pool)\n",
    "no-private-key": "-----BEGIN RSA PRIVATE KEY-----\n",
    "no-cloud-key": "KEY = 'AKIAABCDEFGHIJKLMNOP'\n",
}


@pytest.mark.parametrize("rule,code", BAD.items())
def test_each_rule_fires(tmp_path, rule, code):
    (tmp_path / "agent.py").write_text(code)
    ids = {f.rule for f in cc.scan(tmp_path)}
    assert rule in ids


def test_clean_code_passes(tmp_path):
    (tmp_path / "ok.py").write_text(
        "import httpx\n"
        "UA = 'lei-commons-crawler/0.1 (+https://github.com/lei-commons)'\n"
        "r = httpx.get(url, headers={'User-Agent': UA})\n"
    )
    assert cc.scan(tmp_path) == []
    assert cc.main(["--root", str(tmp_path)]) == 0


def test_fields_guard(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fields.yaml").write_text(
        "forbidden: [person.phone]\nperson_role:\n  - {name: phone}\n"
    )
    ids = {f.rule for f in cc.scan(tmp_path)}
    assert {"fields-forbidden-removed", "fields-forbidden-allowed"} <= ids
    assert cc.main(["--root", str(tmp_path)]) == 1


def test_repo_itself_is_clean():
    root = Path(__file__).resolve().parents[2]
    findings = cc.scan(root)
    assert findings == [], "\n".join(f"{f.path}:{f.line} {f.rule}" for f in findings)


def test_the_browser_exception_is_limited_to_the_browser_download_packages(tmp_path):
    """Owner decision 2026-11-18: only lc_browser and ingest_browser may drive a browser."""
    for rel in ("packages/lc_browser/lc_browser/runner.py", "services/ingest_browser/ingest_browser/check_chromium.py",
                "agents/harness/tools.py", "packages/lc_ingest/lc_ingest/crawl.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("from playwright.sync_api import sync_playwright\n")
    found = {(f.path, f.rule) for f in cc.scan(tmp_path)}
    assert ("agents/harness/tools.py", "no-headless-browser") in found
    assert ("packages/lc_ingest/lc_ingest/crawl.py", "no-headless-browser") in found
    assert not any(p.startswith(("packages/lc_browser/", "services/ingest_browser/")) for p, _ in found)


def test_the_browser_exception_never_covers_other_rules(tmp_path):
    path = tmp_path / "packages/lc_browser/lc_browser/x.py"
    path.parent.mkdir(parents=True)
    path.write_text("import twilio\nsolver = capsolver\nverify = False\nua = 'Mozilla/5.0 (X11)'\n")
    rules = {f.rule for f in cc.scan(tmp_path)}
    assert {"no-messaging", "no-captcha-bypass", "no-tls-bypass", "no-ua-spoofing"} <= rules
