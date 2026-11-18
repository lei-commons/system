"""Browser downloads (owner decision 2026-11-18): for official sources whose public data is offered only
through a web page or form (no login, no CAPTCHA, nothing behind an access control), a real headless
browser does what a person would. Every one of its requests goes through the conduct gateway: the
browser's proxy is a relay in the same container (lc_browser.relay) that turns each request into a
gateway /fetch call, so the allow-list, robots.txt, pacing, the fixed address and the honest User-Agent
all apply. Only hosts the owner approved as browser hosts are reachable (config/egress-allowlist.yaml).
"""
