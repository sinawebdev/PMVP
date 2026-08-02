"""Phase 2, Tasks 2.1–2.3, 2.5 — the shared component contracts.

The acceptance criterion for this phase is that a new page can be built from the
Data Table, the state components, the Decision Header, the Action Bar and the
confirmation dialog without inventing a one-off equivalent. The main test here
does exactly that: it renders a complete page using only those macros and asserts
the result is whole.

Also pins the properties that make them worth sharing — the table's controls are
server-side and appear in the URL, the action bar admits exactly one primary
action, and destructive actions carry a confirmation rather than window.confirm.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
# "true" even though these tests need no fixtures: pytest imports every test
# module at collection time, so a module-scope env var set here leaks into the
# whole run. This file sorts last alphabetically, so setting "false" made it the
# winner and silently unseeded ~140 tests in other files. Match the convention.
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from flask import render_template_string  # noqa: E402

from app import create_app  # noqa: E402


class _Page:
    """Stand-in for a Flask-SQLAlchemy Pagination."""

    def __init__(self, page=2, pages=4, total=97, per_page=25, items=None):
        self.page = page
        self.pages = pages
        self.total = total
        self.per_page = per_page
        self.items = items if items is not None else list(range(per_page))
        self.has_prev = page > 1
        self.has_next = page < pages
        self.prev_num = page - 1
        self.next_num = page + 1


class ComponentContractTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.test_request_context("/payroll/runs")
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def _render(self, body, **ctx):
        return render_template_string(
            '{% import "macros/ui.html" as ui %}' + body, **ctx
        )

    # --- 2.1 Data table ----------------------------------------------------
    COLUMNS = [
        {"key": "month", "label": "Period", "sortable": True},
        {"key": "client", "label": "Client", "sortable": True},
        {"key": "net", "label": "Net pay", "align": "num", "sortable": False},
    ]

    def test_table_renders_headers_and_caller_supplied_rows(self):
        out = self._render(
            "{% call ui.data_table(cols, page=page) %}<tr><td>row-one</td></tr>{% endcall %}",
            cols=self.COLUMNS,
            page=_Page(),
        )
        self.assertIn("Period", out)
        self.assertIn("Net pay", out)
        self.assertIn("row-one", out)

    def test_sorting_is_a_link_so_it_lives_in_the_url(self):
        out = self._render(
            "{% call ui.data_table(cols, page=page, sort='month', direction='asc') %}<tr></tr>{% endcall %}",
            cols=self.COLUMNS,
            page=_Page(),
        )
        self.assertIn("sort=client", out)
        # The active column offers the opposite direction next.
        self.assertIn("direction=desc", out)
        self.assertIn('aria-sort="ascending"', out)

    def test_pagination_preserves_the_current_query(self):
        out = self._render(
            "{% call ui.data_table(cols, page=page, params={'q': 'acme', 'status': 'Approved'}) %}<tr></tr>{% endcall %}",
            cols=self.COLUMNS,
            page=_Page(),
        )
        self.assertIn("q=acme", out)
        self.assertIn("status=Approved", out)
        self.assertIn("page=3", out)  # next
        self.assertIn("page=1", out)  # previous

    def test_table_shows_the_empty_state_when_there_are_no_rows(self):
        out = self._render(
            "{% call ui.data_table(cols, page=page, empty_title='No runs yet') %}<tr><td>never</td></tr>{% endcall %}",
            cols=self.COLUMNS,
            page=_Page(items=[], total=0, pages=0),
        )
        self.assertIn("No runs yet", out)
        self.assertIn("state--empty", out)
        self.assertNotIn("never", out)

    def test_wide_table_scrolls_itself_not_the_page(self):
        out = self._render(
            "{% call ui.data_table(cols) %}<tr></tr>{% endcall %}", cols=self.COLUMNS
        )
        self.assertIn("dt-wrap", out)

    # --- 2.3 Action bar ----------------------------------------------------
    def test_action_bar_has_exactly_one_primary_action(self):
        actions = [
            {"label": "Approve", "href": "/a", "method": "POST", "primary": True},
            {"label": "Reject", "href": "/r", "method": "POST"},
            {"label": "Export", "href": "/e"},
        ]
        out = self._render("{{ ui.action_bar(acts) }}", acts=actions)
        self.assertEqual(out.count("btn primary"), 1)

    def test_overflow_actions_go_behind_more(self):
        actions = [
            {"label": "Approve", "href": "/a", "primary": True},
            {"label": "Delete", "href": "/d", "method": "POST", "danger": True,
             "confirm": "Really?", "overflow": True},
        ]
        out = self._render("{{ ui.action_bar(acts) }}", acts=actions)
        self.assertIn("ab-menu", out)
        self.assertIn("More", out)

    def test_destructive_action_carries_a_confirmation_not_window_confirm(self):
        actions = [{
            "label": "Delete run", "href": "/d", "method": "POST", "danger": True,
            "confirm": "This cannot be undone.", "confirm_typed": "July 2026",
        }]
        out = self._render("{{ ui.action_bar(acts) }}", acts=actions)
        self.assertIn('data-confirm="This cannot be undone."', out)
        self.assertIn('data-confirm-danger="1"', out)
        self.assertIn('data-confirm-typed="July 2026"', out)
        self.assertNotIn("onsubmit", out)
        self.assertNotIn("confirm(", out)

    # --- 2.3 Decision header -----------------------------------------------
    def test_decision_header_states_the_reason_for_the_action(self):
        out = self._render(
            "{{ ui.decision_header('July 2026', 'MSC Limited', action=act, figures=figs) }}",
            act={"key": "approve", "label": "Approve", "why": "Waiting for sign-off."},
            figs=[{"label": "Workers", "value": 42}, {"label": "Net", "value": "GHS 91,000"}],
        )
        self.assertIn("July 2026", out)
        self.assertIn("MSC Limited", out)
        self.assertIn("Waiting for sign-off.", out)
        self.assertIn("Workers", out)
        self.assertIn("GHS 91,000", out)

    # --- The phase's acceptance criterion -----------------------------------
    def test_a_whole_page_can_be_built_from_these_components_alone(self):
        page_src = """
        {% import "macros/ui.html" as ui %}
        {{ ui.decision_header('July 2026', 'MSC Limited', action=act,
                              figures=figs, action_html=ui.action_bar(acts)) }}
        {% call ui.data_table(cols, page=page, params={'q': 'msc'}, sort='month') %}
          <tr><td>July 2026</td><td>MSC Limited</td><td class="num">91,000</td></tr>
        {% endcall %}
        """
        out = render_template_string(
            page_src,
            act={"key": "approve", "label": "Approve", "why": "Waiting for sign-off."},
            figs=[{"label": "Workers", "value": 42}],
            acts=[
                {"label": "Approve", "href": "/a", "method": "POST", "primary": True},
                {"label": "Reject", "href": "/r", "method": "POST"},
            ],
            cols=self.COLUMNS,
            page=_Page(),
        )
        for marker in ("dh-title", "ab", "dt-wrap", "dt-foot", "Waiting for sign-off."):
            self.assertIn(marker, out, f"missing {marker}")
        self.assertEqual(out.count("btn primary"), 1)


class SharedLayerIsLoadedByBothShellsTests(unittest.TestCase):
    """The states were unusable off the dashboard because their CSS was
    page-scoped. Both shells must load the shared component layer."""

    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_both_base_templates_load_components_css(self):
        env = self.app.jinja_env
        for shell in ("base.html", "client/base.html"):
            src = env.loader.get_source(env, shell)[0]
            self.assertIn("components.css", src, f"{shell} does not load the shared layer")


if __name__ == "__main__":
    unittest.main()
