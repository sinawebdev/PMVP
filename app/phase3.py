from datetime import date, timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.models import (
    AttendanceRecord,
    CleaningJob,
    ClientCompany,
    ClientServiceRequest,
    GoodsSupplyOrder,
    Invoice,
    InvoicePayment,
    Product,
    Supplier,
    WorkerAssignment,
)
from app.operations_services import parse_date, parse_time
from app.phase3_services import (
    INVOICE_STATUSES,
    INVOICE_TYPES,
    ORDER_STATUSES,
    PAYMENT_METHODS,
    PRODUCT_CATEGORIES,
    REQUEST_PRIORITIES,
    REQUEST_STATUSES,
    REQUEST_TYPES,
    add_goods_order_item,
    add_invoice_item,
    create_invoice_for_goods_order,
    deliver_goods_order,
    export_goods_order_excel,
    export_invoice_excel,
    export_invoice_pdf,
    next_invoice_number,
    next_order_number,
    recalculate_goods_order,
    recalculate_invoice,
)

phase3_bp = Blueprint("phase3", __name__)

ACCOUNTS_ROLES = ("admin", "md", "accounts_officer")
OPS_ROLES = ("admin", "md", "operations_supervisor")
GOODS_ROLES = ("admin", "md", "accounts_officer", "operations_supervisor")


def client_or_404():
    if current_user.role != "client_user" or not current_user.client_company_id:
        flash("This area is only for client portal users.", "warning")
        return None
    return current_user.client_company


def client_scoped_or_redirect(row):
    if current_user.role == "client_user" and row.client_company_id != current_user.client_company_id:
        flash("You do not have access to that client record.", "warning")
        return False
    return True


def clients_for_select():
    return ClientCompany.query.order_by(ClientCompany.name).all()


@phase3_bp.route("/client-portal")
@role_required("client_user")
def client_portal():
    client = client_or_404()
    if client is None:
        return redirect(url_for("auth.login"))
    today = date.today()
    month_start = today.replace(day=1)
    assignments = WorkerAssignment.query.filter_by(client_company_id=client.id, status="Active").all()
    attendance = AttendanceRecord.query.filter(
        AttendanceRecord.client_company_id == client.id,
        AttendanceRecord.work_date >= month_start,
    ).all()
    jobs = CleaningJob.query.filter(
        CleaningJob.client_company_id == client.id,
        CleaningJob.scheduled_date >= month_start,
    ).all()
    invoices = Invoice.query.filter_by(client_company_id=client.id).order_by(Invoice.invoice_date.desc()).all()
    requests = ClientServiceRequest.query.filter_by(client_company_id=client.id).order_by(ClientServiceRequest.created_at.desc()).all()
    orders = GoodsSupplyOrder.query.filter_by(client_company_id=client.id).order_by(GoodsSupplyOrder.order_date.desc()).all()
    return render_template(
        "client_portal.html",
        client=client,
        assignments=assignments,
        attendance=attendance,
        jobs=jobs,
        invoices=invoices,
        requests=requests,
        orders=orders,
        cards={
            "active_workers": len(assignments),
            "attendance_this_month": len(attendance),
            "cleaning_jobs_this_month": len(jobs),
            "pending_requests": sum(1 for row in requests if row.status in {"Submitted", "Under Review"}),
            "outstanding_invoices": sum(1 for row in invoices if row.balance_due > 0),
            "goods_orders": len(orders),
            "completed_jobs": sum(1 for job in jobs if job.status == "Completed"),
            "pending_feedback": sum(1 for job in jobs if job.status == "Completed" and not job.completion_report),
        },
        attendance_summary={
            "present": sum(1 for row in attendance if row.attendance_status == "Present"),
            "absent": sum(1 for row in attendance if row.attendance_status == "Absent"),
            "late": sum(1 for row in attendance if row.attendance_status == "Late"),
            "hours": sum(row.hours_worked or 0 for row in attendance),
            "overtime": sum(row.overtime_hours or 0 for row in attendance),
        },
    )


@phase3_bp.route("/client-portal/requests")
@role_required("client_user")
def client_requests():
    rows = ClientServiceRequest.query.filter_by(client_company_id=current_user.client_company_id).order_by(ClientServiceRequest.created_at.desc()).all()
    return render_template("client_requests.html", requests=rows)


