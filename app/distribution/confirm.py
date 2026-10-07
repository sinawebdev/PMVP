"""What a send would do, shown before it is queued (SMS Phase 3).

An SMS send costs credits and lands on people's phones, so before an SMS or
`auto` send is queued, the operator (or the company's admin) sees who it will
reach and how: to a phone, to a MoMo number, by email or WhatsApp (auto only),
and who it cannot reach at all. Everything is computed with the functions the
send path itself uses (resolve_channel, sms_contact, the same skip rules), so
the preview and the send cannot disagree about a worker.

The confirm step is a redirect, not a modal: a send form posts as before, and a
post on a confirmed channel without ``confirmed=1`` is sent to the confirm view
instead of being queued. Its button posts back with ``confirmed=1`` and the same
nonce, so the existing idempotency still collapses a double-click into one batch.

Bulk Distribute on the runs list is an `auto` send too, so it stops at the same
step: the same counts summed over the selected runs, with each run's share and
the runs it would not queue. A run has at most one unfinished batch, so a
double press there cannot queue a run twice either.
"""
import uuid

from flask import url_for
from sqlalchemy.orm import joinedload

from app.models import (
    CHANNEL_AUTO,
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    DELIVERY_FAILED,
    PayrollRun,
    PayslipDelivery,
)
from app.paging import paginate_list
from app.permissions import can_distribute_run

from .contacts import SOURCE_INVALID, SOURCE_ROSTER_PHONE, sms_contact
from .queue import _in_flight_batch
from .service import SKIPPED_BY_SEND, _contact_for, resolve_channel

# SMS costs a credit per message, and `auto` means SMS for most of this
# workforce, so both are confirmed. Email and WhatsApp queue as before.
CONFIRMED_CHANNELS = (CHANNEL_SMS, CHANNEL_AUTO)
CONFIRM_ACTIONS = ("send", "resend", "schedule")
PROBLEMS_PER_PAGE = 25

REASONS = {
    "no_number": "No phone or MoMo number on record",
    "no_contact": "No phone, MoMo number or email on record",
    "invalid": "Number on record isn't a valid Ghana mobile number",
}


def needs_confirm(channel, form):
    """True when a send on ``channel`` must go through the confirm step first."""
    return channel in CONFIRMED_CHANNELS and form.get("confirmed") != "1"


def confirm_url(endpoint, run, channel, action, form):
    """Where an unconfirmed send is redirected, carrying what it was asked to do."""
    params = {
        "run_id": run.id, "channel": channel, "action": action,
        "nonce": form.get("nonce") or uuid.uuid4().hex,
    }
    if form.get("scheduled_for"):
        params["scheduled_for"] = form.get("scheduled_for")
    return url_for(endpoint, **params)


def send_preview(run, channel, *, only_failed=False):
    """Counts of where each payslip in ``run`` would go, and who it cannot reach.

    Payslips the send would skip (already sent, sending, or unconfirmed, or for a
    resend, anything not failed) are counted as ``skipped`` and nothing else."""
    existing = {
        (d.payroll_item_id, d.channel): d
        for d in PayslipDelivery.query.filter_by(payroll_run_id=run.id)
    }
    counts = dict.fromkeys(
        ("to_phone", "to_momo", "by_email", "by_whatsapp", "no_contact", "invalid", "skipped"),
        0,
    )
    problems = []
    for item in list(run.items):
        ch = resolve_channel(item) if channel == CHANNEL_AUTO else channel
        delivery = existing.get((item.id, ch))
        if only_failed:
            skip = delivery is None or delivery.status != DELIVERY_FAILED
        else:
            skip = delivery is not None and delivery.status in SKIPPED_BY_SEND
        if skip:
            counts["skipped"] += 1
            continue
        if ch == CHANNEL_SMS:
            number, source = sms_contact(item)
            if number:
                counts["to_phone" if source == SOURCE_ROSTER_PHONE else "to_momo"] += 1
                continue
            if source == SOURCE_INVALID:
                reason = "invalid"
            else:
                # On an auto send, SMS is only the fallback once nothing else
                # reaches the worker either.
                reason = "no_contact" if channel == CHANNEL_AUTO else "no_number"
        elif _contact_for(ch, item):
            counts["by_email" if ch == CHANNEL_EMAIL else "by_whatsapp"] += 1
            continue
        else:
            reason = "no_contact"
        counts["invalid" if reason == "invalid" else "no_contact"] += 1
        problems.append({"item": item, "reason": REASONS[reason]})

    sms = counts["to_phone"] + counts["to_momo"]
    counts.update(
        sms_recipients=sms,
        # Each SMS is built to fit one part, so one credit each. SasuSync's
        # real charge is recorded per message as `units`.
        credits=sms,
        reachable=sms + counts["by_email"] + counts["by_whatsapp"],
        problems=problems,
    )
    return counts


