"""Bulk registers approved by the owner on 2026-11-26, batch 4, on trial first (docs/source-trials.md).

As in batch 3, the layouts come from the publishers' documentation, never from a downloaded file, so columns are
read by name (``Columns``) and the first trial run says what the file really holds.

- Peru (SUNAT, padrón reducido del RUC): the daily ZIP of every taxpayer, one pipe-separated text file. Only
  RUCs starting 20 (legal persons: companies, associations, foundations, the state's own bodies) are kept. RUC 10
  and 15 rows are natural persons, and 17 is a non-domiciled person: they are skipped at the first step, before
  any field is mapped. Kept of a legal person: its RUC, name, taxpayer status and fiscal address (street,
  number, interior, block and lot, department from the UBIGEO code). The file's names can hold
  bare double quotes, so it is read with quotes as plain characters.
"""

from __future__ import annotations

import re

from lc_core.directory_id import mint

from .bulk import Profile, _clean
from .bulk_batch3 import Columns, _address, table
from .gleif import Skip

# ---- Peru: SUNAT, padrón reducido del RUC ----

PE_COLUMNS = Columns(
    {"ruc": ("RUC",), "name": ("NOMBRE O RAZÓN SOCIAL", "NOMBRE O RAZON SOCIAL", "RAZON SOCIAL", "RAZÓN SOCIAL"),
     "status": ("ESTADO DEL CONTRIBUYENTE", "ESTADO")},
    {"ubigeo": ("UBIGEO",), "roadType": ("TIPO DE VÍA", "TIPO DE VIA"), "road": ("NOMBRE DE VÍA", "NOMBRE DE VIA"),
     "zoneCode": ("CÓDIGO DE ZONA", "CODIGO DE ZONA"), "zoneType": ("TIPO DE ZONA",),
     "number": ("NÚMERO", "NUMERO"), "interior": ("INTERIOR",), "lot": ("LOTE",),
     "flat": ("DEPARTAMENTO",), "block": ("MANZANA",), "km": ("KILÓMETRO", "KILOMETRO")})

# The first two digits of a UBIGEO code: Peru's departments (INEI).
PE_DEPARTMENTS = {
    "01": "Amazonas", "02": "Áncash", "03": "Apurímac", "04": "Arequipa", "05": "Ayacucho", "06": "Cajamarca",
    "07": "Callao", "08": "Cusco", "09": "Huancavelica", "10": "Huánuco", "11": "Ica", "12": "Junín",
    "13": "La Libertad", "14": "Lambayeque", "15": "Lima", "16": "Loreto", "17": "Madre de Dios", "18": "Moquegua",
    "19": "Pasco", "20": "Piura", "21": "Puno", "22": "San Martín", "23": "Tacna", "24": "Tumbes", "25": "Ucayali"}

BLANK = {"", "-", "--", "----"}


def _part(value) -> str:
    value = _clean(value)
    return "" if value in BLANK else value


def pe_record(row: dict) -> dict:
    ruc = _clean(row.get("ruc"))
    if not ruc.startswith("20"):
        raise Skip("natural person (RUC 10, 15 or 17)")
    name = _clean(row.get("name"))
    if not re.fullmatch(r"20\d{9}", ruc) or not name:
        raise Skip("no number or name")
    street = " ".join(p for p in (_part(row.get("roadType")), _part(row.get("road"))) if p)
    line = ", ".join(p for p in (
        " ".join(p for p in (street, _part(row.get("number"))) if p),
        f"Int. {_part(row.get('interior'))}" if _part(row.get("interior")) else "",
        f"Dpto. {_part(row.get('flat'))}" if _part(row.get("flat")) else "",
        f"Mz. {_part(row.get('block'))}" if _part(row.get("block")) else "",
        f"Lt. {_part(row.get('lot'))}" if _part(row.get("lot")) else "",
        f"Km. {_part(row.get('km'))}" if _part(row.get("km")) else "",
        " ".join(p for p in (_part(row.get("zoneType")), _part(row.get("zoneCode"))) if p)) if p)
    ubigeo = _part(row.get("ubigeo"))
    return {
        "directoryId": mint("PE", "PE-RUC", ruc),
        "legalName": name,
        "legalJurisdiction": "PE",
        "registrationAuthority": "PE-SUNAT",
        "registrationNumber": ruc,
        "legalStatus": _part(row.get("status")) or None,
        "legalAddress": _address(addressLines=[line] if line else [],
                                 region=PE_DEPARTMENTS.get(ubigeo[:2], "") if re.fullmatch(r"\d{6}", ubigeo) else "",
                                 country="PE"),
        "recordMode": "mirror",
    }


PERU_SUNAT = Profile(
    source="pe-sunat-ruc", job="ingest-pe-ruc", store="pe-ruc", filename="pe-sunat-padron-reducido", extension=".zip",
    file_url="https://www2.sunat.gob.pe/padron_reducido_ruc.zip", guard_names=True, ra="PE-RUC",
    read=table(PE_COLUMNS, fallback_encoding="cp1252", literal_quotes=True), mapper=pe_record)

BATCH4 = (PERU_SUNAT,)
