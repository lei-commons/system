"""US state business registers published on Socrata open-data portals: Colorado, Oregon, Connecticut.

One profile per register: its portal and dataset, GLEIF's code for it (so a company with an LEI
merges with its register record), the query, and the mapping. Only the columns a profile names
are requested; registered agents, owners, officers, emails and mailing addresses are never
downloaded. The company's own address (principal office or business address) keeps its street
lines when the register has them (owner decision 2026-11-16), and city, region and postcode always. Each profile keeps an allow-list of entity types: general partnerships, sole
proprietorships, assumed business names and name reservations are dropped (they are people, or
not entities), and so is any type the register adds later until it is reviewed.

GLEIF codes checked against api.gleif.org on 2026-11-15: RA000599 Colorado, RA000631 Oregon,
RA000600 Connecticut. Colorado and Connecticut numbers match GLEIF's ``registeredAs`` exactly.
Oregon's match once GLEIF's dash is dropped (it always is) and its leading zeros too: GLEIF writes
"057491-14" for the dataset's 5749114 (config/sources.yaml gleif_identity, checked 2026-11-20).
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import date

from lc_core.directory_id import mint

from .gleif import Skip
from .registers import US_CODES, US_STATES

LIMIT = 20_000_000  # above any of these datasets, so one request returns the whole dataset

US_NAMES = {name.upper(): code for name, code in US_STATES.items()}
US_COUNTRY = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA", "U.S.A.", "U.S."}


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def us_jurisdiction(value) -> str | None:
    """'CO', 'Colorado', 'COLORADO' -> 'US-CO'; anything else (a country, empty) -> None."""
    value = _clean(value).upper()
    if value in US_CODES:
        return f"US-{value}"
    code = US_NAMES.get(value)
    return f"US-{code}" if code else None


PLACEHOLDERS = {"NO INFORMATION PROVIDED", "N/A", "NA", "NONE", "UNKNOWN", "NOT PROVIDED", "-", "."}


def _real(value) -> str:
    """The value, or '' if it is a register's placeholder for "nothing here"."""
    value = _clean(value)
    return "" if value.upper() in PLACEHOLDERS else value


def us_address(city, state, postal, country=None, lines=()) -> dict:
    """A company's address: street lines when given (optional), then city, state, ZIP and country."""
    state = _real(state).upper()
    country = _real(country).upper()
    city, postal = _real(city), _real(postal)
    address = {"addressLines": [line for line in (_real(x) for x in lines) if line],
               "city": city.upper(), "region": state, "postalCode": postal,
               "country": "US" if (state in US_CODES and (not country or country in US_COUNTRY)) else ""}
    return {k: v for k, v in address.items() if v}


def _date(value) -> str | None:
    value = _clean(value)[:10]
    return value or None


def _quote(values) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))


@dataclass(frozen=True)
class Profile:
    source: str          # the registry id (config/sources.yaml), which records also carry
    job: str             # its Cloud Run job, and the lease's name
    store: str           # its prefix in the data bucket
    host: str
    dataset: str
    ra: str              # GLEIF's code for the register: directory IDs are minted with it
    registry: str        # registrationAuthority as published
    params: dict = field(default_factory=dict)
    mapper: object = None

    def url(self) -> str:
        query = urllib.parse.urlencode({**self.params, "$limit": str(LIMIT)}, safe="$,()'")
        return f"https://{self.host}/resource/{self.dataset}.csv?{query}"

    def key(self, today: date) -> str:
        """One run per ISO week: the registers change little from day to day, and comparing millions of
        records every day would take most of the cycle."""
        year, week, _ = today.isocalendar()
        return f"{year}-W{week:02d}"

    def record(self, row: dict) -> dict:
        record = self.mapper(row)
        return {k: v for k, v in record.items() if v not in (None, "", [], {})}


# ---- Colorado: Business Entities in Colorado (data.colorado.gov, 4ykn-tg5h), public domain ----

