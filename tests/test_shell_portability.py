"""Prod runs Ubuntu 26.04, whose coreutils are uutils: the obsolete numeric
form `tail -40` / `head -1` is a HARD ERROR there ("unexpected argument '-4'
found"), not a style nit. The operator's very first `tail -40 <log>` after a
deploy failed on it, so the guards are checked rather than remembered."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "tools").glob("*.sh"))
OBSOLETE = re.compile(r"(?<![\w-])(?:tail|head)\s+-(\d+)(?![\d\w])")


class ShellPortabilityTests(unittest.TestCase):
    def test_there_are_scripts_to_check(self):
        self.assertGreaterEqual(len(SCRIPTS), 4)

    def test_no_obsolete_numeric_tail_or_head(self):
        offenders = []
        for path in SCRIPTS:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for match in OBSOLETE.finditer(line):
                    tool = match.group(0).split()[0].lstrip("|").strip()
                    offenders.append(
                        f"{path.name}:{lineno}: {match.group(0)} "
                        f"(uutils rejects it; write '{tool} -n {match.group(1)}')")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_check_actually_matches(self):
        self.assertTrue(OBSOLETE.search("docker logs bot | tail -40"))
        self.assertTrue(OBSOLETE.search("x | head -1)"))
        self.assertIsNone(OBSOLETE.search("docker logs --tail 200 bot"))
        self.assertIsNone(OBSOLETE.search("x | tail -n 40"))
        self.assertIsNone(OBSOLETE.search("head -n 1)"))


if __name__ == "__main__":
    unittest.main()
