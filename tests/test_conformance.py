"""install.py against the installer conformance fixture (tests/conformance/).

Every case of expected.json, installed from its own directory into a
prefix that already holds an install (the four-set release of the same
name, another release's descriptor, and a file of the user's beside them):

- installed: the release's files exactly (paths and sha256), its four
  descriptors exactly, absolute under the prefix; no other 10-/20- descriptor
  of ours left, the user's file kept;
- refused: an InstallError whose message is the case's, and the prefix as
  it was (every path, each file's bytes);
- waits: with <prefix>/.lock held, nothing changes for a while; once it is
  released, the install completes.

Also: the fixture is what make-fixture.py generates (--check).
"""

from __future__ import annotations

import fcntl
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from axlfw import install  # noqa: E402

HERE = Path(__file__).resolve().parent / "conformance"
EXPECTED = json.loads((HERE / "expected.json").read_text())
# How long a held lock must keep the install waiting, in this test.
LOCK_HOLD = 1.0


def snapshot(prefix: Path) -> dict[str, bytes | None]:
    """Every path under prefix: a file's bytes, or None for a directory."""
    return {
        str(p.relative_to(prefix)): (p.read_bytes() if p.is_file() else None) for p in sorted(prefix.rglob("*"))
    }


def pattern(template: str, tarball: Path, prefix: Path, release: str) -> re.Pattern[str]:
    """A case's message as a regular expression: its placeholders filled."""
    parts = re.split(r"(\{tarball\}|\{tarball_dir\}|\{unpacked\})", template)
    fill = {
        "{tarball}": re.escape(str(tarball)),
        "{tarball_dir}": re.escape(str(tarball.parent)),
        "{unpacked}": re.escape(str(prefix)) + r"/\.install\.[^/]+/" + re.escape(f"axl-firmware-{release}"),
    }
    return re.compile("".join(fill.get(p, re.escape(p)) for p in parts) + r"\Z")


class ConformanceTests(unittest.TestCase):
    """install.py meets every case of the fixture."""

    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.prefix = (self.root / "prefix").resolve()
        self.release = str(EXPECTED["release"])

    def tearDown(self) -> None:
        self._t.cleanup()

    def case(self, name: str) -> dict[str, object]:
        return next(c for c in EXPECTED["cases"] if c["name"] == name)

    def given(self, case: dict[str, object], label: str = "given") -> Path:
        """The case's files, copied to a directory of its own: its tarball."""
        d = self.prefix.parent / label / str(case["name"])
        d.mkdir(parents=True)
        for key in ("tarball", "sha256sums"):
            if case[key] is not None:
                shutil.copyfile(HERE / str(case[key]), d / Path(str(case[key])).name)
        return d / Path(str(case["tarball"])).name

    def earlier(self) -> None:
        """The prefix as an earlier install left it."""
        earlier = self.given(self.case("good-four-sets"), "earlier")
        install.install("o/r", str(earlier), self.prefix, None, symbols=False)
        (self.prefix / "descriptors" / "10-axl-firmware-edk2-stable199912-r1-x64.json").write_text("{}\n")
        (self.prefix / "descriptors" / "50-mine.json").write_text("{}\n")

    def assert_installed(self, case: dict[str, object]) -> None:
        want = EXPECTED["installed"]
        rel = self.prefix / self.release
        got = {
            str(p.relative_to(self.prefix)): install._sha256(p)  # pyright: ignore[reportPrivateUsage]
            for p in rel.rglob("*")
            if p.is_file()
        }
        self.assertEqual(got, case["files"])
        # Files 0644 and directories 0755, whatever the tarball said.
        modes = {(p.is_dir(), p.stat().st_mode & 0o7777) for p in [rel, *rel.rglob("*")]}
        self.assertEqual(modes, {(True, 0o755), (False, 0o644)})
        descriptors = {p.name: json.loads(p.read_text()) for p in (self.prefix / "descriptors").iterdir()}
        mine = descriptors.pop("50-mine.json", None)
        self.assertEqual(mine, {})
        filled = json.loads(json.dumps(want["descriptors"]).replace("{prefix}", str(self.prefix)))
        self.assertEqual(descriptors, filled)

    def test_cases(self) -> None:
        for case in EXPECTED["cases"]:
            if case["outcome"] == "waits":
                continue
            with self.subTest(case["name"]):
                self.prefix = (self.root / str(case["name"]) / "prefix").resolve()
                self.one(case)

    def one(self, case: dict[str, object]) -> None:
        self.earlier()
        before = snapshot(self.prefix)
        tarball = self.given(case)
        pin = case["pin"] if isinstance(case["pin"], str) else None
        if case["outcome"] == "installed":
            install.install("o/r", str(tarball), self.prefix, pin, symbols=False)
            self.assert_installed(case)
            return
        self.assertEqual(case["outcome"], "refused")
        with self.assertRaises(install.InstallError) as cm:
            install.install("o/r", str(tarball), self.prefix, pin, symbols=False)
        self.assertRegex(str(cm.exception), pattern(str(case["message"]), tarball, self.prefix, self.release))
        self.assertEqual(snapshot(self.prefix), before)

    def test_locked(self) -> None:
        case = self.case("locked")
        self.earlier()
        before = snapshot(self.prefix)
        tarball = self.given(case)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                install.install("o/r", str(tarball), self.prefix, None, symbols=False)
            except BaseException as err:  # noqa: BLE001 - reported below
                errors.append(err)

        with (self.prefix / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            t = threading.Thread(target=run)
            t.start()
            time.sleep(LOCK_HOLD)
            self.assertTrue(t.is_alive())
            self.assertEqual(snapshot(self.prefix), before)
        t.join(60)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assert_installed(case)

    def test_every_case_named(self) -> None:
        names = [c["name"] for c in EXPECTED["cases"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            sorted(p.name for p in (HERE / "cases").iterdir()), sorted(names)
        )
        self.assertEqual(EXPECTED["max_member"], install.MAX_MEMBER)
        self.assertEqual(EXPECTED["max_total"], install.MAX_TOTAL)
        self.assertEqual(EXPECTED["max_members"], install.MAX_MEMBERS)

    def test_generated(self) -> None:
        r = subprocess.run(  # noqa: S603 - our own script
            [sys.executable, "-I", str(HERE / "make-fixture.py"), "--check"], capture_output=True, text=True, check=False
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
