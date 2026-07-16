from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import db
from app.audit import record_audit
from app.models import ClientCompany, PayrollItem, PayrollRun
from app.tenancy import platform_required

payslip_bp = Blueprint("payslip", __name__, url_prefix="/payslip")


@payslip_bp.route("")
@platform_required
def index():
    client_id = request.args.get("client_id", type=int)
    run_id = request.args.get("run_id", type=int)
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    runs = []
    selected_client = None
    selected_run = None
    items = []

    if client_id:
        selected_client = db.get_or_404(ClientCompany, client_id)
        runs = (
            PayrollRun.query.filter_by(client_company_id=selected_client.id)
            .order_by(PayrollRun.created_at.desc())
            .all()
        )

    if run_id:
        selected_run = db.get_or_404(PayrollRun, run_id)
        if selected_client and selected_run.client_company_id != selected_client.id:
            flash("Selected payroll run does not belong to that client.", "warning")
            return redirect(url_for("payslip.index", client_id=selected_client.id))
        items = selected_run.items

    return render_template(
        "payslip/index.html",
        clients=clients,
        selected_client=selected_client,
        selected_run=selected_run,
        runs=runs,
        items=items,
    )


@payslip_bp.route("/generate", methods=["POST"])
@platform_required
def generate():
    item_ids = [int(item_id) for item_id in request.form.getlist("payroll_item_ids") if item_id]
    if not item_ids:
        flash("Select at least one employee payroll record.", "warning")
        return redirect(url_for("payslip.index"))

    items = PayrollItem.query.filter(PayrollItem.id.in_(item_ids)).all()
    for item in items:
        record_audit(
            "Payslip generated",
            item,
            f"Payslip generated for {item.full_name} from payroll run #{item.payroll_run_id}.",
        )
    db.session.commit()
    return render_template("payslip/generated.html", items=items)
