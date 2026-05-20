from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app import db
from app.auth import role_required
from app.models import ClientCompany, Proposal

proposals_bp = Blueprint("proposals", __name__, url_prefix="/proposals")


@proposals_bp.route("", methods=["GET", "POST"])
@role_required("admin", "md", "accounts_officer")
def proposals():
    clients = ClientCompany.query.filter_by(status="Active").order_by(ClientCompany.name).all()
    if request.method == "POST":
        proposal = Proposal(
            client_company_id=request.form.get("client_company_id") or None,
            title=request.form["title"],
            service_summary=request.form["service_summary"],
            proposed_amount=float(request.form.get("proposed_amount") or 0),
            status=request.form.get("status", "Draft"),
            drafted_by=current_user.id,
        )
        db.session.add(proposal)
        db.session.commit()
        flash("Proposal draft saved.", "success")
        return redirect(url_for("proposals.proposals"))

    proposal_rows = Proposal.query.order_by(Proposal.created_at.desc()).all()
    return render_template(
        "proposals.html",
        proposals=proposal_rows,
        clients=clients,
    )
