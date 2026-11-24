"""Sources approved by the owner on 2026-11-22, batch 3, all on trial first: Cyprus, Slovenia, the Dutch ANBI list,
Scotland's OSCR, Canada's CRA charities and the US NPPES organisations. Files are synthetic, in the layouts the
publishers document; the first trial run confirms them (a mismatch names the columns, never values)."""

import gzip
import io
import zipfile
from datetime import date

import pytest

from lc_core.directory_id import mint
from lc_ingest.bulk_batch3 import (CANADA_CRA, CYPRUS, NETHERLANDS_ANBI, SCOTLAND_OSCR, SLOVENIA, US_NPPES, cra_locate,
                                   nppes_locate, table, Columns)
from lc_ingest.gleif import Skip
from lc_ingest.profiles import PROFILES


def zipped(files: dict):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files.items():
            z.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
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


# ---- the shared reader ----

def test_the_reader_finds_columns_by_any_documented_name_delimiter_and_encoding():
    columns = Columns({"n": ("Number", "Ident_stevilka"), "name": ("Name",)}, {"city": ("Poštni kraj",)})
    text = "IDENT_STEVILKA;name;Poštni kraj;Other\n5012345000;Čebela d.o.o.;Ljubljana;x\n"
    for raw in (text.encode("cp1250"), text.encode("utf-8")):
        rows = list(table(columns, fallback_encoding="cp1250")(io.BytesIO(raw)))
        assert rows == [{"n": "5012345000", "name": "Čebela d.o.o.", "city": "Ljubljana"}]
    zrows = list(table(columns)(zipped({"readme.txt": "x", "data.csv": text})))
    assert zrows[0]["name"] == "Čebela d.o.o."
    grows = list(table(columns)(io.BytesIO(gzip.compress(text.encode()))))
    assert grows[0]["city"] == "Ljubljana"


def test_the_reader_stops_with_column_names_only():
    columns = Columns({"n": ("Number",)})
    with pytest.raises(ValueError, match=r"no column for \['n'\].*the file has \['Name', 'City'\]"):
        list(table(columns)(io.BytesIO(b"Name,City\nSECRET CO,Town\n")))


# ---- Cyprus ----

CY = ("ORGANISATION_NAME,REGISTRATION_NO,ORGANISATION_TYPE_CODE,ORGANISATION_TYPE,ORGANISATION_SUB_TYPE,"
      "NAME_STATUS_CODE,NAME_STATUS,REGISTRATION_DATE,ORGANISATION_STATUS,ORGANISATION_STATUS_DATE,ADDRESS_SEQ_NO\n"
      "ACME LIMITED,12345,C,Εταιρεία,Ιδιωτική,L,Τελευταίο Όνομα,12/03/2010,Εγγεγραμμένη,,1\n"
      "ACME OLD NAME LIMITED,12345,C,Εταιρεία,Ιδιωτική,P,Προηγούμενο Όνομα,12/03/2010,Εγγεγραμμένη,,1\n"
      "FOREIGN CO INC,777,O,Αλλοδαπή Εταιρεία,,L,Τελευταίο Όνομα,01/01/2015,Διαγραμμένη,30/06/2020,2\n"
      "ΓΙΩΡΓΟΣ ΠΑΠΑΔΟΠΟΥΛΟΣ,999,B,Εμπορική Επωνυμία,,L,Τελευταίο Όνομα,01/01/2018,Εγγεγραμμένη,,3\n"
      "Α & Β ΣΥΝΕΤΑΙΡΟΙ,888,P,Συνεταιρισμός,,L,Τελευταίο Όνομα,01/01/2018,Εγγεγραμμένη,,4\n")


def test_cyprus_keeps_companies_under_their_register_number_and_drops_business_names_and_partnerships():
    kept, why = mapped(CYPRUS, CYPRUS.read(io.BytesIO(CY.encode("utf-8"))))
    assert [r["registrationNumber"] for r in kept] == ["HE12345", "AE777"]
    assert why == ["an earlier name of a company", "business name or partnership (usually people)",
                   "business name or partnership (usually people)"]
    acme, foreign = kept
    assert acme["directoryId"] == mint("CY", "RA000161", "HE12345") and acme["incorporationDate"] == "2010-03-12"
    assert acme["legalForm"] == "Εταιρεία - Ιδιωτική" and "dissolutionDate" not in acme
    assert foreign["legalStatus"] == "Διαγραμμένη" and foreign["dissolutionDate"] == "2020-06-30"
    assert "ΠΑΠΑΔΟΠΟΥΛΟΣ" not in repr(kept)


