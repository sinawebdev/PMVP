import os
from datetime import date, timedelta

from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app import db
from app.models import (
    GoodsSupplyOrder,
    GoodsSupplyOrderItem,
    InventoryMovement,
    Invoice,
    InvoiceItem,
    Product,
)

DEFAULT_TAX_RATE = 0.0

REQUEST_TYPES = ["Labour Request", "Cleaning Request", "Goods Supply Request", "Complaint", "General Inquiry"]
REQUEST_PRIORITIES = ["Low", "Normal", "High", "Urgent"]
REQUEST_STATUSES = ["Submitted", "Under Review", "Approved", "Rejected", "In Progress", "Completed", "Cancelled"]
INVOICE_TYPES = ["Payroll/Labour Invoice", "Cleaning Services Invoice", "Goods Supply Invoice", "Mixed Services Invoice"]
INVOICE_STATUSES = ["Draft", "Sent", "Partially Paid", "Paid", "Overdue", "Cancelled"]
PAYMENT_METHODS = ["Bank Transfer", "Cash", "Cheque", "Mobile Money", "Other"]
PRODUCT_CATEGORIES = ["Cleaning Supplies", "Office Supplies", "Safety Equipment", "General Goods", "Consumables", "Other"]
ORDER_STATUSES = ["Draft", "Submitted", "Approved", "Processing", "Delivered", "Cancelled", "Invoiced"]


def next_invoice_number():
    return f"INV-{date.today().year}-{(Invoice.query.count() + 1):04d}"


def next_order_number():
    return f"GSO-{date.today().year}-{(GoodsSupplyOrder.query.count() + 1):04d}"


def recalculate_invoice(invoice):
    invoice.subtotal = round(sum(item.line_total or 0 for item in invoice.items), 2)
    invoice.total_amount = round((invoice.subtotal or 0) + (invoice.tax_amount or 0) - (invoice.discount_amount or 0), 2)
    invoice.amount_paid = round(sum(payment.amount_paid or 0 for payment in invoice.payments), 2)
    invoice.balance_due = round(max(invoice.total_amount - invoice.amount_paid, 0), 2)
    if invoice.total_amount and invoice.balance_due <= 0:
        invoice.status = "Paid"
    elif invoice.amount_paid > 0:
        invoice.status = "Partially Paid"
    elif invoice.due_date and invoice.due_date < date.today() and invoice.status not in {"Paid", "Cancelled"}:
        invoice.status = "Overdue"
    return invoice


def recalculate_goods_order(order, tax_rate=DEFAULT_TAX_RATE):
    for item in order.items:
        item.line_total = round((item.quantity or 0) * (item.unit_price or 0), 2)
    order.subtotal = round(sum(item.line_total or 0 for item in order.items), 2)
    order.tax_amount = round(order.subtotal * tax_rate, 2)
    order.total_amount = round(order.subtotal + order.tax_amount, 2)
    return order


def add_invoice_item(invoice, description, quantity, unit_price, source_type="Manual", source_id=None):
    item = InvoiceItem(
        invoice=invoice,
        description=description,
        quantity=float(quantity or 0),
        unit_price=float(unit_price or 0),
        line_total=round(float(quantity or 0) * float(unit_price or 0), 2),
        source_type=source_type,
        source_id=source_id,
    )
    db.session.add(item)
    return item


def create_invoice_for_goods_order(order, created_by):
    invoice = Invoice(
        client_company_id=order.client_company_id,
        invoice_number=next_invoice_number(),
        invoice_date=date.today(),
        due_date=date.today() + timedelta(days=14),
        billing_period_start=order.order_date,
        billing_period_end=order.requested_delivery_date or order.order_date,
        invoice_type="Goods Supply Invoice",
        tax_amount=order.tax_amount or 0,
        status="Draft",
        notes=f"Generated from goods order {order.order_number}.",
        created_by=created_by,
    )
    db.session.add(invoice)
    db.session.flush()
    for item in order.items:
        add_invoice_item(
            invoice,
            item.description or item.product.product_name,
            item.quantity,
            item.unit_price,
            "GoodsSupplyOrder",
            order.id,
        )
    recalculate_invoice(invoice)
    order.status = "Invoiced"
    return invoice


