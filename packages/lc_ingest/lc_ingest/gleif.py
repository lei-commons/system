"""GLEIF LEI records to directory records.

Natural persons are never records (constitution Article 3.4). GLEIF's
SOLE_PROPRIETOR category is a person trading in their own name, often at a home
address, so those records are dropped here, before anything is stored.
"""

from __future__ import annotations

from lc_core.directory_id import mint

# GLEIF Registration Authority codes for registers we also read directly, so
# both sources mint the same directory ID for the same company.
REGISTRIES = {
    "RA000585": "GB-COH",  # Companies House, England and Wales
    "RA000586": "GB-COH",  # Companies House, Northern Ireland
    "RA000587": "GB-COH",  # Companies House, Scotland
}
NATURAL_PERSON_CATEGORIES = {"SOLE_PROPRIETOR"}
ADDRESS_FIELDS = ("addressLines", "city", "region", "country", "postalCode")


class Skip(Exception):
    """This record must not enter the directory. The message says why."""


def _address(raw):
    if not raw:
        return None
    return {k: raw[k] for k in ADDRESS_FIELDS if raw.get(k)}


def _names(entity):
    names = [n.get("name") for n in entity.get("otherNames") or []]
    return [n for n in names if n]


def _date(value):
    return value[:10] if value else None


def to_record(raw: dict) -> dict:
    """Map one record from the GLEIF API to directory fields. Raises Skip to drop it."""
    attributes = raw["attributes"]
    lei, entity = attributes["lei"], attributes["entity"]
    if entity.get("category") in NATURAL_PERSON_CATEGORIES:
        raise Skip("natural person")

    address = _address(entity.get("legalAddress"))
    country = (address or {}).get("country") or (entity.get("jurisdiction") or "")[:2]
    if not country:
        raise Skip("no country")
    authority = (entity.get("registeredAt") or {}).get("id")
    number = entity.get("registeredAs")

    if authority and number:
        directory_id = mint(country, REGISTRIES.get(authority, authority), number)
    else:
        directory_id = mint(country, "LEI", lei)

    record = {
        "directoryId": directory_id,
        "lei": lei,
        "legalName": (entity.get("legalName") or {}).get("name"),
        "otherNames": _names(entity),
        "legalForm": (entity.get("legalForm") or {}).get("id"),
        "legalJurisdiction": entity.get("jurisdiction"),
        "registrationAuthority": authority,
        "registrationNumber": number,
        "legalAddress": address,
        "headquartersAddress": _address(entity.get("headquartersAddress")),
        "legalStatus": entity.get("status"),
        "incorporationDate": _date(entity.get("creationDate")),
    }
    if not record["legalName"]:
        raise Skip("no legal name")
    return {k: v for k, v in record.items() if v not in (None, [], {})}
