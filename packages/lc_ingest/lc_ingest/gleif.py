"""GLEIF LEI records to directory records.

Natural persons are never records (constitution Article 3.4). GLEIF's
SOLE_PROPRIETOR category is a person trading in their own name, often at a home
address, so those records are dropped here, before anything is stored.
"""

from __future__ import annotations

from lc_core import gleif_identity
from lc_core.directory_id import mint

# How GLEIF's registration authority and number map to a register we read directly (Companies House's
# three codes, Oregon, Canada, Norway, Japan), so both sources mint the same directory ID for the same
# company, lives in config/sources.yaml ``gleif_identity`` (lc_core.gleif_identity).
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


def lei_id(country: str, lei: str) -> str:
    """The ID for a record identified by its LEI alone."""
    return mint(country, "LEI", lei)


def record_country(record: dict) -> str:
    return (record.get("legalAddress") or {}).get("country") or (record.get("legalJurisdiction") or "")[:2]


def directory_id(lei: str, authority, number, country: str, identity=None) -> str:
    """The ID for a GLEIF record: its register's key when GLEIF gives one, else its LEI.
    ``identity`` (lc_core.gleif_identity.Identity) defaults to config/sources.yaml's rules."""
    if authority and number:
        return mint(*(identity or gleif_identity.default()).key(authority, number, country))
    return lei_id(country, lei)


def stored_id(record: dict, identity=None) -> str:
    """The ID ``to_record`` gives a stored GLEIF record under ``identity``, from the record's own fields
    (lc_ingest.rekey). Before any collision is kept apart (pipeline.separate_collision)."""
    return directory_id(record["lei"], record.get("registrationAuthority"), record.get("registrationNumber"),
                        record_country(record), identity)


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

    record = {
        "directoryId": directory_id(lei, authority, number, country),
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
