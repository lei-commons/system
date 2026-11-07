import csv
import io
import zipfile

import pytest

from lc_ingest.gleif import Skip, to_record
from lc_ingest.gleif_csv import read_zip, to_api_shape

ROW = {
    "LEI": "5493001KJTIIGC8Y1R12", "Entity.LegalName": "EXAMPLE LIMITED",
    "Entity.OtherEntityNames.OtherEntityName.1": "EXAMPLE LTD", "Entity.OtherEntityNames.OtherEntityName.2": "",
    "Entity.LegalAddress.FirstAddressLine": "1 Example Street", "Entity.LegalAddress.AdditionalAddressLine.1": "Floor 2",
    "Entity.LegalAddress.City": "London", "Entity.LegalAddress.Region": "GB-LND", "Entity.LegalAddress.Country": "GB",
    "Entity.LegalAddress.PostalCode": "EC1A 1AA", "Entity.HeadquartersAddress.FirstAddressLine": "1 Example Street",
    "Entity.HeadquartersAddress.City": "London", "Entity.HeadquartersAddress.Country": "GB",
    "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA000585",
    "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "01234567", "Entity.LegalJurisdiction": "GB",
    "Entity.EntityCategory": "GENERAL", "Entity.LegalForm.EntityLegalFormCode": "H0PO", "Entity.EntityStatus": "ACTIVE",
    "Entity.EntityCreationDate": "2001-02-03T00:00:00Z", "Registration.ManagingLOU": "ignored",
}


def test_csv_row_maps_to_the_same_record_as_the_api():
    r = to_record(to_api_shape(ROW))
    assert r["directoryId"] == "LECOZZFZ6LZCOJBDKT73" and r["otherNames"] == ["EXAMPLE LTD"]
    assert r["legalAddress"]["addressLines"] == ["1 Example Street", "Floor 2"]
    assert r["incorporationDate"] == "2001-02-03" and "region" not in r["headquartersAddress"]


def test_csv_sole_proprietor_is_dropped():
    with pytest.raises(Skip, match="natural person"):
        to_record(to_api_shape({**ROW, "Entity.EntityCategory": "SOLE_PROPRIETOR"}))


def test_other_authority_used_when_no_list_code():
    row = {**ROW, "Entity.RegistrationAuthority.RegistrationAuthorityID": "",
           "Entity.RegistrationAuthority.OtherRegistrationAuthorityID": "Some register"}
    assert to_record(to_api_shape(row))["registrationAuthority"] == "Some register"


def zipped(rows, names=("data.csv",)):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in names:
            text = io.StringIO()
            w = csv.DictWriter(text, fieldnames=list(ROW))
            w.writeheader()
            w.writerows(rows)
            z.writestr(name, text.getvalue())
    buf.seek(0)
    return buf


def test_read_zip_streams_rows():
    rows = [ROW, {**ROW, "LEI": "529900T8BM49AURSDO55", "Entity.LegalName": "SECOND, \\"QUOTED\\" LTD"}]
    out = [to_record(r) for r in read_zip(zipped(rows))]
    assert [r["lei"] for r in out] == ["5493001KJTIIGC8Y1R12", "529900T8BM49AURSDO55"]
    assert out[1]["legalName"] == 'SECOND, \\"QUOTED\\" LTD'


@pytest.mark.parametrize("names", [(), ("a.csv", "b.csv")])
def test_archive_must_hold_exactly_one_csv(names):
    with pytest.raises(ValueError):
        list(read_zip(zipped([ROW], names)))
