import pytest

from lc_core.actions import Refused, apply, catalogue, known

SOURCES = [{"job": "ingest-gleif", "label": "GLEIF"}, {"job": "ingest-br", "label": "Receita Federal"}]


def ids(actions):
    return [a["id"] for a in actions]


def test_the_catalogue_follows_the_sources_and_the_state():
    running = catalogue(SOURCES, {"state": "running", "paused_components": ["job:ingest-br"]})
    got = ids(running)
    assert "resume" not in got and "pause" in got and "stop" in got
    assert ["run:ingest-gleif", "run:ingest-br", "run:build-snapshot", "run:load-db"] == [i for i in got if i.startswith("run:")]
    assert "resume-component:job:ingest-br" in got and "pause-component:job:publish" in got
    stopped = ids(catalogue(SOURCES, {"state": "stopped"}))
    assert "restart" in stopped and "resume" not in stopped and "stop" not in stopped
    more = ids(catalogue(SOURCES + [{"job": "ingest-xx", "label": "New"}], {"state": "running"}))
    assert "run:ingest-xx" in more  # a new source gets its actions with no other change


def test_known_actions_only():
    assert known(SOURCES, "run:load-db") and known(SOURCES, "pause-component:job:ingest-br")
    assert not known(SOURCES, "run:something-else") and not known(SOURCES, "pause-component:job:x") and not known(SOURCES, "drop")


def test_state_rules_match_the_control_repository():
    now, actor = "2026-11-11T10:00:00Z", "owner:o@x.test"
    assert apply("pause", None, actor, now)["state"] == "paused"  # always works, even on a missing state
    assert apply("stop", {"state": "garbage"}, actor, now)["state"] == "stopped"
    with pytest.raises(Refused):
        apply("resume", None, actor, now)
    with pytest.raises(Refused):
        apply("resume", {"state": "stopped"}, actor, now)
    assert apply("restart", {"state": "stopped"}, actor, now)["state"] == "paused"
    with pytest.raises(Refused):
        apply("restart", {"state": "running"}, actor, now)
    new = apply("pause-component:job:ingest-br", {"state": "running", "paused_components": []}, actor, now)
    assert new["paused_components"] == ["job:ingest-br"] and new["updated_by"] == actor
    assert apply("resume-component:job:ingest-br", new, actor, now)["paused_components"] == []
    with pytest.raises(Refused):
        apply("run:load-db", {"state": "running"}, actor, now)


def test_every_registry_source_can_be_approved_denied_or_kept_pending():
    from lc_core.actions import source_catalogue

    registry = [{"id": "gleif", "name": "GLEIF"}, {"id": "us-co-sos", "name": "Colorado"}]
    got = ids(source_catalogue(registry, {"gleif": {"status": "approved"}, "us-co-sos": {"status": "pending"}}))
    assert got == ["source-deny:gleif", "source-pending:gleif", "source-approve:us-co-sos", "source-deny:us-co-sos"]
    assert known([], "source-deny:gleif", registry) and not known([], "source-deny:nowhere", registry)
