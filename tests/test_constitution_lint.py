"""Phase 7, Task 7.3 — the constitution's checkable clauses, enforced by CI.

Every rule below was fixed by hand in an earlier phase. That is exactly why they
need a test: a rule that was enforced once, by a reviewer who happened to
remember it, is a rule the next PR reinstates. These fail the build instead.

Each check carries an ALLOW list rather than a blanket skip, because "this one
is fine" is a claim that should be written down with its reason next to it. A
new entry in an ALLOW list is a deliberate, reviewable act; a new violation is
not.
"""

import io
import os
import re
import unittest
from glob import glob

TEMPLATES = sorted(glob("app/templates/**/*.html", recursive=True))
STATIC_JS = sorted(glob("app/static/*.js"))


def _read(path):
    return io.open(path, encoding="utf8").read()


_JINJA_COMMENT = re.compile(r"{#.*?#}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _read_code(path):
    """Source with comments blanked out, line numbering preserved.

    A comment that names a banned construct in order to explain why it is banned
    is not a use of it — and these files are heavily commented by design."""
    source = _read(path)
    for pattern in (_JINJA_COMMENT, _HTML_COMMENT):
        source = pattern.sub(
            lambda m: re.sub(r"[^\n]", " ", m.group(0)), source
        )
    return source


def _relpath(path):
    return path.replace(os.sep, "/")


_BRANCH = re.compile(r"{%-?\s*(if|elif|else|endif)\b.*?-?%}", re.S)


def max_on_any_path(source, pattern):
    """The most matches of ``pattern`` that can appear on ONE rendered path.

    Counting matches in the file over-reports: a screen offering
    "Re-upload corrected file" in the failure branch and "Confirm & create run"
    in the success branch shows exactly one of them, and a rule about what the
    reader SEES has to be counted the way the reader sees it.

    Walks the {% if %}/{% elif %}/{% else %}/{% endif %} tree and takes the max
    across sibling branches, the sum along a path. Unbalanced or exotic markup
    degrades to a plain count, which is the conservative direction.
    """
    tokens = list(_BRANCH.finditer(source))
    if not tokens:
        return len(pattern.findall(source))

    def count_between(start, end):
        return len(pattern.findall(source[start:end]))

    # Each frame: (text_so_far_on_this_branch, best_of_finished_branches)
    stack = [[0, 0]]
    cursor = 0
    for token in tokens:
        stack[-1][0] += count_between(cursor, token.start())
        cursor = token.end()
        kind = token.group(1)
        if kind == "if":
            stack.append([0, 0])
        elif kind in ("elif", "else"):
            if len(stack) == 1:
                return len(pattern.findall(source))  # unbalanced
            stack[-1][1] = max(stack[-1][1], stack[-1][0])
            stack[-1][0] = 0
        elif kind == "endif":
            if len(stack) == 1:
                return len(pattern.findall(source))  # unbalanced
            branch = stack.pop()
            stack[-1][0] += max(branch[0], branch[1])
    stack[-1][0] += count_between(cursor, len(source))
    return max(stack[0][0], stack[0][1]) if len(stack) == 1 else len(
        pattern.findall(source)
    )


class NoRoleLiteralsInTemplatesTests(unittest.TestCase):
    """Phase 1, Task 1.2 — a role literal in a template cannot be read by the
    route that guards the same action, which is how UI and access drift apart.
    Templates ask a `can_*` predicate instead."""

    # `current_user.role ==`, `.role in [...]`, `role == "admin"`, etc.
    PATTERN = re.compile(
        r"""(current_user\.role|(?<![\w.])role)\s*(==|!=|\s+in\s+)\s*['"\[(]"""
    )

    def test_no_template_compares_a_role_to_a_literal(self):
        offenders = []
        for path in TEMPLATES:
            for lineno, line in enumerate(_read_code(path).splitlines(), 1):
                if self.PATTERN.search(line):
                    offenders.append(f"{_relpath(path)}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(
            offenders,
            [],
            "role literals in templates — use a can_* predicate from "
            "app/permissions.py so the template and the route agree:\n  "
            + "\n  ".join(offenders),
        )


class NoNativeConfirmTests(unittest.TestCase):
    """Phase 2, Task 2.5 — native confirm() blocks the renderer and cannot carry
    the consequence copy a destructive action needs. Use data-confirm (app.js
    turns it into the styled modal) or hx-confirm."""

    PATTERN = re.compile(r"(?<![\w.$-])confirm\s*\(")
    # app.js is where the replacement modal lives, so it necessarily names the
    # thing it replaces.
    ALLOW = {"app/static/app.js"}

    def test_nothing_calls_native_confirm(self):
        offenders = []
        for path in TEMPLATES + STATIC_JS:
            rel = _relpath(path)
            if rel in self.ALLOW:
                continue
            for lineno, line in enumerate(_read_code(path).splitlines(), 1):
                if self.PATTERN.search(line) and "hx-confirm" not in line:
                    offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(
            offenders,
            [],
            "native confirm() — use data-confirm / hx-confirm instead:\n  "
            + "\n  ".join(offenders),
        )


class NoUnboundedCollectionRendersTests(unittest.TestCase):
    """Phase 4/7 — a table whose row count is a function of how much data a
    customer has must be paginated, or it is a page that gets slower forever.

    A template passes if its table body iterates a Pagination page
    (``something.items``) or it appears in ALLOW with a reason.
    """

    # path -> why this collection cannot grow with customer data.
    ALLOW = {
        "app/templates/client/run_upload.html": "12 months",
        "app/templates/client/statutory.html": "PAYE bands — a handful, set by GRA",
        "app/templates/statutory_rates.html": "PAYE bands per rate set",
        "app/templates/macros/import_preview.html": "preview_rows is capped at 20 by the importer",
        "app/templates/macros/reports.html": "bank_groups — one row per bank, not per worker",
        "app/templates/macros/ui.html": "the pagination component itself",
        "app/templates/dashboard.html": "fixed KPI/period/signal sets; company_table is top-N",
        "app/templates/distribution/analytics.html": "grouped aggregates + filter options",
        "app/templates/distribution/_dashboard_fragment.html": "recent/running batches are LIMITed in collect_dashboard_stats",
        "app/templates/payroll_preview.html": "one row per matched client sheet in one workbook",
        "app/templates/employees/bulk_import_preview.html": "one upload's rows, shown before commit",
        "app/templates/client/audit.html": "server-side LIMIT; see app/client audit route",
        "app/templates/audit.html": "server-side LIMIT 250/100; see app/audit.py",
        "app/templates/search_results.html": "server-side LIMIT 25 clients / 50 items; see main.search",
        "app/templates/payroll_detail.html": "comparison.rows is a fixed metric set; the items grid pages via ui.data_table",
        "app/templates/wage_rates.html": "one row per configured pay code for one client, not per worker",
        "app/templates/macros/distribution.html": "delivery rows come in as a page (rows_page); `channels` is the three delivery channels",
    }

    # Only loops that generate ROWS count. A <select> of filter options in a
    # template that also has a table is not an unbounded table.
    TABLE_BODY = re.compile(r"<tbody\b.*?</tbody>|<table\b.*?</table>", re.S)
    LOOP = re.compile(r"{%-?\s*for\s+\w+\s+in\s+([\w.]+)")

    def _row_loops(self, source):
        loops = []
        for block in self.TABLE_BODY.finditer(source):
            loops.extend(self.LOOP.findall(block.group(0)))
        return loops

    def test_every_table_paginates_or_says_why_not(self):
        offenders = []
        for path in TEMPLATES:
            rel = _relpath(path)
            if rel in self.ALLOW:
                continue
            source = _read(path)
            loops = self._row_loops(source)
            if not loops:
                continue
            # `x.items` is the Flask-SQLAlchemy Pagination page contract.
            if any(loop.endswith(".items") for loop in loops):
                continue
            # Either the shared component or a hand-rolled footer over a
            # Flask-SQLAlchemy Pagination — both mean the query is paged.
            if "ui.pagination" in source or re.search(r"\bpagination\.(page|pages|has_next|total)\b", source):
                continue
            offenders.append(f"{rel}: iterates {', '.join(sorted(set(loops)))}")
        self.assertEqual(
            offenders,
            [],
            "unpaginated collection renders — paginate them, or add an entry to "
            f"{type(self).__name__}.ALLOW stating why the collection is bounded:\n  "
            + "\n  ".join(offenders),
        )


class PollingRegionsTerminateTests(unittest.TestCase):
    """Phase 7, Task 7.2 — every polling region stops on its own, stands down
    while the tab is hidden, and holds no focusable control (a poll swaps the
    region out from under whatever the cursor is in)."""

    POLL = re.compile(r'hx-trigger="every [^"]*"')
    FOCUSABLE = re.compile(r"<(select|input|textarea|button)\b")

    def _polling_templates(self):
        return [p for p in TEMPLATES if self.POLL.search(_read(p))]

    def test_there_are_polling_regions_to_check(self):
        self.assertGreaterEqual(len(self._polling_templates()), 3)

    def test_every_poll_stands_down_when_the_tab_is_hidden(self):
        offenders = []
        for path in self._polling_templates():
            for trigger in self.POLL.findall(_read(path)):
                if "visibilityState" not in trigger:
                    offenders.append(f"{_relpath(path)}: {trigger}")
        self.assertEqual(
            offenders,
            [],
            "polling that continues in a backgrounded tab — add the trigger "
            "filter [document.visibilityState === 'visible']:\n  "
            + "\n  ".join(offenders),
        )

    def test_every_poll_is_conditional_on_something_being_in_flight(self):
        """The stop condition: the swapped-in markup must be able to come back
        WITHOUT the trigger, which means the trigger sits behind an {% if %}."""
        offenders = []
        for path in self._polling_templates():
            source = _read(path)
            trigger_at = source.index("hx-trigger=")
            # The {% if %} guarding it opens within the same element.
            element_start = source.rindex("<div", 0, trigger_at)
            if "{% if" not in source[element_start:trigger_at]:
                offenders.append(_relpath(path))
        self.assertEqual(
            offenders,
            [],
            "polling with no terminal condition — guard the hx-trigger with the "
            "state that makes it necessary:\n  " + "\n  ".join(offenders),
        )

    def test_no_polling_region_contains_a_focusable_control(self):
        """Checked on the rendered contract, not just the file: the two run
        status fragments pull their table from macros/distribution.html, whose
        control is gated on `live` for exactly this reason."""
        offenders = []
        for path in self._polling_templates():
            source = _read(path)
            for match in self.FOCUSABLE.finditer(source):
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{_relpath(path)}:{line}: <{match.group(1)}>")
        self.assertEqual(
            offenders,
            [],
            "focusable controls inside a region that swaps itself on a timer:\n  "
            + "\n  ".join(offenders),
        )


class OnePrimaryActionPerScreenTests(unittest.TestCase):
    """Phase 4 — rank is expressed visually, so exactly one control per screen
    may carry the primary treatment. Two primaries is no primary."""

    # `.btn primary` is the canonical component (macros/ui.html::action_bar and
    # the tenant shell); Bootstrap's btn-primary is the operator shell's.
    PRIMARY = re.compile(r'class="[^"]*\b(?:btn primary|btn-primary)\b')
    ALLOW = {
        "app/templates/macros/ui.html": "the component that renders the one primary",
        "app/templates/macros/reports.html": "one primary per report block, each a separate decision",
        "app/templates/client/run_reports.html": "same — one download per report section",
        "app/templates/payroll_run_reports.html": "same — one download per report section",
        "app/templates/employees/_roster_body.html": "per-row actions in a table, not screen-level",
        "app/templates/payroll/_runs_body.html": "per-row actions in a table, not screen-level",
        "app/templates/distribution/history.html": "per-row actions in a table",
        "app/templates/wage_rates.html": "per-row save in an editable grid",
        "app/templates/payroll_items_edit.html": "per-row save in an editable grid",
        "app/templates/client/run_upload.html": "one submit per upload tab; tabs are mutually exclusive at runtime, and one lives in a JS-built preview string this cannot see into",
        "app/templates/payroll_new_run.html": "one submit per upload tab, as above",
        "app/templates/errors/404.html": "content vs public_content blocks — base.html renders one or the other, never both",
        "app/templates/errors/500.html": "content vs public_content blocks, as above",
    }

    def test_no_screen_offers_two_primary_actions(self):
        offenders = []
        for path in TEMPLATES:
            rel = _relpath(path)
            if rel in self.ALLOW:
                continue
            count = max_on_any_path(_read_code(path), self.PRIMARY)
            if count > 1:
                offenders.append(f"{rel}: {count} primary controls")
        self.assertEqual(
            offenders,
            [],
            "more than one visually primary action on a screen — demote all but "
            "the recommended one, or add an ALLOW entry if these are per-row:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
