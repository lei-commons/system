"""GLEIF golden copy CSV rows, in the shape ``gleif.to_record`` reads.

The golden copy and the API carry the same data with different names. Turning
CSV rows into the API's shape keeps one mapping, so both paths apply the same
rules, including the natural-person filter.
"""

from __future__ import annotations

import csv
import io
import zipfile

MAX_OTHER_NAMES = 5
MAX_EXTRA_LINES = 3


def _address(row, prefix):
    lines = [row.get(f"{prefix}.FirstAddressLine", "")]
    lines += [row.get(f"{prefix}.AdditionalAddressLine.{i}", "") for i in range(1, MAX_EXTRA_LINES + 1)]
    return {
        "addressLines": [line for line in lines if line],
        "city": row.get(f"{prefix}.City") or None,
        "region": row.get(f"{prefix}.Region") or None,
        "country": row.get(f"{prefix}.Country") or None,
        "postalCode": row.get(f"{prefix}.PostalCode") or None,
    }


def to_api_shape(row: dict) -> dict:
    other = [{"name": row[f"Entity.OtherEntityNames.OtherEntityName.{i}"]}
             for i in range(1, MAX_OTHER_NAMES + 1) if row.get(f"Entity.OtherEntityNames.OtherEntityName.{i}")]
    authority = (row.get("Entity.RegistrationAuthority.RegistrationAuthorityID")
                 or row.get("Entity.RegistrationAuthority.OtherRegistrationAuthorityID") or None)
    entity = {
        "legalName": {"name": row.get("Entity.LegalName") or None},
        "otherNames": other,
        "legalAddress": _address(row, "Entity.LegalAddress"),
        "headquartersAddress": _address(row, "Entity.HeadquartersAddress"),
        "registeredAt": {"id": authority},
        "registeredAs": row.get("Entity.RegistrationAuthority.RegistrationAuthorityEntityID") or None,
        "jurisdiction": row.get("Entity.LegalJurisdiction") or None,
        "category": row.get("Entity.EntityCategory") or None,
        "legalForm": {"id": row.get("Entity.LegalForm.EntityLegalFormCode") or None},
        "status": row.get("Entity.EntityStatus") or None,
        "creationDate": row.get("Entity.EntityCreationDate") or None,
    }
    return {"attributes": {"lei": row["LEI"], "entity": entity}}


def read_zip(binary_file):
    """Yield API-shaped records from a golden copy zip, streaming one row at a time."""
    with zipfile.ZipFile(binary_file) as archive:
        names = [n for n in archive.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in the archive, found {len(names)}")
        with archive.open(names[0]) as raw:
            for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline="")):
                yield to_api_shape(row)
