"""The second factor's lockout: one count for every service that checks codes, which a restart doesn't reset."""

import base64

from lc_core.totp import LOCKOUT_SECONDS, MAX_FAILURES, MemorySteps, Verifier, code_at

SECRET = base64.b32encode(b"0123456789abcdefghij").decode()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def test_two_services_share_one_count_and_a_restart_keeps_it():
    clock, shared = Clock(), MemorySteps()
    site, console = Verifier(shared, clock), Verifier(shared, clock)
    for i in range(MAX_FAILURES):  # wrong codes spread over both services
        assert not (site if i % 2 else console).verify(SECRET, "000000")
    assert site.locked() and console.locked()
    restarted = Verifier(shared, clock)  # a new process reads the same lockout
    assert restarted.locked()
    assert not restarted.verify(SECRET, code_at(SECRET, int(clock.t) // 30))  # even a right code waits it out
    clock.t += LOCKOUT_SECONDS + 1
    assert restarted.verify(SECRET, code_at(SECRET, int(clock.t) // 30))


def test_a_right_code_clears_the_count():
    clock, shared = Clock(), MemorySteps()
    v = Verifier(shared, clock)
    for _ in range(MAX_FAILURES - 1):
        assert not v.verify(SECRET, "000000")
    assert v.verify(SECRET, code_at(SECRET, int(clock.t) // 30))
    assert shared.read_lockout()[0]["failures"] == 0
    assert not v.verify(SECRET, "000000") and not v.locked()


def test_a_store_without_a_lockout_counts_in_the_process():
    class StepsOnly:
        last = -1

        def claim(self, step):
            ok, self.last = step > self.last, max(self.last, step)
            return ok

    clock = Clock()
    v = Verifier(StepsOnly(), clock)
    for _ in range(MAX_FAILURES):
        v.verify(SECRET, "000000")
    assert v.locked()
