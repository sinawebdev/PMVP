"""Guards against a footgun that silently breaks unrelated tests.

Every test module in this suite configures the app through module-scope
``os.environ[...]`` assignments. pytest imports *all* test modules during
collection, before running anything, so those assignments do not scope to the
module that wrote them — the last one imported wins for the entire session.

A single file setting ``SEED_DEMO_DATA = "false"`` therefore unseeds the whole
run if it happens to sort late in the alphabet, and the damage shows up as
hundreds of failures in files that never mentioned the variable. That is exactly
what happened when tests/test_ui_components.py (which sorts last) set it to
"false": 140 tests in 18 other files failed looking for seeded fixtures.

The real fix is per-test app configuration rather than process-wide env, which is
a larger refactor. Until then this test makes the trap fail loudly, in the one
file whose job is to notice it, instead of as a scattered mystery.
"""

import pathlib
import re
import unittest

TESTS_DIR = pathlib.Path(__file__).parent

# Module-scope assignment of a shared env var, e.g.  os.environ["X"] = "false"
_ASSIGN = re.compile(
    r'^os\.environ\[\s*["\'](?P<var>[A-Z_]+)["\']\s*\]\s*=\s*["\'](?P<value>[^"\']*)["\']',
    re.MULTILINE,
)

# Variables whose value must be identical across every module, because the last
# import silently decides it for all of them.
SHARED = {
    "SEED_DEMO_DATA": "true",
    "DATABASE_URL": "sqlite:///:memory:",
    "PERSISTENCE_REQUIRED": "false",
    "SKIP_DOTENV": "true",
}


class ModuleScopeEnvIsConsistentTests(unittest.TestCase):
    def _assignments(self):
        """{var: {value: [files]}} for module-scope assignments across the suite."""
        found = {}
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            for match in _ASSIGN.finditer(source):
                var, value = match.group("var"), match.group("value")
                if var in SHARED:
                    found.setdefault(var, {}).setdefault(value, []).append(path.name)
        return found

    def test_shared_env_vars_agree_across_every_module(self):
        for var, by_value in self._assignments().items():
            expected = SHARED[var]
            offenders = {
                value: files for value, files in by_value.items() if value != expected
            }
            self.assertEqual(
                offenders,
                {},
                f"{var} must be {expected!r} in every test module — pytest imports "
                f"them all before running anything, so the last import wins for the "
                f"whole session. Offending files: {offenders}",
            )

    def test_the_scan_actually_finds_something(self):
        """Guards the guard: a broken regex would make this file vacuously pass."""
        found = self._assignments()
        self.assertIn("SEED_DEMO_DATA", found)
        self.assertGreater(sum(len(f) for f in found["SEED_DEMO_DATA"].values()), 30)


if __name__ == "__main__":
    unittest.main()
