from collections import Counter
from datetime import date, datetime, timezone

import pandas as pd
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app import db
from app.audit import record_audit
from app.auth import role_required
from app.models import (
    AttendanceRecord,
    CleaningJob,
    CleaningJobWorker,
    ClientCompany,
    Employee,
    JobCompletionReport,
    PayrollItem,
    PayrollRun,
    User,
    WorkerAssignment,
)
from app.operations_services import (
    ASSIGNMENT_STATUSES,
    ATTENDANCE_STATUSES,
    COMPLETION_STATUSES,
    JOB_STATUSES,
    JOB_TYPES,
    JOB_WORKER_ROLES,
    SHIFT_TYPES,
    active_assignment_for,
    apply_attendance_calculations,
    assignment_warnings,
    attendance_warnings,
    availability_for,
    availability_groups,
    cleaning_worker_warnings,
    month_date_range,
    parse_date,
    parse_time,
)

operations_bp = Blueprint("operations", __name__)

OPS_ROLES = ("admin", "md", "operations_supervisor")


def employees_for_select():
    return Employee.query.order_by(Employee.full_name).all()


def clients_for_select():
    return ClientCompany.query.order_by(ClientCompany.name).all()


def supervisors_for_select():
    return User.query.filter(User.role.in_(["admin", "md", "operations_supervisor"])).order_by(User.name).all()


def assignment_from_form(assignment=None):
    assignment = assignment or WorkerAssignment()
    assignment.employee_id = int(request.form["employee_id"])
    assignment.client_company_id = int(request.form["client_company_id"])
    assignment.role = request.form.get("role", "").strip() or "Worker"
    assignment.site_location = request.form.get("site_location")
    assignment.assignment_start_date = parse_date(request.form.get("assignment_start_date")) or date.today()
    assignment.assignment_end_date = parse_date(request.form.get("assignment_end_date"))
    assignment.status = request.form.get("status", "Pending")
    assignment.shift_type = request.form.get("shift_type", "Full Day")
    assignment.notes = request.form.get("notes")
    if not assignment.created_by and current_user.is_authenticated:
        assignment.created_by = current_user.id
    return assignment


@operations_bp.route("/operations-dashboard")
@role_required(*OPS_ROLES)
def operations_dashboard():
    today = date.today()
    availability = availability_groups()
    clients = ClientCompany.query.order_by(ClientCompany.name).all()
    attendance_today = AttendanceRecord.query.filter_by(work_date=today).all()
    jobs_today = CleaningJob.query.filter_by(scheduled_date=today).order_by(CleaningJob.start_time).all()
    active_assignments = WorkerAssignment.query.filter_by(status="Active").all()
    overlapping = []
    for assignment in active_assignments:
        if "overlap" in " ".join(assignment_warnings(assignment)).lower():
            overlapping.append(assignment)
    missing_assignment = [
        employee for employee, _assignment in availability["Available"]
        if str(employee.status) == "Active"
    ]
    missing_clock_out = [
        record for record in attendance_today
        if record.attendance_status in {"Present", "Late"} and not record.clock_out
    ]
    jobs_without_workers = [
        job for job in CleaningJob.query.filter(CleaningJob.status.in_(["Scheduled", "In Progress"])).all()
        if not job.workers
    ]
    completed_without_reports = [
        job for job in CleaningJob.query.filter_by(status="Completed").all()
        if not job.completion_report
    ]
    from app.models import ClientServiceRequest, GoodsSupplyOrder

    pending_client_requests = ClientServiceRequest.query.filter(ClientServiceRequest.status.in_(["Submitted", "Under Review"])).all()
    approved_client_requests = ClientServiceRequest.query.filter_by(status="Approved").all()
    goods_pending_action = GoodsSupplyOrder.query.filter(GoodsSupplyOrder.status.in_(["Submitted", "Approved", "Processing"])).all()
    return render_template(
        "operations_dashboard.html",
        availability=availability,
        clients=clients,
        attendance_today=attendance_today,
        jobs_today=jobs_today,
        cards={
            "active_workers": Employee.query.filter_by(status="Active").count(),
            "available_workers": len(availability["Available"]),
            "assigned_workers": len(availability["Assigned"]),
            "on_leave_workers": len(availability["On Leave"]),
            "active_clients": ClientCompany.query.filter_by(status="Active").count(),
            "active_assignments": len(active_assignments),
            "attendance_today": len(attendance_today),
            "absentees_today": sum(1 for row in attendance_today if row.attendance_status == "Absent"),
            "late_today": sum(1 for row in attendance_today if row.attendance_status == "Late"),
            "jobs_today": len(jobs_today),
            "pending_reports": CleaningJob.query.filter_by(status="Completed").outerjoin(JobCompletionReport).filter(JobCompletionReport.id.is_(None)).count(),
            "pending_client_requests": len(pending_client_requests),
            "approved_requests": len(approved_client_requests),
            "goods_pending_action": len(goods_pending_action),
        },
        client_breakdown=[
            {
                "client": client,
                "active": WorkerAssignment.query.filter_by(client_company_id=client.id, status="Active").count(),
                "pending": WorkerAssignment.query.filter_by(client_company_id=client.id, status="Pending").count(),
                "completed": WorkerAssignment.query.filter_by(client_company_id=client.id, status="Completed").count(),
                "suspended": WorkerAssignment.query.filter_by(client_company_id=client.id, status="Suspended").count(),
            }
            for client in clients
        ],
        action_required={
            "overlapping": overlapping,
            "missing_assignment": missing_assignment,
            "missing_clock_out": missing_clock_out,
            "jobs_without_workers": jobs_without_workers,
            "completed_without_reports": completed_without_reports,
        },
    )