def confirm_context(run, channel, action, args, *, post_url, back_url):
    """Everything ``macros/distribution.html::send_confirm`` renders."""
    preview = send_preview(run, channel, only_failed=action == "resend")
    scheduled_for = args.get("scheduled_for") or ""
    reachable = preview["reachable"]
    workers = f"{reachable} worker{'' if reachable == 1 else 's'}"
    button = {
        "send": f"Send to {workers}",
        "resend": f"Resend to {workers}",
        "schedule": f"Schedule for {scheduled_for.replace('T', ' ')} GMT",
    }[action]
    hidden = {"nonce": args.get("nonce") or uuid.uuid4().hex, "channel": channel,
              "confirmed": "1"}
    if scheduled_for:
        hidden["scheduled_for"] = scheduled_for
    page_params = {"channel": channel, "action": action, "nonce": hidden["nonce"]}
    if scheduled_for:
        page_params["scheduled_for"] = scheduled_for
    return {
        **preview,
        "channel": channel,
        "action": action,
        "auto": channel == CHANNEL_AUTO,
        "problems": paginate_list(preview["problems"], per_page=PROBLEMS_PER_PAGE),
        "button": button,
        "hidden": hidden,
        "post_url": post_url,
        "back_url": back_url,
        "page_params": page_params,
    }


# --- Bulk Distribute (runs list) ------------------------------------------------

_SUMMED = ("to_phone", "to_momo", "by_email", "by_whatsapp", "no_contact", "invalid",
           "skipped", "sms_recipients", "credits", "reachable")


def bulk_confirm_url(form):
    """Where an unconfirmed Bulk Distribute is redirected, carrying the selection
    and the runs list's filters."""
    return url_for(
        "distribution.bulk_confirm", run_ids=form.getlist("run_ids"),
        status=form.get("status") or None, client_id=form.get("client_id") or None,
    )


def _selected_ids(raw_ids):
    """Unique integer ids in the order they were selected; junk is dropped."""
    ids = []
    for raw in raw_ids:
        try:
            run_id = int(raw)
        except (TypeError, ValueError):
            continue
        if run_id not in ids:
            ids.append(run_id)
    return ids


def bulk_confirm_context(args, role, *, post_url, back_url):
    """``confirm_context`` for several runs at once, or None when none is selected.

    A run is left out, and named with the reason, when Bulk Distribute would not
    queue a new batch for it: missing, not sendable for ``role``, or already
    holding an unfinished batch. Only the runs it would queue are posted back,
    so what is sent is exactly what this showed."""
    ids = _selected_ids(args.getlist("run_ids"))
    if not ids:
        return None
    found = {
        run.id: run
        for run in PayrollRun.query.options(joinedload(PayrollRun.client_company))
        .filter(PayrollRun.id.in_(ids))
    }
    runs, included, problems = [], [], []
    for run_id in ids:
        run = found.get(run_id)
        if run is None:
            runs.append({"label": f"Run #{run_id}", "reason": "Not found"})
            continue
        label = f"{run.month} {run.year}"
        if run.client_company:
            label += f" ({run.client_company.name})"
        if not can_distribute_run(role, run):
            runs.append({"label": label,
                         "reason": f"Not eligible to distribute in its current state ({run.status})"})
            continue
        if _in_flight_batch(run.id) is not None:
            runs.append({"label": label,
                         "reason": "A send is already scheduled, queued or running"})
            continue
        preview = send_preview(run, CHANNEL_AUTO)
        included.append(run.id)
        runs.append({"label": label, "preview": preview})
        problems.extend(dict(p, run=label) for p in preview["problems"])

    counts = {key: sum(r["preview"][key] for r in runs if "preview" in r) for key in _SUMMED}
    workers = f"{counts['reachable']} worker{'' if counts['reachable'] == 1 else 's'}"
    in_runs = f"{len(included)} run{'' if len(included) == 1 else 's'}"
    filters = {"status": args.get("status") or "", "client_id": args.get("client_id") or ""}
    return {
        **counts,
        "channel": CHANNEL_AUTO,
        "action": "send",
        "auto": True,
        "runs": runs,
        "problems": paginate_list(problems, per_page=PROBLEMS_PER_PAGE),
        "button": f"Send to {workers} in {in_runs}",
        "hidden": {"confirmed": "1", "run_ids": included, **filters},
        "post_url": post_url,
        "back_url": back_url,
        "page_params": {"run_ids": ids, **filters},
    }
