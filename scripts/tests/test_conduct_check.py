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