@phase3_bp.route("/client-portal/requests/new", methods=["GET", "POST"])
@role_required("client_user")
def client_new_request():
    if request.method == "POST":
        row = ClientServiceRequest(
            client_company_id=current_user.client_company_id,
            submitted_by=current_user.id,
            request_type=request.form["request_type"],
            title=request.form["title"],
            description=request.form.get("description"),
            requested_date=parse_date(request.form.get("requested_date")),
            preferred_start_time=parse_time(request.form.get("preferred_start_time")),
            preferred_end_time=parse_time(request.form.get("preferred_end_time")),
            location=request.form.get("location"),
            number_of_workers_requested=int(request.form.get("number_of_workers_requested") or 0),
            priority=request.form.get("priority", "Normal"),
            status="Submitted",
        )
        db.session.add(row)
        db.session.flush()
        record_audit("Client request submitted", row, row.title)
        db.session.commit()
        flash("Request submitted.", "success")
        return redirect(url_for("phase3.client_request_detail", request_id=row.id))
    return render_template("client_request_form.html", request_types=REQUEST_TYPES, priorities=REQUEST_PRIORITIES)


@phase3_bp.route("/client-portal/requests/<int:request_id>")
@role_required("client_user")
def client_request_detail(request_id):
    row = db.get_or_404(ClientServiceRequest, request_id)
    if not client_scoped_or_redirect(row):
        return redirect(url_for("phase3.client_requests"))
    return render_template("client_request_detail.html", request_row=row)


@phase3_bp.route("/service-requests")
@role_required(*OPS_ROLES)
def service_requests():
    query = ClientServiceRequest.query
    if request.args.get("client_id"):
        query = query.filter_by(client_company_id=request.args.get("client_id"))
    if request.args.get("status"):
        query = query.filter_by(status=request.args.get("status"))
    return render_template("service_requests.html", requests=query.order_by(ClientServiceRequest.created_at.desc()).all(), clients=clients_for_select(), statuses=REQUEST_STATUSES)


@phase3_bp.route("/service-requests/<int:request_id>")
@role_required(*OPS_ROLES)
def service_request_detail(request_id):
    row = db.get_or_404(ClientServiceRequest, request_id)
    return render_template("service_request_detail.html", request_row=row)


@phase3_bp.route("/service-requests/<int:request_id>/approve", methods=["POST"])
@role_required(*OPS_ROLES)
def approve_request(request_id):
    row = db.get_or_404(ClientServiceRequest, request_id)
    row.status = "Approved"
    row.internal_notes = request.form.get("internal_notes") or row.internal_notes
    record_audit("Request approved", row, row.title)
    db.session.commit()
    flash("Service request approved.", "success")
    return redirect(url_for("phase3.service_request_detail", request_id=row.id))


@phase3_bp.route("/service-requests/<int:request_id>/reject", methods=["POST"])
@role_required(*OPS_ROLES)
def reject_request(request_id):
    row = db.get_or_404(ClientServiceRequest, request_id)
    row.status = "Rejected"
    row.internal_notes = request.form.get("internal_notes") or row.internal_notes
    record_audit("Request rejected", row, row.title)
    db.session.commit()
    flash("Service request rejected.", "warning")
    return redirect(url_for("phase3.service_request_detail", request_id=row.id))


@phase3_bp.route("/invoices")
@role_required(*ACCOUNTS_ROLES)
def invoices():
    rows = Invoice.query.order_by(Invoice.invoice_date.desc()).all()
    return render_template("invoices.html", invoices=rows)


@phase3_bp.route("/invoices/new", methods=["GET", "POST"])
@role_required(*ACCOUNTS_ROLES)
def new_invoice():
    if request.method == "POST":
        invoice = Invoice(
            client_company_id=int(request.form["client_company_id"]),
            invoice_number=request.form.get("invoice_number") or next_invoice_number(),
            invoice_date=parse_date(request.form.get("invoice_date")) or date.today(),
            due_date=parse_date(request.form.get("due_date")),
            billing_period_start=parse_date(request.form.get("billing_period_start")),
            billing_period_end=parse_date(request.form.get("billing_period_end")),
            invoice_type=request.form.get("invoice_type", "Manual"),
            tax_amount=float(request.form.get("tax_amount") or 0),
            discount_amount=float(request.form.get("discount_amount") or 0),
            status=request.form.get("status", "Draft"),
            notes=request.form.get("notes"),
            created_by=current_user.id,
        )
        db.session.add(invoice)
        db.session.flush()
        if request.form.get("description"):
            add_invoice_item(invoice, request.form.get("description"), request.form.get("quantity") or 1, request.form.get("unit_price") or 0)
        recalculate_invoice(invoice)
        record_audit("Invoice created", invoice, invoice.invoice_number)
        db.session.commit()
        flash("Invoice created.", "success")
        return redirect(url_for("phase3.invoice_detail", invoice_id=invoice.id))
    return render_template("invoice_form.html", invoice=None, clients=clients_for_select(), invoice_types=INVOICE_TYPES, statuses=INVOICE_STATUSES)