def test_cyprus_reads_a_greek_windows_file_too():
    kept, _ = mapped(CYPRUS, CYPRUS.read(io.BytesIO(CY.encode("cp1253"))))
    assert kept[0]["legalForm"] == "Εταιρεία - Ιδιωτική"


# ---- Slovenia ----

SI = ("Ident_stevilka;Popolno_ime;Kratko_ime;Oblika;Skrajsano_ime;Postna_stevilka;Postni_kraj;Datum_vpisa;Skd\n"
      "5012345000;ČEBELA, proizvodnja medu, d.o.o.;ČEBELA d.o.o.;Družba z omejeno odgovornostjo;;1000;LJUBLJANA;"
      "15.03.1995;A01.490\n"
      "5012345001;ČEBELA d.o.o., podružnica Maribor;;Podružnica;;2000;MARIBOR;01.01.2000;A01.490\n"
      "6012345000;JANEZ NOVAK s.p.;;Samostojni podjetnik posameznik;JANEZ NOVAK s.p.;4000;KRANJ;01.01.2010;\n"
      "7012345000;MARIJA KOVAČ;;Fizična oseba - sobodajalec;;6320;PORTOROŽ;01.01.2012;\n")


def test_slovenia_keeps_businesses_and_drops_sole_traders_room_renters_and_branches():
    kept, why = mapped(SLOVENIA, SLOVENIA.read(io.BytesIO(SI.encode("cp1250"))))
    assert [r["registrationNumber"] for r in kept] == ["5012345000"]
    assert why == ["a branch unit of a registered business", "sole trader or private room renter (people)",
                   "sole trader or private room renter (people)"]
    r = kept[0]
    assert r["directoryId"] == mint("SI", "SI-PRS", "5012345000") and r["otherNames"] == ["ČEBELA d.o.o."]
    assert r["legalAddress"] == {"city": "LJUBLJANA", "postalCode": "1000", "country": "SI"}
    assert r["incorporationDate"] == "1995-03-15" and r["industryCodes"] == ["A01.490"]
    assert "NOVAK" not in repr(kept) and "KOVAČ" not in repr(kept)



def test_slovenia_reads_a_utf16_file_by_its_byte_order_mark():
    kept, _ = mapped(SLOVENIA, SLOVENIA.read(io.BytesIO(SI.encode("utf-16"))))
    assert [r["registrationNumber"] for r in kept] == ["5012345000"]
    assert kept[0]["legalName"] == "ČEBELA, proizvodnja medu, d.o.o." and kept[0]["legalAddress"]["city"] == "LJUBLJANA"


# ---- Netherlands ANBI ----

