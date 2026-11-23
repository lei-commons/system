import gzip
from pathlib import Path

import pytest
import yaml

from lc_core.fetch import FetchError
from web_claims import commoncrawl, evidence, names, text
from web_claims.settings import SettingsError, load, parse

ROOT = Path(__file__).resolve().parents[3]


def configs():
    return (yaml.safe_load((ROOT / "config" / "web_claims.yaml").read_text()),
            yaml.safe_load((ROOT / "config" / "models.yaml").read_text()))


def test_the_committed_settings_load():
    s = load(ROOT / "config" / "web_claims.yaml", ROOT / "config" / "models.yaml")
    assert s.enabled and s.with_website_count == 10000 and s.without_website_count == 10000
    assert set(s.without_website_groups) == {"US", "SG", "EU", "UK"} and s.with_website_countries == ("FI",)
    assert s.maker.vendor != s.checker.vendor
    assert s.max_model_usd_per_run <= 40  # under models.yaml limits.max_daily_model_usd


def test_maker_and_checker_must_come_from_different_vendors():
    cfg, models = configs()
    models["tasks"]["web_claims"]["checker"] = dict(models["tasks"]["web_claims"]["maker"])
    with pytest.raises(SettingsError):
        parse(cfg, models)


def test_out_of_range_values_stop_the_run():
    cfg, models = configs()
    cfg["run"]["max_model_usd_per_run"] = 41
    with pytest.raises(SettingsError):
        parse(cfg, models)
    cfg, models = configs()
    cfg["candidates"]["without_website"]["groups"]["EU"] = ["Germany"]
    with pytest.raises(SettingsError):
        parse(cfg, models)


def test_candidate_addresses_come_from_the_name_without_its_legal_form():
    s = load(ROOT / "config" / "web_claims.yaml", ROOT / "config" / "models.yaml")
    assert names.candidate_domains("Acme Widgets Limited", "GB", s.country_domains, s.everywhere, 8) == [
        "acmewidgets.co.uk", "acme-widgets.co.uk", "acmewidgets.uk", "acme-widgets.uk", "acmewidgets.com",
        "acme-widgets.com"]
    assert names.candidate_domains("Müller & Söhne GmbH", "DE", s.country_domains, s.everywhere, 3) == [
        "mullerandsohne.de", "muller-and-sohne.de", "mullerand.de"]
    assert names.candidate_domains("Pte Ltd", "SG", s.country_domains, s.everywhere, 8) == []
    assert names.core_words("The Coca-Cola Company") == ["coca", "cola"]


def test_two_letter_legal_forms_count_only_written_as_forms():
    p = names.legal_form_pattern()
    assert p.search("Example Oy") and p.search("EXAMPLE AB") and p.search("Widgets Limited")
    assert not p.search("as we said, co-operation is ad hoc")


def test_evidence_lei_number_and_name_plus_country():
    rec = {"lei": "5493001KJTIIGC8Y1R12", "registrationNumber": "1234567-8", "legalName": "Esimerkki Oy",
           "country": "FI"}
    assert evidence.find(rec, ["LEI 5493001KJTIIGC8Y1R12"], "esimerkki.fi")["kind"] == "lei"
    assert evidence.find(rec, ["Y-tunnus: 1234567-8"], "esimerkki.fi")["kind"] == "registration-number"
    assert evidence.find(rec, ["VAT FI12345678"], "esimerkki.fi")["kind"] == "registration-number"
    assert evidence.find(rec, ["© Esimerkki Oy"], "esimerkki.fi")["kind"] == "legal-name+country"  # .fi names the country
    assert evidence.find(rec, ["© Esimerkki Oy"], "esimerkki.com") is None
    assert evidence.find(rec, ["© Esimerkki Oy, Helsinki"], "esimerkki.com")["kind"] == "legal-name+country"
    assert evidence.find(rec, ["Esimerkki Oyj Group"], "esimerkki.com") is None  # not the exact name
    assert evidence.find(rec, ["order 12345678901"], "esimerkki.com") is None


def test_uk_numbers_with_or_without_leading_zeros():
    rec = {"registrationNumber": "01234567", "legalName": "Acme Widgets Limited", "country": "GB"}
    assert evidence.find(rec, ["Registered in England No. 1234567"], "acme.com")["kind"] == "registration-number"
    assert evidence.find(rec, ["Company number 01234567."], "acme.com")["kind"] == "registration-number"
    assert evidence.find(rec, ["Ref 912345678"], "acme.com") is None


PAGE = b"""<html><head><title>Esimerkki</title><script>var ceo='x';</script></head><body>
<p>Esimerkki Oy is part of the Emo Group Oyj.</p>
<p>CEO Matti Meikalainen</p>
<p>Contact: matti.meikalainen@example.fi</p>
<p>Tel +358 40 123 4567</p>
<p>Y-tunnus 1234567-8</p>
<p>We make things.</p>
</body></html>"""


