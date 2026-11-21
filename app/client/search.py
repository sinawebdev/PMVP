"""Search in the client portal: the company's pages and its employees.

The portal had no search at all. It now has the same box as the operator
console (app.page_search does the matching for both), scoped to the company.
"""
from flask import jsonify, render_template, request

from app.client import _company, client_bp
from app.page_search import company_employees, find_pages, suggestions
from app.tenancy import tenant_required


@client_bp.route("/search")
@tenant_required
def search():
    q = (request.args.get("q") or "").strip()[:100]
    pages = find_pages(q) if q else []
    employees = company_employees(q) if q else []
    return render_template("client/search.html", company=_company(), q=q,
                           pages=pages, employees=employees)


@client_bp.route("/search/suggest")
@tenant_required
def search_suggest():
    return jsonify(suggestions(request.args.get("q")))
