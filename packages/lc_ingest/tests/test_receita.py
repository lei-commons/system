import io
import json
import zipfile
from collections import Counter

from lc_core.directory_id import mint
from lc_ingest.receita import (Join, build_lookup, directory_id, head_office_cnpj, is_person, mapper,
                               read_lookup, rows)
from lc_ingest.store import shard_of


def zipped(lines, name="F.CSV"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "\n".join(";".join(f'"{v}"' for v in line) for line in lines).encode("latin-1"))
    buf.seek(0)
    return buf


COMPANIES = [
    ["11222333", "ACME COMERCIO LTDA", "2062", "49", "1000,00", "03", ""],
    ["22333444", "JOSE DA SILVA 12345678901", "2135", "50", "0,00", "01", ""],  # MEI: a natural person
    ["33444555", "ASSOCIAÇÃO DE MORADORES", "3999", "16", "0,00", "05", ""],
    ["44555666", "CANDIDATO FULANO", "4090", "51", "0,00", "05", ""],  # natural-person nature
    ["55666777", "VELHA FECHADA S.A.", "2054", "10", "0,00", "05", ""],
]


def establishment(base, head="1", status="02", trade="", email="fulano@example.com"):
    order, dv = ("0001", head_office_cnpj(base)[-2:]) if head == "1" else ("0002", "99")
    return [base, order, dv, head, trade, status, "20240101", "00", "", "", "20100315", "4711302", "4712100,4713004",
            "RUA", "DAS FLORES", "100", "SALA 2", "CENTRO", "01001000", "SP", "7107", "11", "999998888", "", "", "",
            "", email, "", ""]


def test_check_digits_and_ids_match_gleif():
    assert head_office_cnpj("11222333") == "11222333000181"
    # GLEIF writes "11.222.333/0001-81" under RA000681; normalised, the IDs are the same.
    assert directory_id("11222333") == mint("BR", "RA000681", "11.222.333/0001-81")
    assert is_person("2135") and is_person("4090") and not is_person("2062") and not is_person("3999")


def test_lookup_drops_natural_persons(tmp_path):
    counts = Counter()
    paths = build_lookup([zipped(COMPANIES)], str(tmp_path), counts)
    lookup = {}
    for p in paths:
        with open(p, "rb") as f:
            lookup.update(read_lookup(f.read()))
    assert set(lookup) == {"11222333", "33444555", "55666777"} and counts["companies skipped: natural person"] == 2
    assert "12345678901" not in json.dumps(lookup)
    assert lookup["11222333"] == ("ACME COMERCIO LTDA", "2062")


def test_head_offices_join_their_company_and_contacts_are_never_read(tmp_path):
    counts = Counter()
    paths = build_lookup([zipped(COMPANIES)], str(tmp_path), counts)

    def read_shard(n):
        with open(paths[n], "rb") as f:
            return read_lookup(f.read())

    to_record = mapper({"7107": "SAO PAULO"})
    raw = [establishment("11222333", trade="ACME"), establishment("11222333", head="2"),
           establishment("22333444"), establishment("33444555"), establishment("55666777", status="08")]
    mapped = []
    for row in rows(zipped(raw)):
        try:
            mapped.append(to_record(row))
        except Exception as exc:
            counts[type(exc).__name__] += 1
    assert len(mapped) == 4  # the branch office is skipped
    join, kept = Join(read_shard), []
    by_shard = {}
    for r in mapped:
        by_shard.setdefault(shard_of(r["directoryId"]), []).append(r)
    for n, recs in by_shard.items():
        kept += join(n, recs, {}, counts)
    names = {r["legalName"]: r for r in kept}
    assert set(names) == {"ACME COMERCIO LTDA", "ASSOCIAÇÃO DE MORADORES"}  # no MEI, no closed newcomer
    acme = names["ACME COMERCIO LTDA"]
    assert acme["registrationNumber"] == "11222333000181" and acme["legalForm"] == "2062" and acme["otherNames"] == ["ACME"]
    assert acme["legalAddress"] == {"addressLines": ["RUA DAS FLORES, 100", "SALA 2", "CENTRO"], "city": "SAO PAULO",
                                    "region": "SP", "postalCode": "01001000", "country": "BR"}
    assert acme["industryCodes"] == ["4711302", "4712100", "4713004"] and acme["incorporationDate"] == "2010-03-15"
    text = json.dumps(kept)
    assert "fulano@example.com" not in text and "999998888" not in text


def test_a_stored_company_keeps_getting_status_updates(tmp_path):
    counts = Counter()
    paths = build_lookup([zipped(COMPANIES)], str(tmp_path), counts)
    to_record = mapper({})
    closed = to_record(next(rows(zipped([establishment("55666777", status="08")]))))
    n = shard_of(closed["directoryId"])
    with open(paths[n], "rb") as f:
        lookup = read_lookup(f.read())
    kept = Join(lambda _: lookup)(n, [closed], {closed["directoryId"]: {"legalStatus": "Active"}}, counts)
    assert kept[0]["legalStatus"] == "Closed" and kept[0]["dissolutionDate"] == "2024-01-01"