def xlsx(rows):
    """A minimal workbook: shared strings for text, numbers inline."""
    strings, cells = [], []
    for r, row in enumerate(rows, 1):
        xml = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, (int, float)):
                xml.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                strings.append(value)
                xml.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        cells.append(f'<row r="{r}">{"".join(xml)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    shared = f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>"
    sheet = f'<worksheet {ns}><sheetData>{"".join(cells)}</sheetData></worksheet>'
    book = io.BytesIO()
    with zipfile.ZipFile(book, "w") as z:
        z.writestr("xl/workbook.xml", f"<workbook {ns}/>")
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return book.getvalue()


def test_anbi_reads_the_workbook_in_the_zip():
    book = xlsx([["fiscaalNummer", "dossierNummer", "naam", "vestigingsPlaats", "webSite", "ingangsDatum", "aliasNaam"],
                 [2345678, 111, "Stichting Vrienden van het Park", "UTRECHT", "https://vrienden.example", "2010-01-01",
                  "Parkvrienden"],
                 ["804512345", 112, "Vereniging Muziek &amp; Zo", "Zwolle", "", "", ""]])
    kept, why = mapped(NETHERLANDS_ANBI, NETHERLANDS_ANBI.read(zipped({"anbi.xlsx": book})))
    assert [r["registrationNumber"] for r in kept] == ["002345678", "804512345"] and why == []
    park = kept[0]
    assert park["directoryId"] == mint("NL", "NL-RSIN", "002345678") and park["otherNames"] == ["Parkvrienden"]
    assert park["legalAddress"] == {"city": "UTRECHT", "country": "NL"} and park["website"] == "https://vrienden.example"
    assert kept[1]["legalName"] == "Vereniging Muziek & Zo" and kept[1]["legalStatus"] == "ANBI"


def test_anbi_names_the_workbook_columns_when_they_differ():
    book = xlsx([["Something", "Else"], ["SECRET", "x"]])
    with pytest.raises(ValueError, match=r"the file has \['Something', 'Else'\]"):
        list(NETHERLANDS_ANBI.read(zipped({"anbi.xlsx": book})))



ANBI_README = "Toelichting bij het ANBI-bestand.\nVelden: fiscaalNummer, naam, vestigingsPlaats, webSite\n" * 50


def test_anbi_skips_the_readme_and_reads_an_xml_data_file():
    # 2026-11-23/24: the archive held README.txt beside a data file that was neither .xlsx nor .csv.
    xml = ('<?xml version="1.0" encoding="UTF-8"?><anbis xmlns="urn:example">'
           '<anbi><fiscaalNummer>2345678</fiscaalNummer><naam>Stichting Vrienden van het Park</naam>'
           '<vestigingsPlaats>UTRECHT</vestigingsPlaats><webSite>https://vrienden.example</webSite></anbi>'
           '<anbi><fiscaalNummer>804512345</fiscaalNummer><naam>Vereniging Muziek &amp; Zo</naam>'
           '<vestigingsPlaats>Zwolle</vestigingsPlaats><webSite/></anbi></anbis>')
    kept, why = mapped(NETHERLANDS_ANBI, NETHERLANDS_ANBI.read(zipped({"README.txt": ANBI_README, "anbi.xml": xml})))
    assert [r["registrationNumber"] for r in kept] == ["002345678", "804512345"] and why == []
    assert kept[0]["website"] == "https://vrienden.example" and kept[1]["legalName"] == "Vereniging Muziek & Zo"


def test_anbi_reads_a_json_data_file_and_a_csv_beside_a_bigger_readme():
    doc = '{"meta": {"n": 1}, "anbis": [{"fiscaalNummer": "804512345", "naam": "Stichting Een", "webSite": null}]}'
    kept, _ = mapped(NETHERLANDS_ANBI, NETHERLANDS_ANBI.read(zipped({"README.txt": ANBI_README, "anbi.json": doc})))
    assert [r["legalName"] for r in kept] == ["Stichting Een"]
    csv_text = "fiscaalNummer;naam\n804512345;Stichting Twee\n"
    kept, _ = mapped(NETHERLANDS_ANBI, NETHERLANDS_ANBI.read(zipped({"README.txt": ANBI_README, "anbi.csv": csv_text})))
    assert [r["legalName"] for r in kept] == ["Stichting Twee"]


def test_anbi_without_a_data_file_names_the_archive_members_only():
    with pytest.raises(ValueError, match=r"no .xlsx, .csv, .xml or .json data file.*README.txt.*anbi.ods"):
        list(NETHERLANDS_ANBI.read(zipped({"README.txt": ANBI_README, "anbi.ods": "SECRET"})))


# ---- Scotland OSCR ----

OSCR = ('"Charity Number","Charity Name","Registered Date","Known As","Charity Status","Notes","Postcode",'
        '"Constitutional Form","Principal Office/Trustees Address","Website","Main Operating Location",'
        '"Regulatory Type"\n'
        '"SC012345","The Glen Trust","01/01/1992","Glen Trust","Active","","EH1 1AA","Trust (founding document is a deed '
        'of trust) (other than educational endowment)","1 Private Road, Edinburgh","www.glentrust.example",'
        '"City of Edinburgh","Standard"\n'
        '"SC023456","Glen Company Ltd","01/01/2001","","Active","","G1 1AA","Company (the charity is registered with '
        'Companies House)","","","Glasgow City","Standard"\n'
        '"SC034567","UK Wide Charity","01/01/2005","","Not Submitted","","","SCIO (Scottish Charitable Incorporated '
        'Organisation)","","","Outwith Scotland","Cross-Border"\n'
        '"SC045678","Highland Pipers SCIO","15/06/2015","","Not Monitored","","IV1 1AA","SCIO (Scottish Charitable '
        'Incorporated Organisation)","3 Croft Lane","","Highland","Standard"\n')


def test_oscr_keeps_charities_not_companies_or_cross_border_and_never_the_trustees_address():
    kept, why = mapped(SCOTLAND_OSCR, SCOTLAND_OSCR.read(io.BytesIO(OSCR.encode())))
    assert [r["registrationNumber"] for r in kept] == ["SC012345", "SC045678"]
    assert why == ["also a company: in the directory through Companies House",
                   "cross-border: in the directory through the Charity Commission"]
    glen = kept[0]
    assert glen["directoryId"] == mint("GB", "GB-SC", "SC012345") and glen["otherNames"] == ["Glen Trust"]
    assert glen["legalAddress"] == {"region": "City of Edinburgh", "country": "GB"}
    assert glen["incorporationDate"] == "1992-01-01" and glen["website"] == "www.glentrust.example"
    assert not any(x in repr(kept) for x in ("Private Road", "Croft Lane", "EH1", "IV1"))


# ---- Canada CRA ----

def test_cra_keeps_charities_with_city_and_province_only():
    text = ("BN,Category,Sub-Category,Designation,Legal Name,Account Name,Address Line 1,Address Line 2,City,Province,"
            "Postal Code,Country\n"
            "107951618RR0001,0030,,C,FRIENDS OF THE LIBRARY,LIBRARY FRIENDS,12 HOME ST,,TORONTO,ON,M5V 1A1,CA\n"
            "123456789RR0001,0010,,B,SMITH FAMILY FOUNDATION,,,,,,,US\n"
            "bad,0010,,C,NO NUMBER,,,,,,,\n")
    kept, why = mapped(CANADA_CRA, CANADA_CRA.read(io.BytesIO(text.encode("cp1252"))))
    assert [r["registrationNumber"] for r in kept] == ["107951618RR0001", "123456789RR0001"]
    assert why == ["no number or name"]
    lib = kept[0]
    assert lib["directoryId"] == mint("CA", "CA-CRA", "107951618RR0001") and lib["legalForm"] == "Charitable organization"
    assert lib["legalAddress"] == {"city": "TORONTO", "region": "ON", "country": "CA"}
    assert "HOME ST" not in repr(kept) and "M5V" not in repr(kept)
    assert "legalAddress" not in kept[1]  # abroad: no address kept


def test_cra_takes_the_newest_years_identification_file():
    class Gateway:
        def get_json(self, url):
            def package(year, modified):
                return {"title_translated": {"en": f"{year} List of charities", "fr": "x"},
                        "resources": [{"name_translated": {"en": "Codes Lists"}, "url": "https://open.canada.ca/c.pdf"},
                                      {"name_translated": {"en": "Identification"}, "last_modified": modified,
                                       "url": f"https://open.canada.ca/data/dataset/x/resource/y/download/ident_{year}.csv"}]}
            return {"result": {"results": [package(2024, "2026-11-15T21:31:22"), package(2025, "2026-11-18T14:21:17"),
                                           {"title": "Something else", "resources": []}]}}
    assert cra_locate(Gateway()) == ("2025 20261118",
                                     "https://open.canada.ca/data/dataset/x/resource/y/download/ident_2025.csv")



def test_cra_resolves_a_relative_link_against_the_catalogue():
    # 2026-11-23/24: the catalogue gave "/data/dataset/.../download/ident_2025_upd..." for an uploaded file.
    class Gateway:
        def get_json(self, url):
            return {"result": {"results": [{"title": "2025 List of charities", "resources": [
                {"name": "Identification", "last_modified": "2026-11-20T10:00:00",
                 "url": "/data/dataset/x/resource/y/download/ident_2025_upd.csv"}]}]}}
    assert cra_locate(Gateway()) == ("2025 20261120",
                                     "https://open.canada.ca/data/dataset/x/resource/y/download/ident_2025_upd.csv")

    class Elsewhere(Gateway):
        def get_json(self, url):
            doc = super().get_json(url)
            doc["result"]["results"][0]["resources"][0]["url"] = "https://example.com/ident.csv"
            return doc
    with pytest.raises(ValueError, match="unexpected place"):
        cra_locate(Elsewhere())


# ---- US NPPES ----

NPPES_HEADER = ['"NPI"', '"Entity Type Code"', '"Replacement NPI"', '"Employer Identification Number (EIN)"',
                '"Provider Organization Name (Legal Business Name)"', '"Provider Last Name (Legal Name)"',
                '"Provider Other Organization Name"', '"Provider Business Practice Location Address City Name"',
                '"Provider Business Practice Location Address State Name"',
                '"Provider Business Practice Location Address Postal Code"',
                '"Provider Business Practice Location Address Country Code (If outside U.S.)"',
                '"Provider Enumeration Date"', '"NPI Deactivation Date"', '"NPI Reactivation Date"',
                '"Authorized Official Last Name"', '"Authorized Official Telephone Number"', '"Is Organization Subpart"']


def nppes_line(*values):
    return ",".join(f'"{v}"' for v in values) + "\n"


def test_nppes_keeps_organisations_only_never_people_or_the_authorised_official():
    text = (",".join(NPPES_HEADER) + "\n"
            + nppes_line("1234567893", "2", "", "<UNAVAIL>", "ACME CLINIC LLC", "", "ACME HEALTH", "BOSTON", "MA",
                         "021081234", "", "05/23/2005", "", "", "SMITH", "6175551234", "N")
            + nppes_line("1234567894", "1", "", "", "", "JONES", "", "BOSTON", "MA", "02108", "", "05/23/2005", "", "",
                         "", "", "")
            + nppes_line("1234567895", "2", "", "", "ACME CLINIC CARDIOLOGY", "", "", "BOSTON", "MA", "02108", "",
                         "05/23/2006", "", "", "DOE", "6175550000", "Y")
            + nppes_line("1234567896", "2", "", "", "CLOSED CLINIC", "", "", "SALEM", "MA", "01970", "", "05/23/2006",
                         "01/15/2020", "", "ROE", "", "N"))
    archive = zipped({"npidata_pfile_20050523-20261109_fileheader.csv": ",".join(NPPES_HEADER),
                      "npidata_pfile_20050523-20261109.csv": text, "othername_pfile.csv": "x"})
    kept, why = mapped(US_NPPES, US_NPPES.read(archive))
    assert [r["registrationNumber"] for r in kept] == ["1234567893", "1234567896"]
    assert why == ["an individual practitioner (a person)", "an organisation subpart, not a separate organisation"]
    acme, closed = kept
    assert acme["directoryId"] == mint("US", "US-NPI", "1234567893") and acme["otherNames"] == ["ACME HEALTH"]
    assert acme["legalAddress"] == {"city": "BOSTON", "region": "MA", "postalCode": "02108", "country": "US"}
    assert acme["legalStatus"] == "Active" and closed["legalStatus"] == "Deactivated"
    assert not any(x in repr(kept) for x in ("SMITH", "JONES", "6175551234", "DOE", "ROE"))


def test_nppes_finds_this_months_file_or_last_months():
    class Gateway:
        def __init__(self, published):
            self.published, self.asked = published, []

        def open(self, url, method="GET"):
            self.asked.append((method, url))
            if url not in self.published:
                raise OSError("404")

            class Conn:
                def close(self):
                    pass
            return Conn(), None

    nov = "https://download.cms.gov/nppes/NPPES_Data_Dissemination_November_2026_V2.zip"
    dec = "https://download.cms.gov/nppes/NPPES_Data_Dissemination_December_2026_V2.zip"
    jan = "https://download.cms.gov/nppes/NPPES_Data_Dissemination_January_2027_V2.zip"
    assert nppes_locate(Gateway({nov}), date(2026, 11, 22)) == ("2026-11", nov)
    gw = Gateway({nov})
    assert nppes_locate(gw, date(2026, 12, 3)) == ("2026-11", nov) and gw.asked[0] == ("HEAD", dec)
    assert nppes_locate(Gateway({dec}), date(2027, 1, 4)) == ("2026-12", dec)
    assert nppes_locate(Gateway({jan}), date(2027, 1, 20)) == ("2027-01", jan)
    with pytest.raises(ValueError, match="no NPPES full file"):
        nppes_locate(Gateway(set()), date(2026, 11, 22))


def test_batch_3_is_registered_on_trial():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    for profile in (CYPRUS, SLOVENIA, NETHERLANDS_ANBI, SCOTLAND_OSCR, CANADA_CRA, US_NPPES):
        assert PROFILES[profile.source] is profile and profile.guard_names
        assert built[profile.source]["job"] == profile.job and built[profile.source]["stage"] == "trial"
