"""New York and Singapore registers to directory records.

New York (data.ny.gov, "Active Corporations: Beginning 1800"): only the
columns below are ever requested. The process, chairman and registered-agent
names and addresses are never downloaded. The business location is kept
as city, state and ZIP only, because a small LLC's location is often a home.

Singapore (data.gov.sg, "ACRA Information on Corporate Entities"): sole
proprietorships and partnerships are registered business names held by
people, not legal entities, so they are dropped (Article 3.4). Officer
counts and audit firms are not kept.

Both mint directory IDs with GLEIF's code for the register (RA000628 for
New York, RA000523 for ACRA), so a company with an LEI merges with its
GLEIF record.
"""

from __future__ import annotations

import csv
import io

from lc_core.directory_id import mint

from .gleif import Skip

NY_GLEIF_REGISTRY, NY_REGISTRY = "RA000628", "US-NY-DOS"
NY_COLUMNS = ["dos_id", "current_entity_name", "initial_dos_filing_date", "county", "jurisdiction", "entity_type",
              "location_city", "location_state", "location_zip"]
SG_GLEIF_REGISTRY, SG_REGISTRY = "RA000523", "SG-ACRA"
SG_PEOPLE = {"Sole Proprietorship/ Partnership"}
SG_FORMER_NAMES = 15

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "Puerto Rico": "PR",
}
US_CODES = set(US_STATES.values())


def _clean(value) -> str:
    return " ".join((value or "").split())


def ny_record(row: dict) -> dict:
    number, name = _clean(row.get("dos_id")), _clean(row.get("current_entity_name"))
    if not number or not name:
        raise Skip("no number or name")
    state = _clean(row.get("location_state")).upper()
    address = {"city": _clean(row.get("location_city")).upper(), "region": state,
               "postalCode": _clean(row.get("location_zip")), "country": "US" if state in US_CODES else ""}
    jurisdiction = US_STATES.get(_clean(row.get("jurisdiction")))
    record = {
        "directoryId": mint("US", NY_GLEIF_REGISTRY, number),
        "legalName": name,
        "legalForm": _clean(row.get("entity_type")),
        "legalJurisdiction": f"US-{jurisdiction}" if jurisdiction else None,
        "registrationAuthority": NY_REGISTRY,
        "registrationNumber": number,
        "legalStatus": "Active",  # the dataset lists active corporations only
        "incorporationDate": _clean(row.get("initial_dos_filing_date"))[:10] or None,
        "legalAddress": {k: v for k, v in address.items() if v},
        "recordMode": "mirror",
    }
    return {k: v for k, v in record.items() if v not in (None, "", [], {})}


def sg_record(row: dict) -> dict:
    if _clean(row.get("entity_type_description")) in SG_PEOPLE:
        raise Skip("natural person")
    number, name = _clean(row.get("uen")).upper(), _clean(row.get("entity_name"))
    if not number or not name or number == "UEN":
        raise Skip("no number or name")
    unit = "-".join(p for p in (_clean(row.get("level_no")), _clean(row.get("unit_no"))) if p and p != "na")
    first = " ".join(p for p in (_clean(row.get("block")), _clean(row.get("street_name"))) if p and p.lower() != "na")
    lines = [first, f"#{unit}" if unit else "", _clean(row.get("building_name")),
             _clean(row.get("other_address_line1")), _clean(row.get("other_address_line2"))]
    address = {"addressLines": [line for line in lines if line and line.lower() != "na"],
               "postalCode": _clean(row.get("postal_code")), "country": "SG"}
    codes = [_clean(row.get("primary_ssic_code")), _clean(row.get("secondary_ssic_code"))]
    record = {
        "directoryId": mint("SG", SG_GLEIF_REGISTRY, number),
        "legalName": name,
        "otherNames": [n for n in (_clean(row.get(f"former_entity_name{i}")) for i in range(1, SG_FORMER_NAMES + 1))
                       if n and n.lower() != "na"],
        "legalForm": _clean(row.get("company_type_description")) or _clean(row.get("entity_type_description")),
        "legalJurisdiction": "SG" if "Foreign" not in _clean(row.get("entity_type_description")) else None,
        "registrationAuthority": SG_REGISTRY,
        "registrationNumber": number,
        "legalStatus": _clean(row.get("entity_status_description")),
        "incorporationDate": _clean(row.get("registration_incorporation_date"))[:10] or None,
        "legalAddress": {k: v for k, v in address.items() if v},
        "industryCodes": [c for c in codes if c and c.lower() != "na"],
        "recordMode": "mirror",
    }
    return {k: v for k, v in record.items() if v not in (None, "", [], {}, "na")}


def read_csv(binary_file):
    """Rows of one CSV, or several CSVs joined end to end (repeated header rows are skipped)."""
    reader = csv.reader(io.TextIOWrapper(binary_file, encoding="utf-8", newline=""))
    header = None
    for row in reader:
        if header is None:
            header = [h.strip() for h in row]
            continue
        if [c.strip() for c in row] == header:
            continue
        yield dict(zip(header, row))