CO_FORMS = {
    "DLLC": "Limited liability company", "DPC": "Profit corporation", "DNC": "Nonprofit corporation",
    "FLLC": "Foreign limited liability company", "FPC": "Foreign profit corporation", "DLP": "Limited partnership",
    "DLLP": "Limited liability partnership", "FNC": "Foreign nonprofit corporation",
    "DLLLP": "Limited liability limited partnership", "FLP": "Foreign limited partnership", "FO": "Foreign entity (other)",
    "DPC-PBC": "Public benefit corporation", "FLLP": "Foreign limited liability partnership", "IC": "Insurance company",
    "DC56": "Cooperative (Article 56)", "DLPA": "Limited partnership association", "UNA": "Unincorporated nonprofit association",
    "FLLLP": "Foreign limited liability limited partnership", "DLCA": "Limited cooperative association", "CS": "CS",
    "DT": "Business trust", "WC": "WC", "DC55": "Cooperative (Article 55)", "FCOOP": "Foreign cooperative",
    "DLCA-PBC": "Public benefit limited cooperative association", "DC56-PBC": "Public benefit cooperative (Article 56)",
    "CU": "Credit union", "DC55-PBC": "Public benefit cooperative (Article 55)", "SL": "Savings and loan association",
    "FLPA": "Foreign limited partnership association", "FLCA": "Foreign limited cooperative association",
}
CO_PEOPLE = {"GP", "SP"}  # general partnerships and sole proprietorships: people's business names
CO_COLUMNS = ["entityid", "entityname", "entitystatus", "jurisdictonofformation", "entitytype", "entityformdate",
              "principaladdress1", "principaladdress2", "principalcity", "principalstate", "principalzipcode", "principalcountry"]


def co_record(row: dict) -> dict:
    kind = _clean(row.get("entitytype")).upper()
    if kind in CO_PEOPLE:
        raise Skip("natural person")
    if kind not in CO_FORMS:
        raise Skip(f"entity type not reviewed: {kind or 'none'}")
    number, name = _clean(row.get("entityid")), _clean(row.get("entityname"))
    if not number or not name:
        raise Skip("no number or name")
    return {
        "directoryId": mint("US", "RA000599", number),
        "legalName": name,
        "legalForm": CO_FORMS[kind],
        "legalJurisdiction": us_jurisdiction(row.get("jurisdictonofformation")),
        "registrationAuthority": "US-CO-SOS",
        "registrationNumber": number,
        "legalStatus": _clean(row.get("entitystatus")) or None,
        "incorporationDate": _date(row.get("entityformdate")),
        "legalAddress": us_address(row.get("principalcity"), row.get("principalstate"), row.get("principalzipcode"),
                                   row.get("principalcountry"), (row.get("principaladdress1"), row.get("principaladdress2"))),
        "recordMode": "mirror",
    }


COLORADO = Profile(
    source="us-co-sos", job="ingest-co", store="us-co", host="data.colorado.gov", dataset="4ykn-tg5h", ra="RA000599",
    registry="US-CO-SOS", mapper=co_record,
    params={"$select": ",".join(CO_COLUMNS), "$where": f"entitytype in ({_quote(CO_FORMS)})", "$order": "entityid"})


# ---- Oregon: Active Businesses - ALL (data.oregon.gov, tckn-sxa6), no licence stated ----
# One row per associated name (registered agent, principal place of business, ...), so the query
# groups by registry number: one row per business, with the principal place of business's address
# only. Names and addresses of agents and representatives are never requested.

OR_FORMS = {
    "DOMESTIC LIMITED LIABILITY COMPANY", "DOMESTIC BUSINESS CORPORATION", "DOMESTIC NONPROFIT CORPORATION",
    "FOREIGN LIMITED LIABILITY COMPANY", "FOREIGN BUSINESS CORPORATION", "DOMESTIC PROFESSIONAL CORPORATION",
    "FOREIGN NONPROFIT CORPORATION", "DOMESTIC LIMITED PARTNERSHIP", "FOREIGN LIMITED PARTNERSHIP",
    "FOREIGN PROFESSIONAL CORPORATION", "DOMESTIC REGISTERED LIMITED LIABILITY PARTNERSHIP", "COOPERATIVE",
    "FOREIGN REGISTERED LIMITED LIABILITY PARTNERSHIP", "DISTRICT IMPROVEMENT NONPROFIT", "FOREIGN BUSINESS TRUST",
    "DOMESTIC BUSINESS TRUST", "ACT OF GOVERNMENT", "DISTRICT IMPROVEMENT PROFIT",
}
OR_NOT_ENTITIES = {"ASSUMED BUSINESS NAME", "RESERVED NAME", "REGISTERED NAME"}
_PPB = "associated_name_type='PRINCIPAL PLACE OF BUSINESS'"
OR_SELECT = ["registry_number", "max(business_name) as entity_name", "max(entity_type) as entity_kind",
             "min(registry_date) as registered", "max(jurisdiction) as formed_in",
             f"max(case({_PPB}, address, true, null)) as ppb_address", f"max(case({_PPB}, address_continued, true, null)) as ppb_address_2",
             f"max(case({_PPB}, city, true, null)) as ppb_city", f"max(case({_PPB}, state, true, null)) as ppb_state",
             f"max(case({_PPB}, zip, true, null)) as ppb_zip"]


