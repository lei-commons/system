"""Brazil: Receita Federal's open CNPJ data to directory records.

The monthly release has company files (Empresas: name and legal nature) and
establishment files (Estabelecimentos: status, address, activity codes and
dates, one row per office). A directory record is a company joined to its
head office.

Natural persons are dropped at the first step (Article 3.4). The legal
natures 2135 (empresário individual, which includes every MEI) and 4xxx
(natural persons) are not companies, and their names often embed a person's
name and CPF. They never reach the lookup, so their offices are never joined.
The establishment files' phone, fax and email columns are never read.

The directory ID uses the head office's CNPJ (base + 0001 + check digits),
minted with GLEIF's code for this register (RA000681). A company with an
LEI therefore gets the same ID from both sources, and the two merge.
"""

from __future__ import annotations

import csv
import gzip
import io
import zipfile

from lc_core.directory_id import mint

from .gleif import Skip
from .store import SHARDS, shard_of

GLEIF_REGISTRY = "RA000681"  # GLEIF's registration authority code for the CNPJ register
REGISTRY = "BR-RFB"
PERSON_NATURES = {"2135"}  # empresário (individual), which includes MEI
STATUS = {"1": "Null", "2": "Active", "3": "Suspended", "4": "Unfit", "8": "Closed"}
MAX_SECONDARY = 10

# Estabelecimentos columns, per the Receita Federal layout (cnpj-metadados.pdf).
E_BASE, E_ORDER, E_DV, E_HEAD, E_TRADE, E_STATUS, E_STATUS_DATE = 0, 1, 2, 3, 4, 5, 6
E_FOREIGN_CITY, E_COUNTRY, E_START, E_CNAE, E_CNAE2 = 8, 9, 10, 11, 12
E_STREET_TYPE, E_STREET, E_NUMBER, E_COMPLEMENT, E_DISTRICT, E_CEP, E_UF, E_CITY = 13, 14, 15, 16, 17, 18, 19, 20
# Columns 21–27 are phones, fax and email: never read. 28–29 are the special situation.


def is_person(nature: str) -> bool:
    nature = (nature or "").strip()
    return nature in PERSON_NATURES or nature.startswith("4")


def _check_digit(chars: str, weights) -> str:
    # Works for numeric and the new alphanumeric CNPJs: each character's value is its code minus 48.
    total = sum((ord(c) - 48) * w for c, w in zip(chars, weights))
    r = total % 11
    return "0" if r < 2 else str(11 - r)


