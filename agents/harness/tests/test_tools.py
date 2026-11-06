import os

import pytest

from lc_harness.tools import MAX_READ_BYTES, Tools


@pytest.fixture
def t(tmp_path):
    (tmp_path / "a.txt").write_text("one two")
    (tmp_path / "constitution.md").write_text("rules")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    return Tools(tmp_path)


def ok(t, name, **args):
    out, err = t.call(name, args)
    assert not err, out
    return out


def refused(t, name, **args):
    out, err = t.call(name, args)
    assert err
    return out


def test_read_and_list(t):
    assert ok(t, "read_file", path="a.txt") == "one two"
    listing = ok(t, "list_dir", path=".")
    assert "a.txt" in listing and ".git" not in listing


def test_write_creates_directories(t):
    ok(t, "write_file", path="new/dir/b.txt", content="hi")
    assert ok(t, "read_file", path="new/dir/b.txt") == "hi"


def test_edit_needs_one_exact_match(t):
    ok(t, "edit_file", path="a.txt", old="one", new="1")
    assert ok(t, "read_file", path="a.txt") == "1 two"
    assert "0 times" in refused(t, "edit_file", path="a.txt", old="nope", new="x")
    assert "0 times" in refused(t, "edit_file", path="a.txt", old="", new="x")


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "a/../../outside", "..", ""])
def test_paths_outside_the_checkout_refused(t, path):
    refused(t, "read_file", path=path)
    refused(t, "write_file", path=path, content="x")


def test_symlink_out_of_the_checkout_refused(t, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret").write_text("s")
    os.symlink(outside, t.root / "link")
    assert "outside" in refused(t, "read_file", path="link/secret")
    refused(t, "write_file", path="link/new", content="x")


def test_dot_git_is_off_limits(t):
    assert ".git" in refused(t, "read_file", path=".git/config")
    refused(t, "write_file", path=".git/hooks/pre-commit", content="x")
    refused(t, "write_file", path="sub/../.git/x", content="x")


def test_constitution_is_readable_but_not_writable(t):
    assert ok(t, "read_file", path="constitution.md") == "rules"
    assert "owner" in refused(t, "write_file", path="constitution.md", content="x")
    refused(t, "edit_file", path="constitution.md", old="rules", new="x")
    refused(t, "write_file", path="./sub/../constitution.md", content="x")
    assert (t.root / "constitution.md").read_text() == "rules"


def test_big_and_binary_files_refused(t):
    (t.root / "big").write_text("x" * (MAX_READ_BYTES + 1))
    (t.root / "bin").write_bytes(b"\xff\xfe\x00")
    refused(t, "read_file", path="big")
    assert "UTF-8" in refused(t, "read_file", path="bin")


@pytest.mark.parametrize("name,args", [
    ("unknown", {"path": "a"}), ("read_file", {}), ("read_file", {"path": 1}), ("read_file", "a.txt"),
    ("read_file", {"path": "a.txt", "extra": "x"}), ("write_file", {"path": "a.txt"}),
])
def test_bad_calls_are_errors_not_crashes(t, name, args):
    assert t.call(name, args)[1] is True


def test_run_tests_hides_credentials_from_the_test_process(t, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secretvalue")
    (t.root / "test_env.py").write_text("import os\n\ndef test_env():\n    assert 'GITHUB_TOKEN' not in os.environ\n")
    (t.root / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n")
    out = ok(t, "run_tests", path="test_env.py")
    assert out.startswith("exit code 0") and "secretvalue" not in out