@operations_bp.route("/assignments")
@role_required(*OPS_ROLES)
def assignments():
    query = WorkerAssignment.query
    if request.args.get("client_id"):
        query = query.filter_by(client_company_id=request.args.get("client_id"))
    if request.args.get("employee_id"):
        query = query.filter_by(employee_id=request.args.get("employee_id"))
    if request.args.get("status"):
        query = query.filter_by(status=request.args.get("status"))
    return render_template(
        "assignments.html",
        assignments=query.order_by(WorkerAssignment.assignment_start_date.desc()).all(),
        clients=clients_for_select(),
        employees=employees_for_select(),
        statuses=ASSIGNMENT_STATUSES,
    )


@operations_bp.route("/assignments/new", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def new_assignment():
    assignment = WorkerAssignment()
    warnings = []
    if request.method == "POST":
        assignment = assignment_from_form(assignment)
        db.session.add(assignment)
        warnings = assignment_warnings(assignment)
        db.session.commit()
        record_audit("Assignment created", assignment, "; ".join(warnings))
        db.session.commit()
        for warning in warnings:
            flash(warning, "warning")
        flash("Worker assignment saved.", "success")
        return redirect(url_for("operations.assignment_detail", assignment_id=assignment.id))
    return render_template("assignment_form.html", assignment=assignment, clients=clients_for_select(), employees=employees_for_select(), statuses=ASSIGNMENT_STATUSES, shifts=SHIFT_TYPES, warnings=warnings)


@operations_bp.route("/assignments/<int:assignment_id>")
@role_required(*OPS_ROLES)
def assignment_detail(assignment_id):
    assignment = db.get_or_404(WorkerAssignment, assignment_id)
    return render_template("assignment_detail.html", assignment=assignment, warnings=assignment_warnings(assignment))


@operations_bp.route("/assignments/<int:assignment_id>/edit", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def edit_assignment(assignment_id):
    assignment = db.get_or_404(WorkerAssignment, assignment_id)
    warnings = []
    if request.method == "POST":
        assignment_from_form(assignment)
        warnings = assignment_warnings(assignment)
        record_audit("Assignment updated", assignment, "; ".join(warnings))
        db.session.commit()
        for warning in warnings:
            flash(warning, "warning")
        flash("Worker assignment updated.", "success")
        return redirect(url_for("operations.assignment_detail", assignment_id=assignment.id))
    return render_template("assignment_form.html", assignment=assignment, clients=clients_for_select(), employees=employees_for_select(), statuses=ASSIGNMENT_STATUSES, shifts=SHIFT_TYPES, warnings=warnings)


@operations_bp.route("/assignments/<int:assignment_id>/end", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def end_assignment(assignment_id):
    assignment = db.get_or_404(WorkerAssignment, assignment_id)
    if request.method == "POST":
        assignment.assignment_end_date = parse_date(request.form.get("assignment_end_date")) or date.today()
        assignment.status = "Completed"
        assignment.notes = request.form.get("notes") or assignment.notes
        record_audit("Assignment ended", assignment)
        db.session.commit()
        flash("Assignment ended.", "success")
        return redirect(url_for("operations.assignments"))
    return render_template("assignment_end.html", assignment=assignment)


@operations_bp.route("/assignments/<int:assignment_id>/transfer", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def transfer_assignment(assignment_id):
    assignment = db.get_or_404(WorkerAssignment, assignment_id)
    if request.method == "POST":
        assignment.status = "Transferred"
        assignment.assignment_end_date = parse_date(request.form.get("transfer_date")) or date.today()
        new_assignment_record = WorkerAssignment(
            employee_id=assignment.employee_id,
            client_company_id=int(request.form["client_company_id"]),
            role=request.form.get("role") or assignment.role,
            site_location=request.form.get("site_location"),
            assignment_start_date=assignment.assignment_end_date,
            status="Active",
            shift_type=request.form.get("shift_type") or assignment.shift_type,
            notes=request.form.get("notes"),
            created_by=current_user.id,
        )
        db.session.add(new_assignment_record)
        warnings = assignment_warnings(new_assignment_record)
        record_audit("Worker transferred", assignment, "; ".join(warnings))
        db.session.commit()
        for warning in warnings:
            flash(warning, "warning")
        flash("Worker transferred.", "success")
        return redirect(url_for("operations.assignment_detail", assignment_id=new_assignment_record.id))
    return render_template("assignment_transfer.html", assignment=assignment, clients=clients_for_select(), shifts=SHIFT_TYPES)


@operations_bp.route("/workers/availability")
@role_required(*OPS_ROLES)
def worker_availability():
    return render_template("worker_availability.html", availability=availability_groups())


def attendance_from_form(record=None):
    record = record or AttendanceRecord()
    record.employee_id = int(request.form["employee_id"])
    record.client_company_id = int(request.form["client_company_id"])
    record.work_date = parse_date(request.form.get("work_date")) or date.today()
    record.clock_in = parse_time(request.form.get("clock_in"))
    record.clock_out = parse_time(request.form.get("clock_out"))
    record.attendance_status = request.form.get("attendance_status", "Unknown")
    record.remarks = request.form.get("remarks")
    assignment = active_assignment_for(record.employee_id, record.work_date)
    record.assignment_id = assignment.id if assignment else None
    if not record.recorded_by and current_user.is_authenticated:
        record.recorded_by = current_user.id
    return record


@operations_bp.route("/attendance")
@role_required(*OPS_ROLES, "payroll_officer")
def attendance():
    query = AttendanceRecord.query
    if request.args.get("work_date"):
        query = query.filter_by(work_date=parse_date(request.args.get("work_date")))
    if request.args.get("client_id"):
        query = query.filter_by(client_company_id=request.args.get("client_id"))
    if request.args.get("employee_id"):
        query = query.filter_by(employee_id=request.args.get("employee_id"))
    if request.args.get("status"):
        query = query.filter_by(attendance_status=request.args.get("status"))
    return render_template("attendance.html", records=query.order_by(AttendanceRecord.work_date.desc()).all(), clients=clients_for_select(), employees=employees_for_select(), statuses=ATTENDANCE_STATUSES)


@operations_bp.route("/attendance/new", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def new_attendance():
    record = AttendanceRecord(work_date=date.today())
    warnings = []
    if request.method == "POST":
        record = attendance_from_form(record)
        warnings = attendance_warnings(record)
        db.session.add(record)
        db.session.commit()
        record_audit("Attendance recorded", record, "; ".join(warnings))
        db.session.commit()
        for warning in warnings:
            flash(warning, "warning")
        flash("Attendance recorded.", "success")
        return redirect(url_for("operations.attendance_detail", attendance_id=record.id))
    return render_template("attendance_form.html", record=record, clients=clients_for_select(), employees=employees_for_select(), statuses=ATTENDANCE_STATUSES, warnings=warnings)


@operations_bp.route("/attendance/<int:attendance_id>")
@role_required(*OPS_ROLES, "payroll_officer")
def attendance_detail(attendance_id):
    record = db.get_or_404(AttendanceRecord, attendance_id)
    return render_template("attendance_detail.html", record=record, warnings=attendance_warnings(record))


@operations_bp.route("/attendance/<int:attendance_id>/edit", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def edit_attendance(attendance_id):
    record = db.get_or_404(AttendanceRecord, attendance_id)
    warnings = []
    if request.method == "POST":
        attendance_from_form(record)
        warnings = attendance_warnings(record)
        record_audit("Attendance recorded", record, "; ".join(warnings))
        db.session.commit()
        for warning in warnings:
            flash(warning, "warning")
        flash("Attendance updated.", "success")
        return redirect(url_for("operations.attendance_detail", attendance_id=record.id))
    return render_template("attendance_form.html", record=record, clients=clients_for_select(), employees=employees_for_select(), statuses=ATTENDANCE_STATUSES, warnings=warnings)


@operations_bp.route("/attendance/summary")
@role_required(*OPS_ROLES, "payroll_officer")
def attendance_summary():
    rows = AttendanceRecord.query.all()
    summary = Counter(row.attendance_status for row in rows)
    return render_template("attendance_summary.html", summary=summary, records=rows)


@operations_bp.route("/attendance/upload", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def attendance_upload():
    if request.method == "POST":
        upload = request.files.get("attendance_file")
        if not upload:
            flash("Choose an Excel file first.", "warning")
            return redirect(url_for("operations.attendance_upload"))
        frame = pd.read_excel(upload)
        columns = {str(column).strip().lower(): column for column in frame.columns}
        preview = []
        for _, row in frame.iterrows():
            preview.append({
                "staff_id": row.get(columns.get("staff id"), ""),
                "employee_name": row.get(columns.get("employee name"), ""),
                "client_company": row.get(columns.get("client company"), ""),
                "date": str(row.get(columns.get("date"), ""))[:10],
                "clock_in": str(row.get(columns.get("clock in"), ""))[:5],
                "clock_out": str(row.get(columns.get("clock out"), ""))[:5],
                "status": row.get(columns.get("status"), "Unknown"),
            })
        session["attendance_preview"] = preview
        return redirect(url_for("operations.attendance_preview"))
    return render_template("attendance_upload.html")


@operations_bp.route("/attendance/preview", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def attendance_preview():
    preview = session.get("attendance_preview", [])
    if request.method == "POST":
        saved = 0
        for row in preview:
            employee = Employee.query.filter_by(staff_id=str(row.get("staff_id"))).first()
            client = ClientCompany.query.filter_by(name=str(row.get("client_company"))).first()
            if not employee or not client:
                continue
            record = AttendanceRecord(
                employee_id=employee.id,
                client_company_id=client.id,
                work_date=parse_date(row.get("date")) or date.today(),
                clock_in=parse_time(row.get("clock_in")),
                clock_out=parse_time(row.get("clock_out")),
                attendance_status=row.get("status") or "Unknown",
                recorded_by=current_user.id,
            )
            record.assignment_id = (active_assignment_for(employee.id, record.work_date) or record).id
            apply_attendance_calculations(record)
            db.session.add(record)
            saved += 1
        db.session.commit()
        session.pop("attendance_preview", None)
        flash(f"{saved} attendance records imported.", "success")
        return redirect(url_for("operations.attendance"))
    return render_template("attendance_preview.html", rows=preview)


@operations_bp.route("/cleaning-jobs")
@role_required(*OPS_ROLES)
def cleaning_jobs():
    query = CleaningJob.query
    if request.args.get("client_id"):
        query = query.filter_by(client_company_id=request.args.get("client_id"))
    if request.args.get("status"):
        query = query.filter_by(status=request.args.get("status"))
    if request.args.get("scheduled_date"):
        query = query.filter_by(scheduled_date=parse_date(request.args.get("scheduled_date")))
    return render_template("cleaning_jobs.html", jobs=query.order_by(CleaningJob.scheduled_date.desc()).all(), clients=clients_for_select(), statuses=JOB_STATUSES)


def cleaning_job_from_form(job=None):
    job = job or CleaningJob()
    job.client_company_id = int(request.form["client_company_id"])
    job.job_title = request.form.get("job_title", "").strip()
    job.job_type = request.form.get("job_type", "Regular Cleaning")
    job.location = request.form.get("location")
    job.scheduled_date = parse_date(request.form.get("scheduled_date")) or date.today()
    job.start_time = parse_time(request.form.get("start_time"))
    job.end_time = parse_time(request.form.get("end_time"))
    job.supervisor_id = request.form.get("supervisor_id") or None
    job.status = request.form.get("status", "Scheduled")
    job.checklist = request.form.get("checklist")
    job.notes = request.form.get("notes")
    if not job.created_by and current_user.is_authenticated:
        job.created_by = current_user.id
    return job


@operations_bp.route("/cleaning-jobs/new", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def new_cleaning_job():
    job = CleaningJob(scheduled_date=date.today())
    if request.method == "POST":
        job = cleaning_job_from_form(job)
        db.session.add(job)
        db.session.commit()
        record_audit("Cleaning job created", job)
        db.session.commit()
        flash("Cleaning job saved.", "success")
        return redirect(url_for("operations.cleaning_job_detail", job_id=job.id))
    return render_template("cleaning_job_form.html", job=job, clients=clients_for_select(), supervisors=supervisors_for_select(), job_types=JOB_TYPES, statuses=JOB_STATUSES)


@operations_bp.route("/cleaning-jobs/<int:job_id>")
@role_required(*OPS_ROLES)
def cleaning_job_detail(job_id):
    job = db.get_or_404(CleaningJob, job_id)
    return render_template("cleaning_job_detail.html", job=job)


@operations_bp.route("/cleaning-jobs/<int:job_id>/edit", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def edit_cleaning_job(job_id):
    job = db.get_or_404(CleaningJob, job_id)
    if request.method == "POST":
        cleaning_job_from_form(job)
        db.session.commit()
        flash("Cleaning job updated.", "success")
        return redirect(url_for("operations.cleaning_job_detail", job_id=job.id))
    return render_template("cleaning_job_form.html", job=job, clients=clients_for_select(), supervisors=supervisors_for_select(), job_types=JOB_TYPES, statuses=JOB_STATUSES)


@operations_bp.route("/cleaning-jobs/<int:job_id>/assign-workers", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def assign_cleaning_workers(job_id):
    job = db.get_or_404(CleaningJob, job_id)
    warnings = []
    if request.method == "POST":
        employee = db.get_or_404(Employee, int(request.form["employee_id"]))
        worker = CleaningJobWorker(
            cleaning_job_id=job.id,
            employee_id=employee.id,
            role=request.form.get("role", "Cleaner"),
            attendance_status=request.form.get("attendance_status", "Unknown"),
            notes=request.form.get("notes"),
        )
        warnings = cleaning_worker_warnings(job, employee)
        db.session.add(worker)
        db.session.commit()
        for warning in warnings:
            flash(warning, "warning")
        flash("Worker added to cleaning job.", "success")
        return redirect(url_for("operations.assign_cleaning_workers", job_id=job.id))
    return render_template("cleaning_job_workers.html", job=job, employees=employees_for_select(), roles=JOB_WORKER_ROLES, statuses=ATTENDANCE_STATUSES, warnings=warnings)


@operations_bp.route("/cleaning-jobs/<int:job_id>/complete", methods=["POST"])
@role_required(*OPS_ROLES)
def complete_cleaning_job(job_id):
    job = db.get_or_404(CleaningJob, job_id)
    job.status = "Completed"
    record_audit("Cleaning job completed", job)
    db.session.commit()
    flash("Cleaning job marked completed.", "success")
    return redirect(url_for("operations.cleaning_job_detail", job_id=job.id))


@operations_bp.route("/cleaning-jobs/<int:job_id>/completion-report", methods=["GET", "POST"])
@role_required(*OPS_ROLES)
def completion_report(job_id):
    job = db.get_or_404(CleaningJob, job_id)
    report = job.completion_report or JobCompletionReport(cleaning_job_id=job.id)
    if request.method == "POST":
        report.completed_by = current_user.id
        report.completion_status = request.form.get("completion_status", "Completed Successfully")
        report.workers_present = int(request.form.get("workers_present") or 0)
        report.workers_absent = int(request.form.get("workers_absent") or 0)
        report.checklist_completed = bool(request.form.get("checklist_completed"))
        report.issues_found = request.form.get("issues_found")
        report.client_feedback = request.form.get("client_feedback")
        report.supervisor_comments = request.form.get("supervisor_comments")
        report.completed_at = datetime.now(timezone.utc)
        job.status = "Completed" if report.completion_status != "Not Completed" else "In Progress"
        db.session.add(report)
        record_audit("Completion report submitted", report)
        db.session.commit()
        flash("Completion report submitted.", "success")
        return redirect(url_for("operations.job_report_detail", report_id=report.id))
    return render_template("completion_report_form.html", job=job, report=report, statuses=COMPLETION_STATUSES)


@operations_bp.route("/job-reports")
@role_required(*OPS_ROLES)
def job_reports():
    reports = JobCompletionReport.query.order_by(JobCompletionReport.completed_at.desc()).all()
    return render_template("job_reports.html", reports=reports)


@operations_bp.route("/job-reports/<int:report_id>")
@role_required(*OPS_ROLES)
def job_report_detail(report_id):
    report = db.get_or_404(JobCompletionReport, report_id)
    return render_template("job_report_detail.html", report=report)


def payroll_attendance_context(payroll_run):
    if not payroll_run.client_company_id:
        return {}
    start, end = month_date_range(payroll_run.month, payroll_run.year)
    records = AttendanceRecord.query.filter(
        AttendanceRecord.client_company_id == payroll_run.client_company_id,
        AttendanceRecord.work_date >= start,
        AttendanceRecord.work_date < end,
    ).all()
    payroll_employee_ids = {item.employee_id for item in payroll_run.items if item.employee_id}
    assigned_employee_ids = {
        assignment.employee_id
        for assignment in WorkerAssignment.query.filter_by(
            client_company_id=payroll_run.client_company_id,
            status="Active",
        ).all()
    }
    return {
        "attendance_records": records,
        "attendance_days": len({(record.employee_id, record.work_date) for record in records}),
        "overtime_hours": sum(record.overtime_hours or 0 for record in records),
        "absentees": sum(1 for record in records if record.attendance_status == "Absent"),
        "payroll_not_assigned": PayrollItem.query.filter(PayrollItem.payroll_run_id == payroll_run.id, PayrollItem.employee_id.notin_(assigned_employee_ids or {0})).all(),
        "assigned_missing_payroll": Employee.query.filter(Employee.id.in_(assigned_employee_ids - payroll_employee_ids)).all() if assigned_employee_ids - payroll_employee_ids else [],
    }
