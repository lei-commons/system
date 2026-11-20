import csv
import io
import zipfile

import pytest

from lc_ingest import nta
from lc_ingest.pipeline import Skip


def row(**over):
    base = dict.fromkeys(nta.COLUMNS, "")
    base.update(sequenceNumber="1", corporateNumber="3010401016070", process="01", correct="0", kind="301",
                name="株式会社ＡＢＣ商事", prefectureName="東京都", cityName="千代田区", streetNumber="丸の内１丁目１－１",
                prefectureCode="13", cityCode="101", postCode="1000005", assignmentDate="2015-10-05", latest="1",
                enName="ABC Shoji Co., Ltd.", furigana="エービーシーショウジ", hihyoji="0")
    base.update(over)
    return base


def zipped(rows, extra_csv=False):
    out = io.BytesIO()
    text = io.StringIO()
    w = csv.writer(text, lineterminator="\r\n")
    for r in rows:
        w.writerow([r[c] for c in nta.COLUMNS])
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("00_zenkoku_all_20261030.csv", text.getvalue())
        z.writestr("00_zenkoku_all_20261030.csv.asc", "-----BEGIN PGP SIGNATURE-----")
        if extra_csv:
            z.writestr("other.csv", "x")
    out.seek(0)
    return out


def test_a_domestic_company_with_its_street_address_and_english_name():
    r = nta.nta_record(row())
    assert r["legalName"] == "株式会社ABC商事"  # full-width letters normalised
    assert r["transliteratedNames"] == ["ABC Shoji Co., Ltd."]
    assert r["legalForm"] == "Kabushiki kaisha (stock company)" and r["legalJurisdiction"] == "JP"
    assert r["registrationNumber"] == "3010401016070" and r["registrationAuthority"] == "JP-NTA"
    assert r["legalAddress"] == {"addressLines": ["丸の内1丁目1-1"], "city": "千代田区", "region": "東京都",
                                 "postalCode": "100-0005", "country": "JP"}
    assert r["legalStatus"] == "Active" and r["dissolutionDate"] is None


def test_the_directory_id_uses_gleifs_code_for_the_corporate_number():
    from lc_core.directory_id import mint
    assert nta.nta_record(row())["directoryId"] == mint("JP", "RA001075", "3010401016070")


def test_closed_corporations_say_why_and_when():
    r = nta.nta_record(row(closeDate="2024-03-31", closeCause="11"))
    assert r["legalStatus"] == "Closed: dissolved by merger" and r["dissolutionDate"] == "2024-03-31"
    assert nta.nta_record(row(closeDate="2024-03-31", closeCause="99"))["legalStatus"] == "Closed"


def test_a_foreign_company_keeps_its_head_office_abroad_and_no_jurisdiction():
    r = nta.nta_record(row(kind="401", addressOutside="Ｏｎｅ　Ｌｉｍｅ　Ｓｔｒｅｅｔ　Ｌｏｎｄｏｎ"))
    assert r["legalJurisdiction"] is None and r["legalForm"] == "Foreign company"
    assert r["legalAddress"] == {"addressLines": ["One Lime Street London"]}
    assert "headquartersAddress" not in r  # its office in Japan isn't its headquarters


@pytest.mark.parametrize("over,why", [
    ({"hihyoji": "1"}, "excluded"), ({"corporateNumber": "123"}, "number"), ({"name": "  "}, "name"),
])
def test_rows_left_out(over, why):
    with pytest.raises(Skip, match=why):
        nta.nta_record(row(**over))


def test_the_reader_reads_the_one_csv_and_checks_every_row_has_30_columns():
    rows = list(nta.read_zip(zipped([row(), row(sequenceNumber="2", corporateNumber="5010001000001")])))
    assert [r["corporateNumber"] for r in rows] == ["3010401016070", "5010001000001"]
    with pytest.raises(ValueError, match="one CSV"):
        list(nta.read_zip(zipped([row()], extra_csv=True)))
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("a.csv", "1,2,3\r\n")
    bad.seek(0)
    with pytest.raises(ValueError, match="columns"):
        list(nta.read_zip(bad))


def test_the_pages_date_is_the_unicode_files():
    html = ("<h2>CSV形式・Shift_JIS</h2> 令和8年9月29日更新 ... <h2>CSV形式・Unicode</h2> <p>令和8年10月30日更新</p>"
            " <h2>XML形式・Unicode</h2> 令和8年10月30日更新")
    assert nta.page_date(html) == "2026-10-30"
    with pytest.raises(ValueError):
        nta.page_date("<h2>something else</h2>")