def test_page_text_and_what_a_model_may_see():
    t = text.html_text(PAGE, "utf-8", 10_000)
    assert "var ceo" not in t and "Matti" in t
    lines = text.minimise([t], names.legal_form_pattern(), 5000).splitlines()
    assert lines == ["Esimerkki Oy is part of the Emo Group Oyj.", "Y-tunnus 1234567-8"]
    assert "Matti" not in "\n".join(lines) and "@" not in "\n".join(lines)


def capture(url, ts="20261101000000", name="x"):
    return commoncrawl.Capture(url, ts, f"crawl-data/CC-MAIN-2026-44/segments/1/warc/{name}.warc.gz", 0, 10)


def test_pages_chosen_home_first_then_legal_pages():
    caps = [capture("https://www.esimerkki.fi/"), capture("https://www.esimerkki.fi/tuotteet"),
            capture("https://www.esimerkki.fi/yhteystiedot"), capture("https://www.esimerkki.fi/impressum"),
            capture("https://shop.esimerkki.fi/about"), capture("https://www.esimerkki.fi/about?x=1"),
            capture("https://www.esimerkki.fi/", ts="20261102000000")]
    chosen = text.choose(caps, "esimerkki.fi", 3)
    assert [c.url for c in chosen] == ["https://www.esimerkki.fi/", "https://www.esimerkki.fi/impressum",
                                      "https://www.esimerkki.fi/yhteystiedot"]
    assert chosen[0].timestamp == "20261102000000"


def warc(body: bytes, ctype=b"text/html; charset=utf-8") -> bytes:
    http = b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype + b"\r\n\r\n" + body
    rec = b"WARC/1.0\r\nWARC-Type: response\r\nContent-Length: %d\r\n\r\n" % len(http) + http + b"\r\n\r\n"
    return gzip.compress(rec)


def test_a_warc_record_is_read_and_hashed():
    html, ctype = commoncrawl.parse_record(warc(PAGE), 1_000_000)
    assert html.startswith(b"<html>") and "utf-8" in ctype
    with pytest.raises(commoncrawl.ArchiveError):
        commoncrawl.parse_record(b"not gzip", 100)
    with pytest.raises(commoncrawl.ArchiveError):
        commoncrawl.parse_record(gzip.compress(b"WARC/1.0\r\nWARC-Type: request\r\n\r\nGET /"), 100)


class Resp:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]


class Conn:
    def close(self):
        pass


class OneAnswer:
    def __init__(self, data=b"", status=None):
        self.data, self.status, self.asked = data, status, []

    def open(self, target, method="GET", headers=None):
        self.asked.append((target, headers))
        if self.status:
            raise FetchError("no", self.status)
        return Conn(), Resp(self.data)


def test_index_lookup_keeps_only_html_pages_that_answered_200():
    rows = [
        '{"url": "https://a.fi/", "timestamp": "20261101000000", "status": "200", "mime": "text/html", "filename": "crawl-data/CC-MAIN-2026-44/segments/1/warc/a.warc.gz", "offset": "10", "length": "20"}',
        '{"url": "https://a.fi/x.pdf", "timestamp": "20261101000000", "status": "200", "mime": "application/pdf", "filename": "crawl-data/CC-MAIN-2026-44/segments/1/warc/a.warc.gz", "offset": "10", "length": "20"}',
        '{"url": "https://a.fi/y", "timestamp": "20261101000000", "status": "200", "mime": "text/html", "filename": "../../etc/passwd", "offset": "10", "length": "20"}',
        'not json']
    gw = OneAnswer("\n".join(rows).encode())
    found = commoncrawl.lookup(gw, "CC-MAIN-2026-44", "a.fi", 100)
    assert [c.url for c in found] == ["https://a.fi/"] and found[0].captured_at == "2026-11-01T00:00:00Z"
    assert gw.asked[0][0].startswith("https://index.commoncrawl.org/CC-MAIN-2026-44-index?url=a.fi&matchType=domain")
    assert commoncrawl.lookup(OneAnswer(status=404), "CC-MAIN-2026-44", "nothing.fi", 100) == []
    with pytest.raises(FetchError):
        commoncrawl.lookup(OneAnswer(status=500), "CC-MAIN-2026-44", "a.fi", 100)


def test_a_page_is_read_by_a_ranged_request():
    gw = OneAnswer(warc(PAGE))
    page = commoncrawl.read_page(gw, commoncrawl.Capture("https://a.fi/", "20261101000000",
                                                         "crawl-data/CC-MAIN-2026-44/segments/1/warc/a.warc.gz", 100, 50), 10**6)
    assert gw.asked[0] == ("https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-44/segments/1/warc/a.warc.gz",
                           {"Range": "bytes=100-149"})
    assert page.sha256.startswith("sha256:") and page.charset == "utf-8"


def test_newest_crawls_first():
    gw = OneAnswer(b'[{"id": "CC-MAIN-2026-40"}, {"id": "CC-MAIN-2026-44"}, {"id": "bad"}]')
    assert commoncrawl.crawls(gw, 1) == ["CC-MAIN-2026-44"]


def test_an_internationalised_host_is_asked_in_its_ascii_form():
    assert names.ascii_host("www.äänekoski.fi") == "xn--nekoski-4waa.fi"
    assert names.ascii_host("WWW.Example.FI") == "example.fi" and names.ascii_host("") == ""