def or_record(row: dict) -> dict:
    kind = _clean(row.get("entity_kind")).upper()
    if kind in OR_NOT_ENTITIES:
        raise Skip("not an entity")
    if kind not in OR_FORMS:
        raise Skip(f"entity type not reviewed: {kind or 'none'}")
    number, name = _clean(row.get("registry_number")), _clean(row.get("entity_name"))
    if not number or not name:
        raise Skip("no number or name")
    return {
        "directoryId": mint("US", "RA000631", number),
        "legalName": name,
        "legalForm": kind.capitalize(),
        "legalJurisdiction": us_jurisdiction(row.get("formed_in")),
        "registrationAuthority": "US-OR-SOS",
        "registrationNumber": number,
        "legalStatus": "Active",  # the dataset lists active businesses only
        "incorporationDate": _date(row.get("registered")),
        "legalAddress": us_address(row.get("ppb_city"), row.get("ppb_state"), row.get("ppb_zip"),
                                   lines=(row.get("ppb_address"), row.get("ppb_address_2"))),
        "recordMode": "mirror",
    }


OREGON = Profile(
    source="us-or-sos", job="ingest-or", store="us-or", host="data.oregon.gov", dataset="tckn-sxa6", ra="RA000631",
    registry="US-OR-SOS", mapper=or_record,
    params={"$select": ",".join(OR_SELECT), "$where": f"entity_type in ({_quote(OR_FORMS)})", "$group": "registry_number",
            "$order": "registry_number"})


# ---- Connecticut: Business Registry - Business Master (data.ct.gov, n7gp-d28j), public domain ----

CT_FORMS = {
    "LLC": "Limited liability company", "Stock": "Stock corporation", "Non-Stock": "Nonstock corporation",
    "Limited Partnership": "Limited partnership", "Religious": "Religious corporation",
    "LLP": "Limited liability partnership", "Statutory Trust": "Statutory trust", "Special Chartered": "Specially chartered",
    "Cemetery": "Cemetery association", "B Corp": "Benefit corporation", "Insurance Stock": "Insurance stock corporation",
    "Non-Stock Specially Chartered": "Specially chartered nonstock corporation", "Bank Stock": "Bank (stock)",
    "Credit Union Non-Stock": "Credit union (nonstock)", "Stock Specially Chartered": "Specially chartered stock corporation",
    "Bank Non-Stock": "Bank (nonstock)", "Insurance Non-Stock": "Insurance nonstock corporation",
    "Cooperative Association": "Cooperative association", "Credit Union Stock": "Credit union (stock)",
}
CT_NOT_ENTITIES = {"Reserved", "Expired Reservation", "Reserved Cancel", "Rejected", "Expired"}
CT_COLUMNS = ["accountnumber", "name", "business_type", "status", "date_registration", "dissolution_date", "citizenship",
              "state_or_territory_formation", "billingstreet", "billing_unit", "billingcity", "billingstate",
              "billingpostalcode", "billingcountry", "naics_code"]


def ct_record(row: dict) -> dict:
    kind = _clean(row.get("business_type"))
    if kind not in CT_FORMS:
        raise Skip(f"entity type not reviewed: {kind or 'none'}")
    status = _clean(row.get("status"))
    if status in CT_NOT_ENTITIES:
        raise Skip("not an entity")
    number, name = _clean(row.get("accountnumber")), _clean(row.get("name"))
    if not number or not name or set(number) == {"0"}:
        raise Skip("no number or name")
    domestic = _clean(row.get("citizenship")).lower() == "domestic"
    naics = _clean(row.get("naics_code"))
    return {
        "directoryId": mint("US", "RA000600", number),
        "legalName": name,
        "legalForm": CT_FORMS[kind],
        "legalJurisdiction": "US-CT" if domestic else us_jurisdiction(row.get("state_or_territory_formation")),
        "registrationAuthority": "US-CT-SOTS",
        "registrationNumber": number,
        "legalStatus": status or None,
        "incorporationDate": _date(row.get("date_registration")),
        "dissolutionDate": _date(row.get("dissolution_date")),
        "legalAddress": us_address(row.get("billingcity"), row.get("billingstate"), row.get("billingpostalcode"),
                                   row.get("billingcountry"), (row.get("billingstreet"), row.get("billing_unit"))),
        "industryCodes": [naics] if naics.isdigit() else [],
        "recordMode": "mirror",
    }


CONNECTICUT = Profile(
    source="us-ct-sots", job="ingest-ct", store="us-ct", host="data.ct.gov", dataset="n7gp-d28j", ra="RA000600",
    registry="US-CT-SOTS", mapper=ct_record,
    params={"$select": ",".join(CT_COLUMNS),
            "$where": f"business_type in ({_quote(CT_FORMS)}) and status not in ({_quote(CT_NOT_ENTITIES)})",
            "$order": "accountnumber"})


PROFILES = {p.source: p for p in (COLORADO, OREGON, CONNECTICUT)}
