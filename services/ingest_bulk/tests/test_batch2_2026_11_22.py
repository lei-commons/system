"""Sources approved by the owner on 2026-11-22, batch 2, all on trial first: the Australian ABN extract, France's
RNA, Argentina's RNS and Taiwan's tax registrations. Files are synthetic, in the layouts the publishers document;
the first trial run confirms them (a mismatch names the columns or tags, never values)."""

import csv
import io
import zipfile

import pytest

from lc_core.directory_id import mint
from lc_ingest.bulk import (ARGENTINA, AUSTRALIA_ABN, FRANCE_RNA, IRELAND, PROFILES, TAIWAN_TAX, _roc_date, abn_xml,
                            ar_csv, rna_locate)
from lc_ingest.gleif import Skip


def zipped(files: dict):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text.encode("utf-8") if isinstance(text, str) else text)
    out.seek(0)
    return out


def mapped(profile, rows):
    kept, why = [], []
    for row in rows:
        try:
            kept.append(profile.record(row))
        except Skip as exc:
            why.append(str(exc))
    return kept, why


ABN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Transfer>
<ABR recordLastUpdatedDate="20261101" replaced="N"><ABN status="ACT" ABNStatusFromDate="20000101">51824753556</ABN>
 <EntityType><EntityTypeInd>DTT</EntityTypeInd><EntityTypeText>Discretionary Trading Trust</EntityTypeText></EntityType>
 <MainEntity><NonIndividualName type="MN"><NonIndividualNameText>THE TRUSTEE FOR ACME TRUST</NonIndividualNameText></NonIndividualName>
 <BusinessAddress><AddressDetails><State>NSW</State><Postcode>2000</Postcode></AddressDetails></BusinessAddress></MainEntity>
 <OtherEntity><NonIndividualName type="TRD"><NonIndividualNameText>ACME TRADING</NonIndividualNameText></NonIndividualName></OtherEntity>
</ABR>
<ABR recordLastUpdatedDate="20261101" replaced="N"><ABN status="ACT" ABNStatusFromDate="20000101">11111111111</ABN>
 <EntityType><EntityTypeInd>IND</EntityTypeInd><EntityTypeText>Individual/Sole Trader</EntityTypeText></EntityType>
 <LegalEntity><IndividualName type="LGL"><GivenName>JANE</GivenName><FamilyName>CITIZEN</FamilyName></IndividualName></LegalEntity>
</ABR>
<ABR recordLastUpdatedDate="20261101" replaced="N"><ABN status="CAN" ABNStatusFromDate="20100101">22222222222</ABN>
 <EntityType><EntityTypeInd>PRV</EntityTypeInd><EntityTypeText>Australian Private Company</EntityTypeText></EntityType>
 <MainEntity><NonIndividualName type="MN"><NonIndividualNameText>ACME PTY LTD</NonIndividualNameText></NonIndividualName></MainEntity>
 <ASICNumber ASICNumberType="undetermined">123456789</ASICNumber>
</ABR>
<ABR recordLastUpdatedDate="20261101" replaced="N"><ABN status="CAN" ABNStatusFromDate="20100101">33333333333</ABN>
 <EntityType><EntityTypeInd>FPT</EntityTypeInd><EntityTypeText>Family Partnership</EntityTypeText></EntityType>
 <MainEntity><NonIndividualName type="MN"><NonIndividualNameText>J &amp; K CITIZEN</NonIndividualNameText></NonIndividualName></MainEntity>
</ABR>
<ABR recordLastUpdatedDate="20261101" replaced="N"><ABN status="ACT" ABNStatusFromDate="20200101">44444444444</ABN>
 <EntityType><EntityTypeInd>DES</EntityTypeInd><EntityTypeText>Deceased Estate</EntityTypeText></EntityType>
 <MainEntity><NonIndividualName type="MN"><NonIndividualNameText>THE ESTATE OF JOHN CITIZEN</NonIndividualNameText></NonIndividualName></MainEntity>
