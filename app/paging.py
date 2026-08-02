"""One way to page a list — Phase 7, Task 7.1.

Every surface that renders a collection a customer can grow needs the same three
things: read a page number off the query string without trusting it, cap the page
size, and hand the template a Flask-SQLAlchemy ``Pagination`` so
``macros/ui.html::pagination`` can draw the footer.

Written once so that adding a list page is not an invitation to write
``.all()`` again. The lint in tests/test_constitution_lint.py enforces the other
half: a table body must iterate a ``Pagination.items``, or its template must say
in the ALLOW list why its collection is bounded by construction.
"""

from flask import has_request_context, request

# 50 rows is the operator payroll list's long-standing page size; keeping one
# number means the whole product scrolls at the same rate.
DEFAULT_PER_PAGE = 50


def page_arg(arg="page"):
    """The requested page number, clamped to >= 1.

    A junk value is page 1, not a 500 — a page number arrives from a URL a user
    can edit. Outside a request (a worker, a test calling a context builder
    directly) there is no page to read, and page 1 is the honest answer."""
    if not has_request_context():
        return 1
    try:
        return max(1, int(request.args.get(arg, 1)))
    except (TypeError, ValueError):
        return 1


def paginate(query, per_page=DEFAULT_PER_PAGE, arg="page"):
    """Page ``query``. ``error_out=False`` so a page past the end renders an
    empty last page rather than a 404 the user cannot act on."""
    return query.paginate(page=page_arg(arg), per_page=per_page, error_out=False)


def paginate_list(rows, per_page=DEFAULT_PER_PAGE, arg="page"):
    """The same contract for an in-memory list.

    A few surfaces assemble their rows in Python (a run's delivery status pairs
    each payroll item with its latest delivery, for instance) and so have no
    query to page. They still must not render ten thousand rows, and their
    template should not have to care which kind of collection it was handed.
    """
    rows = list(rows)
    page = page_arg(arg)
    total = len(rows)
    start = (page - 1) * per_page
    return _ListPage(rows[start : start + per_page], page, per_page, total)


class _ListPage:
    """The subset of Flask-SQLAlchemy's Pagination that the template contract
    uses. Deliberately not a subclass: that class wants a query."""

    def __init__(self, items, page, per_page, total):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total
        self.pages = max(1, -(-total // per_page)) if total else 0

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages

    @property
    def prev_num(self):
        return self.page - 1 if self.has_prev else None

    @property
    def next_num(self):
        return self.page + 1 if self.has_next else None

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)
