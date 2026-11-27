"""Sources approved by the owner on 2026-11-26, batch 4, on trial first: Peru's SUNAT padrón reducido del RUC. The
file is synthetic, in the layout SUNAT documents; the first trial run confirms it (a mismatch names the columns,
never values)."""

import io
import zipfile

from lc_core.directory_id import mint
from lc_ingest.bulk_batch4 import PERU_SUNAT
from lc_ingest.gleif import Skip
from lc_ingest.profiles import PROFILES

PE = ("RUC|NOMBRE O RAZÓN SOCIAL|ESTADO DEL CONTRIBUYENTE|CONDICIÓN DE DOMICILIO|UBIGEO|TIPO DE VÍA|NOMBRE DE VÍA|"
      "CÓDIGO DE ZONA|TIPO DE ZONA|NÚMERO|INTERIOR|LOTE|DEPARTAMENTO|MANZANA|KILÓMETRO|\n"
      "20100047218|BANCO DE CREDITO DEL PERU|ACTIVO|HABIDO|150114|CAL.|CENTENARIO|-|URB.|156|-|-|-|-|-|\n"
      "20601234567|INVERSIONES \"EL SOL\" S.A.C.|BAJA DE OFICIO|NO HABIDO|040101|AV.|EJERCITO|-|-|-|-|5|-|B|-|\n"
      "10456789012|PEREZ GOMEZ JUAN|ACTIVO|HABIDO|150101|JR.|LIMA|-|-|100|-|-|-|-|-|\n"
      "15012345678|SMITH JOHN|ACTIVO|HABIDO|150101|-|-|-|-|-|-|-|-|-|-|\n"
      "17123456789|DOE JANE|ACTIVO|HABIDO|-|-|-|-|-|-|-|-|-|-|-|\n"
      "20555555555|ventas@example.pe|ACTIVO|HABIDO|150101|-|-|-|-|-|-|-|-|-|-|\n"
      "20777777777|SMITH Y ASOCIADOS S.R.L.|ACTIVO|HABIDO|130101|AV.|ESPAÑA|-|-|200|3|-|-|-|-|\n")


def zipped(name: str, data: bytes):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, data)
    out.seek(0)
    return out


def mapped(rows):
    kept, why = [], []
    for row in rows:
        try:
            kept.append(PERU_SUNAT.record(row))
        except Skip as exc:
            why.append(str(exc))
    return kept, why


def test_peru_keeps_legal_persons_only_and_reads_bare_quotes_in_a_windows_file():
    rows = list(PERU_SUNAT.read(zipped("padron_reducido_ruc.txt", PE.encode("cp1252"))))
    kept, why = mapped(rows)
    assert [r["registrationNumber"] for r in kept] == ["20100047218", "20601234567", "20777777777"]
    assert why.count("natural person (RUC 10, 15 or 17)") == 3
    assert why.count("name holds an email address or phone number") == 1
    bank, sol, smith = kept
    assert bank["directoryId"] == mint("PE", "PE-RUC", "20100047218")
    assert bank["legalStatus"] == "ACTIVO" and bank["legalJurisdiction"] == "PE"
    assert bank["legalAddress"] == {"addressLines": ["CAL. CENTENARIO 156, URB."], "region": "Lima", "country": "PE"}
    assert sol["legalName"] == 'INVERSIONES "EL SOL" S.A.C.'  # a bare quote is a plain character
    assert sol["legalAddress"] == {"addressLines": ["AV. EJERCITO, Mz. B, Lt. 5"], "region": "Arequipa", "country": "PE"}
    assert smith["legalName"] == "SMITH Y ASOCIADOS S.R.L."  # people's names in a company's name are fine
    assert smith["legalAddress"]["addressLines"] == ["AV. ESPAÑA 200, Int. 3"]


def test_peru_never_maps_a_natural_persons_row():
    for ruc in ("10456789012", "15012345678", "17123456789"):
        try:
            PERU_SUNAT.record({"ruc": ruc, "name": "X", "status": "ACTIVO", "ubigeo": "150101", "road": "LIMA"})
        except Skip as exc:
            assert "natural person" in str(exc)
        else:
            raise AssertionError(f"{ruc} was mapped")


def test_peru_skips_a_row_a_pipe_in_its_name_has_shifted():
    shifted = PE + "20888888888|INVERSIONES|LEGAL PARTNER CORPORATION S.A.C.|ACTIVO|HABIDO|150101|-|-|-|-|-|-|-|-|-|-|\n"
    kept, why = mapped(PERU_SUNAT.read(zipped("padron_reducido_ruc.txt", shifted.encode("cp1252"))))
    assert [r["registrationNumber"] for r in kept] == ["20100047218", "20601234567", "20777777777"]
    assert why.count("a | inside a value shifted the row's columns") == 1


def test_peru_every_word_of_the_first_trial_run_has_a_category():
    from lc_core.status import default

    words = {  # as the file spells them, cut at 20 characters (trial run of 2026-11-27)
        "ACTIVO": "active", "PENDIENTE DE INI. DE": "active",
        "SUSPENSION TEMPORAL": "inactive", "BAJA PROVISIONAL": "inactive", "BAJA PROV. POR OFICI": "inactive",
        "INHABILITADO-VENT.UN": "inactive", "ANUL.PROVI.-ACTO ILI": "inactive",
        "BAJA DE OFICIO": "struck-off", "BAJA DEFINITIVA": "struck-off", "BAJA NO ACTIV. SISEV": "struck-off",
        "BAJA MULT.INSCR. Y O": "struck-off", "ANULACION - ERROR SU": "struck-off",
        "OTROS OBLIGADOS": "unknown", "NUM. INTERNO IDENTIF": "unknown"}
    status = default()
    assert {w: status.category("pe-sunat-ruc", w) for w in words} == {w: (c, True) for w, c in words.items()}


def test_batch_4_is_registered_on_trial():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    assert PROFILES["pe-sunat-ruc"] is PERU_SUNAT and PERU_SUNAT.guard_names
    assert built["pe-sunat-ruc"]["job"] == PERU_SUNAT.job and built["pe-sunat-ruc"]["stage"] == "trial"
