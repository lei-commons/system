import pytest

from lc_core.owner import Conflict, perform
from lc_core.totp import MemorySteps, Verifier, code_at, matching_step

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SOURCES = [{"job": "ingest-gleif", "label": "GLEIF"}]
REGISTRY = [{"id": "gleif", "name": "GLEIF", "approval": "approved"}, {"id": "us-co-sos", "name": "Colorado"}]


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Backend:
    def __init__(self):
        self.state, self.state_gen = {"state": "running", "paused_components": []}, 1
        self.approvals, self.approvals_gen = {}, 0
        self.runs, self.events, self.conflict = [], [], False

    def read_state(self):
        return dict(self.state), self.state_gen

    def write_state(self, state, generation):
        if self.conflict or generation != self.state_gen:
            raise Conflict("it changed meanwhile; try again")
        self.state, self.state_gen = state, generation + 1

    def read_approvals(self):
        return dict(self.approvals), self.approvals_gen

    def write_approvals(self, decisions, generation):
        if self.conflict or generation != self.approvals_gen:
            raise Conflict("it changed meanwhile; try again")
        self.approvals, self.approvals_gen = decisions, generation + 1

    def run_job(self, job):
        if job == "load-db" and self.conflict:
            raise PermissionError("not allowed to start load-db")
        self.runs.append(job)
        return f"{job}-x1"

    def record(self, event):
        self.events.append(event)


@pytest.fixture
def env():
    clock = Clock()
    backend = Backend()
    verifier = Verifier(MemorySteps(), clock=clock)

    def act(action, reason="testing", code=None, offset=0):
        code = code if code is not None else code_at(SECRET, int(clock.t) // 30 + offset)
        return perform(backend=backend, verifier=verifier, secret=SECRET, sources=SOURCES, registry=REGISTRY,
                       action=action, reason=reason, code=code, email="o@x.test", now="2026-11-13T09:00:00Z",
                       trigger="site")

    return clock, backend, act


def test_approving_a_pending_source(env):
    clock, backend, act = env
    out = act("source-approve:us-co-sos", reason="terms checked")
    assert out.ok and backend.approvals["us-co-sos"]["status"] == "approved"
    assert backend.events[-1] == {**backend.events[-1], "decision": "done", "subject": "source us-co-sos", "trigger": "site"}


def test_a_code_works_once_even_for_a_different_action(env):
    clock, backend, act = env
    code = code_at(SECRET, int(clock.t) // 30)
    assert act("source-deny:gleif", code=code).ok
    again = act("source-approve:gleif", code=code)
    assert again.status == 403 and backend.approvals["gleif"]["status"] == "denied"
    clock.t += 30
    assert act("source-approve:gleif").ok  # the next code works


def test_a_slow_phone_clock_is_accepted(env):
    clock, backend, act = env
    assert act("pause", offset=-3).ok and backend.state["state"] == "paused"


def test_refusals_change_nothing(env):
    clock, backend, act = env
    assert act("source-approve:nowhere").status == 404
    assert act("source-approve:us-co-sos", reason="  ").status == 400
    assert act("source-approve:us-co-sos", code="123456").status == 403
    assert act("source-approve:us-co-sos", code="abc").status == 403
    assert backend.approvals == {} and backend.events == []


def test_lockout_after_repeated_wrong_codes(env):
    clock, backend, act = env
    for _ in range(5):
        act("pause", code="000000")
    out = act("pause")  # even the right code waits out the lockout
    assert out.status == 403 and "Wait five minutes" in out.message
    clock.t += 301
    assert act("pause").ok


def test_state_rules_and_conflicts_are_reported_and_recorded(env):
    clock, backend, act = env
    backend.state = {"state": "stopped", "paused_components": []}
    out = act("resume")
    assert out.status == 409 and backend.events[-1]["decision"] == "rejected"
    clock.t += 30
    backend.conflict = True
    out = act("source-deny:gleif")
    assert out.status == 409 and "changed meanwhile" in out.message and backend.events[-1]["decision"] == "failed"


def test_running_a_job_and_a_refused_start(env):
    clock, backend, act = env
    assert act("run:ingest-gleif").ok and backend.runs == ["ingest-gleif"]
    clock.t += 30
    backend.conflict = True
    out = act("run:load-db")
    assert out.status == 502 and "not allowed" in out.message


def test_matching_step():
    assert matching_step(SECRET, code_at(SECRET, 100), 100 * 30) == 100
    assert matching_step(SECRET, code_at(SECRET, 100), 200 * 30) is None


def test_the_shared_step_record_refuses_replays_across_services():
    pytest.importorskip("google.api_core")
    from google.api_core.exceptions import NotFound, PreconditionFailed

    from lc_core.totp import GcsSteps

    store = {}

    class Blob:
        generation = None

        def reload(self):
            if "o" not in store:
                raise NotFound("none")
            self.generation = store["o"][1]

        def download_as_bytes(self):
            return store["o"][0]

        def upload_from_string(self, data, content_type=None, if_generation_match=None):
            current = store["o"][1] if "o" in store else 0
            if if_generation_match != current:
                raise PreconditionFailed("moved")
            store["o"] = (data.encode(), current + 1)

    class Bucket:
        def blob(self, name):
            return Blob()

    site, console = GcsSteps(Bucket()), GcsSteps(Bucket())
    assert site.claim(100) and not console.claim(100) and not console.claim(99) and console.claim(101)
