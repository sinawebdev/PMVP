from datetime import date, datetime

from sqlalchemy import and_, or_

from app import db
from app.models import (
    AttendanceRecord,
    CleaningJob,
    CleaningJobWorker,
    Employee,
    WorkerAssignment,
)


ASSIGNMENT_STATUSES = ["Active", "Pending", "Completed", "Suspended", "Transferred"]
SHIFT_TYPES = ["Morning", "Afternoon", "Night", "Full Day", "Rotational"]
ATTENDANCE_STATUSES = ["Present", "Absent", "Late", "On Leave", "Sick", "Suspended", "Unknown"]
JOB_TYPES = [
    "Regular Cleaning",
    "Deep Cleaning",
    "Office Cleaning",
    "Warehouse Cleaning",
    "Post-Event Cleaning",
    "Emergency Cleaning",
    "Other",
]
JOB_STATUSES = ["Scheduled", "In Progress", "Completed", "Cancelled"]
JOB_WORKER_ROLES = ["Team Lead", "Cleaner", "Supervisor", "Support Worker"]
COMPLETION_STATUSES = [
    "Completed Successfully",
    "Completed With Issues",
    "Partially Completed",
    "Not Completed",
]


def parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_time(value):
    if not value:
        return None
    return datetime.strptime(value, "%H:%M").time()


def active_assignment_for(employee_id, on_date=None):
    on_date = on_date or date.today()
    return (
        WorkerAssignment.query.filter_by(employee_id=employee_id, status="Active")
        .filter(WorkerAssignment.assignment_start_date <= on_date)
        .filter(
            or_(
                WorkerAssignment.assignment_end_date.is_(None),
                WorkerAssignment.assignment_end_date >= on_date,
            )
        )
        .order_by(WorkerAssignment.assignment_start_date.desc())
        .first()
    )


def assignments_overlap(first, second):
    first_end = first.assignment_end_date or date.max
    second_end = second.assignment_end_date or date.max
    return first.assignment_start_date <= second_end and second.assignment_start_date <= first_end


def assignment_warnings(assignment, allow_duplicate_active=False):
    warnings = []
    employee = db.session.get(Employee, assignment.employee_id)
    client = assignment.client_company

    if employee and employee.status in {"Inactive", "Terminated"}:
        warnings.append(f"Employee status is {employee.status}.")
    if client and client.status != "Active":
        warnings.append("Selected client company is inactive.")
    if not assignment.assignment_end_date:
        warnings.append("Assignment has no end date.")

    existing = WorkerAssignment.query.filter(
        WorkerAssignment.employee_id == assignment.employee_id,
        WorkerAssignment.id != (assignment.id or 0),
        WorkerAssignment.status.in_(["Active", "Pending"]),
    ).all()
    for other in existing:
        if assignment.status == "Active" and other.status == "Active" and not allow_duplicate_active:
            warnings.append("Worker already has an active assignment.")
            break
        if assignments_overlap(assignment, other):
            warnings.append("Assignment dates overlap another assignment.")
            break
    return warnings


def availability_for(employee):
    status = str(employee.status or "").strip()
    if status in {"Inactive", "Suspended", "Terminated"}:
        return status, None
    if status == "On Leave":
        return "On Leave", None
    assignment = active_assignment_for(employee.id)
    if assignment:
        return "Assigned", assignment
    return "Available", None


def availability_groups():
    groups = {
        "Available": [],
        "Assigned": [],
        "On Leave": [],
        "Inactive": [],
        "Suspended": [],
        "Terminated": [],
    }
    for employee in Employee.query.order_by(Employee.full_name).all():
        status, assignment = availability_for(employee)
        groups.setdefault(status, []).append((employee, assignment))
    return groups


def apply_attendance_calculations(record):
    warnings = []
    if record.attendance_status == "Absent":
        record.hours_worked = 0
        record.overtime_hours = 0
        return warnings

    if record.attendance_status in {"Present", "Late"} and not record.clock_in:
        warnings.append("Missing clock in for Present worker.")
    if record.attendance_status in {"Present", "Late"} and not record.clock_out:
        warnings.append("Missing clock out for Present worker.")

    if record.clock_in and record.clock_out:
        start = datetime.combine(record.work_date or date.today(), record.clock_in)
        end = datetime.combine(record.work_date or date.today(), record.clock_out)
        if end < start:
            warnings.append("Clock out is earlier than clock in.")
            record.hours_worked = None
            record.overtime_hours = 0
        else:
            hours = round((end - start).total_seconds() / 3600, 2)
            record.hours_worked = hours
            record.overtime_hours = round(max(hours - 8, 0), 2)
            if hours > 16:
                warnings.append("Very high hours worked.")
    else:
        record.hours_worked = None
        record.overtime_hours = 0
    return warnings


def attendance_warnings(record):
    warnings = apply_attendance_calculations(record)
    employee = db.session.get(Employee, record.employee_id)
    if employee and employee.status in {"Inactive", "Terminated"}:
        warnings.append(f"Attendance entered for {employee.status.lower()} worker.")
    assignment = active_assignment_for(record.employee_id, record.work_date)
    if record.attendance_status in {"Present", "Late"} and not assignment:
        warnings.append("Worker marked Present but has no active assignment.")
    if assignment and assignment.client_company_id != record.client_company_id:
        warnings.append("Worker is assigned to a different client company.")
    duplicate = AttendanceRecord.query.filter(
        AttendanceRecord.employee_id == record.employee_id,
        AttendanceRecord.work_date == record.work_date,
        AttendanceRecord.id != (record.id or 0),
    ).first()
    if duplicate:
        warnings.append("Duplicate attendance for same worker and date.")
    return warnings


def cleaning_worker_warnings(cleaning_job, employee):
    warnings = []
    if employee.status in {"Inactive", "Terminated"}:
        warnings.append(f"Worker status is {employee.status}.")
    active_assignment = active_assignment_for(employee.id, cleaning_job.scheduled_date)
    if active_assignment and active_assignment.client_company_id != cleaning_job.client_company_id:
        warnings.append("Worker belongs to another active client assignment.")
    overlapping_job = (
        CleaningJobWorker.query.join(CleaningJob)
        .filter(
            CleaningJobWorker.employee_id == employee.id,
            CleaningJob.id != (cleaning_job.id or 0),
            CleaningJob.scheduled_date == cleaning_job.scheduled_date,
            CleaningJob.status.in_(["Scheduled", "In Progress"]),
        )
        .first()
    )
    if overlapping_job:
        warnings.append("Worker is already assigned to another cleaning job at this time.")
    return warnings


def month_date_range(month_name, year):
    month_number = datetime.strptime(month_name, "%B").month
    start = date(int(year), month_number, 1)
    end = date(int(year) + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1)
    return start, end
