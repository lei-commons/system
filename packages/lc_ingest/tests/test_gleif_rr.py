import pytest

from lc_ingest.gleif import Skip
from lc_ingest.gleif_rr import relationship_id, to_relationship

A, B = "5493001KJTIIGC8Y1R12", "529900T8BM49AURSDO55"


def row(**over):
    base = {"Relationship.StartNode.NodeID": A, "Relationship.StartNode.NodeIDType": "LEI",
            "Relationship.EndNode.NodeID": B, "Relationship.EndNode.NodeIDType": "LEI",
            "Relationship.RelationshipType": "IS_DIRECTLY_CONSOLIDATED_BY", "Relationship.RelationshipStatus": "ACTIVE",
            "Relationship.Period.1.periodType": "ACCOUNTING_PERIOD", "Relationship.Period.1.startDate": "2025-01-01T00:00:00Z",
            "Relationship.Period.2.periodType": "RELATIONSHIP_PERIOD", "Relationship.Period.2.startDate": "2019-06-30T00:00:00Z",
            "Relationship.Period.2.endDate": "", "Relationship.Quantifiers.1.QuantifierUnits": "PERCENTAGE",
            "Relationship.Quantifiers.1.QuantifierAmount": "75.5"}
    base.update(over)
    return base


def test_maps_a_parent_relationship():
    r = to_relationship(row())
    assert r == {"directoryId": relationship_id(A, B, "IS_DIRECTLY_CONSOLIDATED_BY"), "startNode": A, "endNode": B,
                 "relationshipType": "IS_DIRECTLY_CONSOLIDATED_BY", "relationshipStatus": "ACTIVE", "percentage": 75.5,
                 "periodStart": "2019-06-30"}


def test_id_is_stable_and_distinguishes_type_and_direction():
    ids = {relationship_id(A, B, "X"), relationship_id(B, A, "X"), relationship_id(A, B, "Y")}
    assert len(ids) == 3 and relationship_id(A, B, "X") == relationship_id(A, B, "X")


@pytest.mark.parametrize("over", [{"Relationship.StartNode.NodeIDType": "NATURAL_PERSON"}, {"Relationship.EndNode.NodeIDType": "OTHER"},
                                  {"Relationship.EndNode.NodeID": A}, {"Relationship.RelationshipType": ""}])
def test_anything_but_lei_to_lei_is_skipped(over):
    with pytest.raises(Skip):
        to_relationship(row(**over))


@pytest.mark.parametrize("over", [{"Relationship.Quantifiers.1.QuantifierUnits": "SHARES"},
                                  {"Relationship.Quantifiers.1.QuantifierAmount": "150"},
                                  {"Relationship.Quantifiers.1.QuantifierAmount": ""}])
def test_percentage_only_when_it_is_one(over):
    assert "percentage" not in to_relationship(row(**over))


def test_bad_percentage_is_an_error_for_the_pipeline_to_count():
    with pytest.raises(ValueError):
        to_relationship(row(**{"Relationship.Quantifiers.1.QuantifierAmount": "abc"}))


def test_only_allow_listed_fields_and_the_key_survive_the_pipeline():
    from collections import Counter

    from lc_core.allowlist import AllowList
    from lc_ingest.pipeline import map_records

    (r,) = map_records([row(Extra="x")], Counter(), AllowList.load(), mapper=to_relationship, kind="relationship")
    assert set(r) - {"directoryId"} <= AllowList.load().allowed["relationship"] and "Extra" not in r