</ABR>
</Transfer>"""


def test_abn_keeps_organisations_not_people_partnerships_or_asic_companies():
    rows = list(abn_xml(zipped({"20261101_Public01.xml": ABN_XML})))
    kept, why = mapped(AUSTRALIA_ABN, rows)
    assert [r["registrationNumber"] for r in kept] == ["51824753556"]
    assert why == ["natural person, partnership or deceased estate",
                   "has an ASIC number: in the directory through the ASIC dataset",
                   "natural person, partnership or deceased estate", "natural person, partnership or deceased estate"]
    r = kept[0]
    assert r["directoryId"] == mint("AU", "AU-ABN", "51824753556") and r["legalForm"] == "Discretionary Trading Trust"
    assert r["otherNames"] == ["ACME TRADING"] and r["legalStatus"] == "Active"
    assert r["legalAddress"] == {"region": "NSW", "postalCode": "2000", "country": "AU"}
    assert "CITIZEN" not in repr(kept) and "JANE" not in repr(kept)  # the individual and the partners never come through


def test_abn_stops_with_tag_names_if_the_layout_changed():
    with pytest.raises(ValueError, match=r"no ABR records; the file has the tags \['Entity', 'Name'"):
        list(abn_xml(zipped({"a.xml": "<Name><Entity>SECRET CO</Entity></Name>"})))


def test_abn_files_are_found_in_the_catalogue_after_the_gate():
    class Gateway:
        asked = []

        def get_json(self, url):
            self.asked.append(url)
            return {"result": {"resources": [
                {"url": "https://data.gov.au/data/dataset/x/resource/1/download/public_split_1_10.zip"},
                {"url": "https://data.gov.au/data/dataset/x/resource/2/download/public_split_11_20.zip"}]}}

    names = [name for name, _ in AUSTRALIA_ABN.parts]
    assert names == ["split-1-10", "split-11-20"]
    assert AUSTRALIA_ABN.parts[1][1](Gateway()).endswith("public_split_11_20.zip")


RNA_HEADER = ("id;id_ex;siret;rup_mi;gestion;date_creat;date_decla;date_publi;date_disso;nature;groupement;titre;"
              "titre_court;objet;objet_social1;objet_social2;adrs_complement;adrs_numvoie;adrs_repetition;adrs_typevoie;"
              "adrs_libvoie;adrs_distrib;adrs_codeinsee;adrs_codepostal;adrs_libcommune;adrg_declarant;adrg_complemid;"
              "adrg_complemgeo;adrg_libvoie;adrg_distrib;adrg_codepostal;adrg_achemine;adrg_pays;dir_civilite;siteweb;"
              "publiweb;observation;position;maj_time\n")


def rna_line(number, title, position="A", disso="0001-01-01"):
    return (f"{number};;;;;1987-03-12;2015-01-01;2015-02-01;{disso};D;S;{title};;Culture;;;;12;;RUE;DES LILAS;;"
            f"75111;75011;PARIS;;M. DUPONT;;3 AVENUE PRIVEE;;69001;LYON;FRANCE;M;;;;{position};2026-10-01\n")


def test_rna_keeps_city_and_postcode_never_the_managers_address_or_street():
    archive = zipped({"rna_waldec_20261101_dpt_75.csv": RNA_HEADER + rna_line("W751000001", "AMIS DU QUARTIER"),
                      "rna_waldec_20261101_dpt_69.csv": RNA_HEADER + rna_line("W691000002", "CLUB DE BOULES", "D",
                                                                              "2020-06-30")})
    kept, why = mapped(FRANCE_RNA, FRANCE_RNA.read(archive))
    assert [r["registrationNumber"] for r in kept] == ["W691000002", "W751000001"]  # members in name order
    paris = kept[1]
    assert paris["directoryId"] == mint("FR", "FR-RNA", "W751000001") and paris["legalForm"] == "Association déclarée"
    assert paris["legalAddress"] == {"city": "PARIS", "postalCode": "75011", "country": "FR"}
    assert paris["incorporationDate"] == "1987-03-12" and "dissolutionDate" not in paris
    assert kept[0]["legalStatus"] == "Dissolved" and kept[0]["dissolutionDate"] == "2020-06-30"
    assert not any(x in repr(kept) for x in ("DUPONT", "AVENUE PRIVEE", "LILAS", "69001"))


def test_rna_picks_the_newest_waldec_file():
    class Gateway:
        def get_json(self, url):
            return {"resources": [{"title": "rna_import_20261101.zip", "url": "https://media.interieur.gouv.fr/rna/i.zip"},
                                  {"title": "rna_waldec_20261001.zip", "url": "https://media.interieur.gouv.fr/rna/a.zip"},
                                  {"title": "rna_waldec_20261101.zip", "url": "https://media.interieur.gouv.fr/rna/b.zip"}]}
    assert rna_locate(Gateway()) == ("rna_waldec_20261101", "https://media.interieur.gouv.fr/rna/b.zip")


AR_HEADER = ("cuit,razon_social,fecha_hora_contrato_social,tipo_societario,fecha_actualizacion,numero_inscripcion,"
             "dom_legal_provincia,dom_legal_localidad,dom_legal_calle,dom_legal_numero,dom_legal_cp\n")


def test_argentina_keeps_companies_and_drops_natural_persons():
    text = (AR_HEADER + "30712345679,ACME S.A.,2008-05-12 00:00:00,SOCIEDAD ANONIMA,2026-10-01,123,"
                        "BUENOS AIRES,LA PLATA,CALLE 7,1234,B1900\n"
            + "20123456786,JUAN PEREZ,2010-01-01 00:00:00,,2026-10-01,,,,,,\n")
    for body in (io.BytesIO(text.encode()), zipped({"rns_2026.csv": text})):  # plain or zipped
        kept, why = mapped(ARGENTINA, ar_csv(body))
        assert [r["registrationNumber"] for r in kept] == ["30712345679"] and why == ["natural person"]
    r = kept[0]
    assert r["directoryId"] == mint("AR", "AR-CUIT", "30712345679") and r["incorporationDate"] == "2008-05-12"
    assert r["legalAddress"] == {"addressLines": ["CALLE 7 1234"], "city": "LA PLATA", "region": "BUENOS AIRES",
                                 "postalCode": "B1900", "country": "AR"}


AR_RESOURCES = {"result": {"resources": [
    {"id": "6096331b", "name": "Registro Nacional de Sociedades - Muestreo", "format": "CSV",
     "last_modified": "2024-12-06T16:14:14", "url": "https://datos.jus.gob.ar/x/muestreo.csv"},
    {"id": "61987339", "name": "Registro Nacional de Sociedades - Asociaciones sin fines de lucro - 20261118",
     "format": "CSV", "last_modified": "2026-11-20T11:26:09", "url": "https://datos.jus.gob.ar/x/asociaciones.csv"},
    {"id": "13a1a66f", "name": "Registro Nacional de Sociedades - 2026", "format": "ZIP",
     "last_modified": "2026-09-18T11:26:19", "url": "https://datos.jus.gob.ar/x/rns-2026.zip"},
    {"id": "a0301845", "name": "Registro Nacional de Sociedades - Asociaciones sin fines de lucro - 2026", "format": "ZIP",
     "last_modified": "2026-11-20T11:24:14", "url": "https://datos.jus.gob.ar/x/asociaciones-2026.zip"},
    {"id": "cf93b46f", "name": "Registro Nacional de Sociedades - 2025", "format": "ZIP",
     "last_modified": "2026-03-02T13:20:45", "url": "https://datos.jus.gob.ar/x/rns-2025.zip"},
    {"id": "6aade8f9", "name": "Registro Nacional de Sociedades - Gráficos estadísticos", "format": "gráfico",
     "url": "https://public.tableau.com/x"},
    {"id": "1c5fa113", "name": "Registro Nacional de Sociedades - 2026 - Tercer bimestre", "format": "ZIP",
     "last_modified": "2026-08-12T09:50:09", "url": "https://datos.jus.gob.ar/x/rns-2026-b3.zip"}]}}


def test_argentina_reads_the_newest_yearly_companies_file_and_the_newest_non_profits_file():
    """The first trial (2026-11-22) read the newest resource, which was the non-profits file: no companies at all."""
    class Gateway:
        def get_json(self, url):
            return AR_RESOURCES
    names = [name for name, _ in ARGENTINA.parts]
    assert names == ["companies", "non-profits"] and ARGENTINA.locate is None
    assert ARGENTINA.parts[0][1](Gateway()) == "https://datos.jus.gob.ar/x/rns-2026.zip"
    assert ARGENTINA.parts[1][1](Gateway()) == "https://datos.jus.gob.ar/x/asociaciones.csv"


def test_argentina_keeps_one_row_per_cuit_the_newest_member_first():
    old = AR_HEADER + "30712345679,ACME VIEJA S.A.,2008-05-12 00:00:00,SOCIEDAD ANONIMA,2026-01-01,,,,,,\n"
    new = (AR_HEADER + "30712345679,ACME S.A.,2008-05-12 00:00:00,SOCIEDAD ANONIMA,2026-09-01,,,,,,\n"
           + "30712345679,ACME S.A.,2008-05-12 00:00:00,SOCIEDAD ANONIMA,2026-09-01,,,,,,\n"
           + "30500000007,OTRA S.R.L.,2001-01-01 00:00:00,SOCIEDAD DE RESPONSABILIDAD LIMITADA,2026-09-01,,,,,,\n")
    kept, _ = mapped(ARGENTINA, ar_csv(zipped({"rns_202601.csv": old, "rns_202609.csv": new})))
    assert [(r["registrationNumber"], r["legalName"]) for r in kept] == [("30712345679", "ACME S.A."),
                                                                          ("30500000007", "OTRA S.R.L.")]
    plain, _ = mapped(ARGENTINA, ar_csv(io.BytesIO(new.encode())))
    assert len(plain) == 2


def test_trial_profiles_drop_a_name_holding_an_email_or_phone():
    text = (AR_HEADER + "30712345679,ASOC. AMIGOS TEL 011 4555-1234,2008-05-12 00:00:00,ASOCIACION CIVIL,,,,,,,\n"
            + "30500000007,CLUB info@club.org.ar,2001-01-01 00:00:00,ASOCIACION CIVIL,,,,,,,\n"
            + "30600000004,CLUB ATLETICO 1905,2001-01-01 00:00:00,ASOCIACION CIVIL,,,,,,,\n")
    kept, why = mapped(ARGENTINA, ar_csv(io.BytesIO(text.encode())))
    assert [r["registrationNumber"] for r in kept] == ["30600000004"]
    assert why == ["name holds an email address or phone number"] * 2
    for source in ("us-irs-eo-bmf", "gb-ccew", "au-abr-abn", "fr-rna", "ar-rns", "tw-mof-tax"):
        assert PROFILES[source].guard_names
    assert not IRELAND.guard_names  # production profiles are unchanged


def test_trial_profiles_leave_out_a_contact_like_value_outside_the_name_and_keep_the_record():
    text = (AR_HEADER
            + "30712345679,ACME S.A.,2008-05-12 00:00:00,SOCIEDAD ANONIMA,,,BUENOS AIRES,CABA,"
              "AV CORRIENTES TEL 011 4555-1234,1234,C1043\n"
            + "30500000007,OTRA S.R.L.,2001-01-01 00:00:00,SOCIEDAD ANONIMA,,,info@otra.com.ar,CABA,"
              "AV DE MAYO,500,C1084\n"
            + "30600000004,CLUB ATLETICO 1905,2001-01-01 00:00:00,ASOCIACION CIVIL,,,CORDOBA,CORDOBA,"
              "CALLE 9 DE JULIO,1905,X5000\n")
    kept, why = mapped(ARGENTINA, ar_csv(io.BytesIO(text.encode())))
    assert why == [] and [r["registrationNumber"] for r in kept] == ["30712345679", "30500000007", "30600000004"]
    first, second, third = (r["legalAddress"] for r in kept)
    assert "addressLines" not in first and first["city"] == "CABA" and first["postalCode"] == "C1043"
    assert "region" not in second and second["addressLines"] == ["AV DE MAYO 500"]
    assert third["addressLines"] == ["CALLE 9 DE JULIO 1905"] and third["region"] == "CORDOBA"
    # Identifiers are never mistaken for phone numbers, and dates stay.
    assert kept[0]["registrationNumber"] == "30712345679" and kept[0]["incorporationDate"] == "2008-05-12"


def test_taiwan_drops_sole_proprietorships_and_partnerships_and_reads_roc_dates():
    header = "營業地址,統一編號,總機構統一編號,營業人名稱,資本額,設立日期,組織別名稱,使用統一發票,行業代號,名稱\n"
    text = (header + "臺北市中正區重慶南路一段2號,12345675,,台灣測試股份有限公司,1000000,1050101,股份有限公司,Y,461411,批發\n"
            + "臺北市大安區,23456789,,王小明商行,10000,0990315,獨資,Y,471911,零售\n"
            + "臺中市,34567891,,好朋友合夥,20000,1001231,合夥,N,,\n")
    kept, why = mapped(TAIWAN_TAX, TAIWAN_TAX.read(zipped({"BGMOPEN1.csv": text})))
    assert [r["registrationNumber"] for r in kept] == ["12345675"] and why == ["sole proprietorship or partnership"] * 2
    r = kept[0]
    assert r["directoryId"] == mint("TW", "TW-UBN", "12345675") and r["incorporationDate"] == "2016-01-01"
    assert r["legalStatus"] == "Operating" and r["industryCodes"] == ["461411"]
    assert _roc_date("990315") == "2010-03-15" and _roc_date("x") is None


def test_batch_2_is_registered_on_trial():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    for source in ("au-abr-abn", "fr-rna", "ar-rns", "tw-mof-tax"):
        assert source in PROFILES and built[source]["job"] == PROFILES[source].job
        assert built[source]["stage"] == ("production" if source in ("tw-mof-tax", "fr-rna") else "trial")  # promoted 2026-11-22, 2026-11-25