def head_office_cnpj(base: str) -> str:
    """The CNPJ of a company's head office, branch 0001, with its check digits.

    >>> head_office_cnpj("11222333")
    '11222333000181'
    """
    first12 = base + "0001"
    d1 = _check_digit(first12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    d2 = _check_digit(first12 + d1, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return first12 + d1 + d2


def directory_id(base: str) -> str:
    return mint("BR", GLEIF_REGISTRY, head_office_cnpj(base))


def _date(value: str):
    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit() or value == "00000000":
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def rows(binary_file):
    """Rows of a Receita Federal zip: one headerless, ';'-separated, Latin-1 CSV."""
    with zipfile.ZipFile(binary_file) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one file in the archive, found {len(names)}")
        with archive.open(names[0]) as raw:
            yield from csv.reader(io.TextIOWrapper(raw, encoding="latin-1", newline=""), delimiter=";")


def build_lookup(company_zips, workdir, counts) -> list:
    """Write the companies (natural persons dropped) to 256 shard files, by their records' shard.

    ``company_zips`` yields open binary files of the Empresas zips. Each line
    is ``base\\tname\\tnature``. Returns the paths, one per shard.
    """
    paths = [f"{workdir}/{n:03d}.tsv.gz" for n in range(SHARDS)]
    outs = [gzip.open(p, "wt", encoding="utf-8", compresslevel=5) for p in paths]
    try:
        for binary in company_zips:
            for row in rows(binary):
                if len(row) < 3 or not row[0].strip():
                    counts["companies skipped: malformed"] += 1
                    continue
                base, name, nature = row[0].strip(), " ".join(row[1].split()), row[2].strip()
                if is_person(nature):
                    counts["companies skipped: natural person"] += 1
                    continue
                if not name:
                    counts["companies skipped: no name"] += 1
                    continue
                outs[shard_of(directory_id(base))].write(f"{base}\t{name}\t{nature}\n")
                counts["companies"] += 1
    finally:
        for out in outs:
            out.close()
    return paths


def read_lookup(data: bytes) -> dict:
    out = {}
    for line in gzip.decompress(data).decode("utf-8").splitlines():
        base, name, nature = line.split("\t")
        out[base] = (name, nature)
    return out


def mapper(municipalities: dict):
    """Head-office rows to partial records. Company name and legal nature come from the lookup."""

    def to_record(row) -> dict:
        if len(row) < 21:
            raise Skip("short row")
        if row[E_HEAD].strip() != "1":
            raise Skip("branch office")
        base, order, dv = row[E_BASE].strip(), row[E_ORDER].strip(), row[E_DV].strip()
        status = STATUS.get(row[E_STATUS].strip().lstrip("0"), row[E_STATUS].strip())
        street = " ".join(p for p in (row[E_STREET_TYPE].strip(), row[E_STREET].strip()) if p)
        number = row[E_NUMBER].strip()
        lines = [f"{street}, {number}" if street and number else street, row[E_COMPLEMENT].strip(), row[E_DISTRICT].strip()]
        foreign = row[E_FOREIGN_CITY].strip()
        address = {
            "addressLines": [" ".join(line.split()) for line in lines if line and line.strip()],
            "city": foreign or municipalities.get(row[E_CITY].strip(), ""),
            "region": row[E_UF].strip() if row[E_UF].strip() != "EX" else "",
            "postalCode": row[E_CEP].strip(),
            "country": "BR" if not foreign else "",
        }
        codes = [row[E_CNAE].strip()] + [c.strip() for c in row[E_CNAE2].split(",") if c.strip()][:MAX_SECONDARY]
        trade = " ".join(row[E_TRADE].split())
        record = {
            "directoryId": directory_id(base),
            "registrationNumber": base + order + dv,
            "registrationAuthority": REGISTRY,
            "legalJurisdiction": "BR",
            "legalStatus": status,
            "incorporationDate": _date(row[E_START]),
            "dissolutionDate": _date(row[E_STATUS_DATE]) if status == "Closed" else None,
            "legalAddress": {k: v for k, v in address.items() if v},
            "industryCodes": [c for c in codes if c],
            "otherNames": [trade] if trade else [],
            "recordMode": "mirror",
        }
        return {k: v for k, v in record.items() if v not in (None, "", [], {})}

    return to_record


class Join:
    """The runner's ``prepare`` hook: add each head office's company, and keep
    active companies, plus any company already stored so its status stays current."""

    def __init__(self, read_shard):
        self._read, self._n, self._lookup = read_shard, None, {}

    def __call__(self, n, records, current, counts):
        if n != self._n:
            self._n, self._lookup = n, self._read(n)
        kept = []
        for record in records:
            company = self._lookup.get(record["registrationNumber"][:8])
            if company is None:
                counts["skipped: no company (natural person or unknown)"] += 1
                continue
            if record.get("legalStatus") != "Active" and record["directoryId"] not in current:
                counts["skipped: not active"] += 1
                continue
            name, nature = company
            record = {**record, "legalName": name, "legalForm": nature}
            others = [o for o in record.get("otherNames", []) if o != name]
            if others:
                record["otherNames"] = others
            else:
                record.pop("otherNames", None)
            kept.append(record)
        return kept


def read_municipalities(binary_file) -> dict:
    return {row[0].strip(): row[1].strip() for row in rows(binary_file) if len(row) >= 2}