@phase3_bp.route("/invoices/<int:invoice_id>")
@role_required(*ACCOUNTS_ROLES)
def invoice_detail(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    return render_template("invoice_detail.html", invoice=invoice, payment_methods=PAYMENT_METHODS)


@phase3_bp.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@role_required(*ACCOUNTS_ROLES)
def edit_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if request.method == "POST":
        invoice.status = request.form.get("status", invoice.status)
        invoice.notes = request.form.get("notes")
        invoice.tax_amount = float(request.form.get("tax_amount") or invoice.tax_amount or 0)
        invoice.discount_amount = float(request.form.get("discount_amount") or invoice.discount_amount or 0)
        recalculate_invoice(invoice)
        db.session.commit()
        flash("Invoice updated.", "success")
        return redirect(url_for("phase3.invoice_detail", invoice_id=invoice.id))
    return render_template("invoice_form.html", invoice=invoice, clients=clients_for_select(), invoice_types=INVOICE_TYPES, statuses=INVOICE_STATUSES)


@phase3_bp.route("/invoices/<int:invoice_id>/record-payment", methods=["POST"])
@role_required(*ACCOUNTS_ROLES)
def record_invoice_payment(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    payment = InvoicePayment(
        invoice_id=invoice.id,
        payment_date=parse_date(request.form.get("payment_date")) or date.today(),
        amount_paid=float(request.form.get("amount_paid") or 0),
        payment_method=request.form.get("payment_method", "Bank Transfer"),
        reference_number=request.form.get("reference_number"),
        received_by=current_user.id,
        notes=request.form.get("notes"),
    )
    db.session.add(payment)
    db.session.flush()
    recalculate_invoice(invoice)
    record_audit("Payment recorded", invoice, payment.reference_number or "")
    db.session.commit()
    flash("Invoice payment recorded.", "success")
    return redirect(url_for("phase3.invoice_detail", invoice_id=invoice.id))


@phase3_bp.route("/invoices/<int:invoice_id>/export-pdf")
@role_required(*ACCOUNTS_ROLES)
def export_invoice_pdf_route(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    return send_file(export_invoice_pdf(invoice, current_app.config["EXPORT_FOLDER"]), as_attachment=True)


@phase3_bp.route("/invoices/<int:invoice_id>/export-excel")
@role_required(*ACCOUNTS_ROLES)
def export_invoice_excel_route(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    return send_file(export_invoice_excel(invoice, current_app.config["EXPORT_FOLDER"]), as_attachment=True)


@phase3_bp.route("/client-portal/invoices")
@role_required("client_user")
def client_invoices():
    rows = Invoice.query.filter_by(client_company_id=current_user.client_company_id).order_by(Invoice.invoice_date.desc()).all()
    return render_template("client_invoices.html", invoices=rows)


@phase3_bp.route("/client-portal/invoices/<int:invoice_id>")
@role_required("client_user")
def client_invoice_detail(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if not client_scoped_or_redirect(invoice):
        return redirect(url_for("phase3.client_invoices"))
    return render_template("client_invoice_detail.html", invoice=invoice)


@phase3_bp.route("/suppliers")
@role_required(*GOODS_ROLES)
def suppliers():
    return render_template("suppliers.html", suppliers=Supplier.query.order_by(Supplier.name).all())


@phase3_bp.route("/suppliers/new", methods=["GET", "POST"])
@role_required(*GOODS_ROLES)
def new_supplier():
    if request.method == "POST":
        supplier = Supplier(
            name=request.form["name"],
            contact_person=request.form.get("contact_person"),
            phone=request.form.get("phone"),
            email=request.form.get("email"),
            address=request.form.get("address"),
            status=request.form.get("status", "Active"),
        )
        db.session.add(supplier)
        db.session.commit()
        flash("Supplier saved.", "success")
        return redirect(url_for("phase3.suppliers"))
    return render_template("supplier_form.html")


@phase3_bp.route("/products")
@role_required(*GOODS_ROLES)
def products():
    products = Product.query.order_by(Product.product_name).all()
    low_stock = [product for product in products if product.current_stock <= product.reorder_level]
    movements = []
    pending_orders = GoodsSupplyOrder.query.filter(GoodsSupplyOrder.status.in_(["Submitted", "Approved", "Processing"])).all()
    return render_template("products.html", products=products, low_stock=low_stock, movements=movements, pending_orders=pending_orders)


@phase3_bp.route("/products/new", methods=["GET", "POST"])
@role_required(*GOODS_ROLES)
def new_product():
    if request.method == "POST":
        product = Product(
            product_name=request.form["product_name"],
            sku=request.form["sku"],
            category=request.form.get("category", "General Goods"),
            unit=request.form.get("unit", "Each"),
            default_unit_price=float(request.form.get("default_unit_price") or 0),
            current_stock=float(request.form.get("current_stock") or 0),
            reorder_level=float(request.form.get("reorder_level") or 0),
            supplier_id=request.form.get("supplier_id") or None,
            status=request.form.get("status", "Active"),
        )
        db.session.add(product)
        db.session.commit()
        flash("Product saved.", "success")
        return redirect(url_for("phase3.products"))
    return render_template("product_form.html", suppliers=Supplier.query.order_by(Supplier.name).all(), categories=PRODUCT_CATEGORIES)


@phase3_bp.route("/goods-orders")
@role_required(*GOODS_ROLES)
def goods_orders():
    return render_template("goods_orders.html", orders=GoodsSupplyOrder.query.order_by(GoodsSupplyOrder.order_date.desc()).all())


def save_goods_order_from_form(order=None, client_company_id=None):
    order = order or GoodsSupplyOrder(order_number=next_order_number(), order_date=date.today())
    order.client_company_id = client_company_id or int(request.form["client_company_id"])
    order.order_date = parse_date(request.form.get("order_date")) or date.today()
    order.requested_delivery_date = parse_date(request.form.get("requested_delivery_date"))
    order.delivery_location = request.form.get("delivery_location")
    order.status = request.form.get("status", order.status or "Draft")
    order.notes = request.form.get("notes")
    if not order.created_by:
        order.created_by = current_user.id
    return order


@phase3_bp.route("/goods-orders/new", methods=["GET", "POST"])
@role_required(*GOODS_ROLES)
def new_goods_order():
    if request.method == "POST":
        order = save_goods_order_from_form()
        db.session.add(order)
        db.session.flush()
        product = db.session.get(Product, int(request.form["product_id"]))
        add_goods_order_item(order, product, request.form.get("quantity") or 1)
        recalculate_goods_order(order)
        record_audit("Goods order created", order, order.order_number)
        db.session.commit()
        flash("Goods order saved.", "success")
        return redirect(url_for("phase3.goods_order_detail", order_id=order.id))
    return render_template("goods_order_form.html", order=None, clients=clients_for_select(), products=Product.query.order_by(Product.product_name).all(), statuses=ORDER_STATUSES, client_mode=False)


@phase3_bp.route("/goods-orders/<int:order_id>")
@role_required(*GOODS_ROLES)
def goods_order_detail(order_id):
    order = db.get_or_404(GoodsSupplyOrder, order_id)
    return render_template("goods_order_detail.html", order=order)


@phase3_bp.route("/goods-orders/<int:order_id>/edit", methods=["GET", "POST"])
@role_required(*GOODS_ROLES)
def edit_goods_order(order_id):
    order = db.get_or_404(GoodsSupplyOrder, order_id)
    if request.method == "POST":
        save_goods_order_from_form(order)
        recalculate_goods_order(order)
        db.session.commit()
        flash("Goods order updated.", "success")
        return redirect(url_for("phase3.goods_order_detail", order_id=order.id))
    return render_template("goods_order_form.html", order=order, clients=clients_for_select(), products=Product.query.order_by(Product.product_name).all(), statuses=ORDER_STATUSES, client_mode=False)


@phase3_bp.route("/goods-orders/<int:order_id>/approve", methods=["POST"])
@role_required(*GOODS_ROLES)
def approve_goods_order(order_id):
    order = db.get_or_404(GoodsSupplyOrder, order_id)
    order.status = "Approved"
    db.session.commit()
    flash("Goods order approved.", "success")
    return redirect(url_for("phase3.goods_order_detail", order_id=order.id))


@phase3_bp.route("/goods-orders/<int:order_id>/deliver", methods=["POST"])
@role_required(*GOODS_ROLES)
def deliver_order(order_id):
    order = db.get_or_404(GoodsSupplyOrder, order_id)
    warnings = deliver_goods_order(order, current_user.id, allow_negative_stock=current_user.role in {"admin", "md"})
    for warning in warnings:
        flash(warning, "warning")
    if not any("below requested" in warning for warning in warnings):
        record_audit("Goods order delivered", order, order.order_number)
        flash("Goods order delivered.", "success")
    db.session.commit()
    return redirect(url_for("phase3.goods_order_detail", order_id=order.id))


@phase3_bp.route("/goods-orders/<int:order_id>/generate-invoice", methods=["POST"])
@role_required(*ACCOUNTS_ROLES)
def generate_goods_invoice(order_id):
    order = db.get_or_404(GoodsSupplyOrder, order_id)
    invoice = create_invoice_for_goods_order(order, current_user.id)
    record_audit("Invoice created", invoice, f"Generated from {order.order_number}")
    db.session.commit()
    flash("Invoice generated from goods order.", "success")
    return redirect(url_for("phase3.invoice_detail", invoice_id=invoice.id))


@phase3_bp.route("/goods-orders/<int:order_id>/export-excel")
@role_required(*GOODS_ROLES)
def export_goods_order_excel_route(order_id):
    order = db.get_or_404(GoodsSupplyOrder, order_id)
    return send_file(export_goods_order_excel(order, current_app.config["EXPORT_FOLDER"]), as_attachment=True)


@phase3_bp.route("/client-portal/goods-orders")
@role_required("client_user")
def client_goods_orders():
    orders = GoodsSupplyOrder.query.filter_by(client_company_id=current_user.client_company_id).order_by(GoodsSupplyOrder.order_date.desc()).all()
    return render_template("client_goods_orders.html", orders=orders)


@phase3_bp.route("/client-portal/goods-orders/new", methods=["GET", "POST"])
@role_required("client_user")
def client_new_goods_order():
    if request.method == "POST":
        order = save_goods_order_from_form(client_company_id=current_user.client_company_id)
        order.status = "Submitted"
        db.session.add(order)
        db.session.flush()
        product = db.session.get(Product, int(request.form["product_id"]))
        add_goods_order_item(order, product, request.form.get("quantity") or 1)
        recalculate_goods_order(order)
        db.session.commit()
        flash("Goods order request submitted.", "success")
        return redirect(url_for("phase3.client_goods_order_detail", order_id=order.id))
    return render_template("goods_order_form.html", order=None, clients=[], products=Product.query.order_by(Product.product_name).all(), statuses=ORDER_STATUSES, client_mode=True)


@phase3_bp.route("/client-portal/goods-orders/<int:order_id>")
@role_required("client_user")
def client_goods_order_detail(order_id):
    order = db.get_or_404(GoodsSupplyOrder, order_id)
    if not client_scoped_or_redirect(order):
        return redirect(url_for("phase3.client_goods_orders"))
    return render_template("client_goods_order_detail.html", order=order)


@phase3_bp.route("/client-portal/assigned-workers")
@role_required("client_user")
def client_assigned_workers():
    assignments = WorkerAssignment.query.filter_by(client_company_id=current_user.client_company_id, status="Active").all()
    return render_template("client_assigned_workers.html", assignments=assignments)


@phase3_bp.route("/client-portal/attendance")
@role_required("client_user")
def client_attendance_summary():
    records = AttendanceRecord.query.filter_by(client_company_id=current_user.client_company_id).order_by(AttendanceRecord.work_date.desc()).all()
    return render_template("client_attendance.html", records=records)


@phase3_bp.route("/client-portal/cleaning-jobs")
@role_required("client_user")
def client_cleaning_jobs():
    jobs = CleaningJob.query.filter_by(client_company_id=current_user.client_company_id).order_by(CleaningJob.scheduled_date.desc()).all()
    return render_template("client_cleaning_jobs.html", jobs=jobs)