# ---- daily difference files ----

DIFF_PAGE = """<h1>差分データダウンロード</h1>
<h2>CSV形式・Shift_JIS</h2><table><tr><th>更新日</th><th>ファイル</th></tr>
<tr><td>令和8年11月19日</td><td><a href="#" onclick="return doDownload(30001);">zip 12KB</a></td></tr></table>
<h2 class="title">CSV形式・<span>Unicode</span></h2><table><tr><th>更新日</th><th>ファイル</th></tr>
<tr><td>令和8年11月19日</td><td><a href="#" onclick="return doDownload(30103);">zip 14KB</a></td></tr>
<tr><td>令和8年 11月 17日</td><td><a href="#" onclick="return doDownload(30101);">zip 13KB</a></td></tr>
<tr><td>2026年11月18日</td><td><a href="#" onclick="return doDownload(30102);">zip 2MB</a></td></tr>
<tr><td>令和8年11月1日～令和8年11月30日</td><td><a href="#" onclick="return doDownload(30199);">zip</a></td></tr>
<tr><td>令和8年11月16日</td><td>no file</td></tr></table>
<h2>XML形式・Unicode</h2><table><tr><td>令和8年11月19日</td><td><a onclick="return doDownload(30201);">zip</a></td></tr></table>"""


def test_the_daily_files_are_the_dated_rows_under_the_section_oldest_first():
    files = nta.daily_files(DIFF_PAGE, "CSV形式・Unicode")
    assert files == [{"day": "2026-11-17", "number": "30101", "label": "令和8年11月17日"},
                     {"day": "2026-11-18", "number": "30102", "label": "2026年11月18日"},
                     {"day": "2026-11-19", "number": "30103", "label": "令和8年11月19日"}]
    # a row with two dates (a period) or no link is not a daily file; other sections are never read
    with pytest.raises(ValueError, match="heading"):
        nta.daily_files(DIFF_PAGE, "CSV形式・UTF-16")
    with pytest.raises(ValueError, match="no dated daily files"):
        nta.daily_files("<h2>CSV形式・Unicode</h2><p>準備中</p>", "CSV形式・Unicode")


def test_a_diff_row_is_the_latest_state_and_every_change_is_an_upsert():
    assert nta.nta_diff_record(row(process="11", name="株式会社新名"))["legalName"] == "株式会社新名"
    closed = nta.nta_diff_record(row(process="71", closeDate="2026-11-18", closeCause="11"))
    assert closed["legalStatus"] == "Closed: dissolved by merger" and closed["dissolutionDate"] == "2026-11-18"
    assert nta.nta_diff_record(row(process="22"))["legalStatus"] == "Active"
    assert "labels" not in nta.nta_diff_record(row(process="01"))


@pytest.mark.parametrize("over,why", [
    ({"latest": "0"}, "earlier state"), ({"process": "55"}, "unknown process code 55"),
    ({"process": ""}, "unknown process code"), ({"hihyoji": "1"}, "excluded"),
])
def test_diff_rows_left_out(over, why):
    with pytest.raises(Skip, match=why):
        nta.nta_diff_record(row(**over))


def test_a_deletion_labels_the_record_and_never_adds_one():
    from collections import Counter
    deleted = nta.nta_diff_record(row(process="99"))
    assert deleted["labels"] == [nta.ABSENT] and deleted["legalName"]  # kept, labelled, never removed
    other = nta.nta_diff_record(row(process="99", corporateNumber="5010001000001"))
    counts = Counter()
    kept = list(nta.keep_known_withdrawn(0, [deleted, other, nta.nta_diff_record(row(process="01", corporateNumber="7010001000002"))],
                                         {deleted["directoryId"]: {}}, counts))
    assert [r["registrationNumber"] for r in kept] == ["3010401016070", "7010001000002"]
    assert counts["skipped: deleted, never held"] == 1


def test_diffs_apply_on_the_latest_full_file_and_a_new_one_applies_them_again():
    processed = ["2026-09-30 zenkoku-unicode", "2026-10-02 diff-unicode on 2026-09-30", "2026-10-30 zenkoku-unicode"]
    assert nta.latest_full(processed) == "2026-10-30" and nta.latest_full(processed[1:2]) is None
    assert nta.diff_key("2026-11-02", "2026-10-30") == "2026-11-02 diff-unicode on 2026-10-30"