def deliver_goods_order(order, user_id, allow_negative_stock=False):
    warnings = []
    for item in order.items:
        product = item.product
        if product.current_stock < item.quantity and not allow_negative_stock:
            warnings.append(f"{product.product_name} stock is below requested quantity.")
        if product.current_stock - item.quantity < product.reorder_level:
            warnings.append(f"{product.product_name} will be below reorder level.")
    if any("below requested" in warning for warning in warnings) and not allow_negative_stock:
        return warnings
    for item in order.items:
        product = item.product
        product.current_stock = round((product.current_stock or 0) - (item.quantity or 0), 2)
        db.session.add(
            InventoryMovement(
                product_id=product.id,
                movement_type="Stock Out",
                quantity=item.quantity,
                reference_type="GoodsSupplyOrder",
                reference_id=order.id,
                notes=f"Delivered goods order {order.order_number}.",
                created_by=user_id,
            )
        )
    order.status = "Delivered"
    return warnings


def export_invoice_pdf(invoice, export_folder):
    os.makedirs(export_folder, exist_ok=True)
    file_path = os.path.join(export_folder, f"{invoice.invoice_number}.pdf")
    pdf = canvas.Canvas(file_path, pagesize=A4)
    width, height = A4
    y = height - 60
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(50, y, "Chrisnat Limited")
    y -= 30
    pdf.setFont("Helvetica", 11)
    lines = [
        f"Invoice: {invoice.invoice_number}",
        f"Client: {invoice.client_company.name}",
        f"Date: {invoice.invoice_date}",
        f"Due Date: {invoice.due_date}",
    ]
    for line in lines:
        pdf.drawString(50, y, line)
        y -= 18
    y -= 10
    for item in invoice.items:
        pdf.drawString(50, y, item.description[:55])
        pdf.drawRightString(width - 50, y, f"{item.line_total:,.2f}")
        y -= 18
    y -= 10
    totals = [
        ("Subtotal", invoice.subtotal),
        ("Tax", invoice.tax_amount),
        ("Total", invoice.total_amount),
        ("Amount Paid", invoice.amount_paid),
        ("Balance Due", invoice.balance_due),
    ]
    for label, amount in totals:
        pdf.drawRightString(width - 140, y, label)
        pdf.drawRightString(width - 50, y, f"{amount or 0:,.2f}")
        y -= 18
    if invoice.notes:
        pdf.drawString(50, y - 10, f"Notes: {invoice.notes[:90]}")
    pdf.save()
    return file_path


def export_invoice_excel(invoice, export_folder):
    os.makedirs(export_folder, exist_ok=True)
    file_path = os.path.join(export_folder, f"{invoice.invoice_number}.xlsx")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Chrisnat Limited"])
    sheet.append(["Invoice Number", invoice.invoice_number])
    sheet.append(["Client Company", invoice.client_company.name])
    sheet.append(["Date", invoice.invoice_date.isoformat()])
    sheet.append(["Due Date", invoice.due_date.isoformat() if invoice.due_date else ""])
    sheet.append([])
    sheet.append(["Description", "Quantity", "Unit Price", "Line Total", "Source Type"])
    for item in invoice.items:
        sheet.append([item.description, item.quantity, item.unit_price, item.line_total, item.source_type])
    sheet.append([])
    sheet.append(["Subtotal", invoice.subtotal])
    sheet.append(["Tax", invoice.tax_amount])
    sheet.append(["Total", invoice.total_amount])
    sheet.append(["Amount Paid", invoice.amount_paid])
    sheet.append(["Balance Due", invoice.balance_due])
    workbook.save(file_path)
    return file_path


def export_goods_order_excel(order, export_folder):
    os.makedirs(export_folder, exist_ok=True)
    file_path = os.path.join(export_folder, f"{order.order_number}.xlsx")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Goods Order"
    sheet.append(["Order Number", order.order_number])
    sheet.append(["Client Company", order.client_company.name])
    sheet.append(["Delivery Location", order.delivery_location])
    sheet.append([])
    sheet.append(["Product", "Description", "Quantity", "Unit Price", "Line Total"])
    for item in order.items:
        sheet.append([item.product.product_name, item.description, item.quantity, item.unit_price, item.line_total])
    workbook.save(file_path)
    return file_path


def add_goods_order_item(order, product, quantity):
    item = GoodsSupplyOrderItem(
        goods_supply_order=order,
        product_id=product.id,
        description=product.product_name,
        quantity=float(quantity or 0),
        unit_price=product.default_unit_price or 0,
    )
    db.session.add(item)
    return item
