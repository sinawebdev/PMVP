"""Demo workspace payslips: prepared and recorded, never handed to a provider.

The staff-only demo workspace (app.demo_workspace) is fake companies on the live
site, where SMS and email have real providers behind them. Its rosters carry
made-up numbers, and Ghana has no reserved range, so a made-up number may well
be somebody's phone. The guarantee therefore sits at the send, not in the data:
:func:`app.distribution.service._attempt_send`, the one place a payslip reaches
a provider, swaps in :class:`DemoSender` for any demo company's run, whatever
sender it was handed. The delivery is recorded `sent` so the demo shows the real
screens end to end; nothing leaves Payrolla.
"""
from flask import current_app

from .channels import Sender, SendResult, recipient_fingerprint


def is_demo_company(company):
    """True for a demo company, or any company of a demo bureau."""
    if company is None:
        return False
    if getattr(company, "is_demo", False):
        return True
    bureau = getattr(company, "bureau", None)
    return bool(bureau is not None and getattr(bureau, "is_demo", False))


class DemoSender(Sender):
    """Logs the attempt and reports success. No network, no credits."""

    def __init__(self, channel):
        self.channel = channel
        self.provider = f"demo-{channel}"

    def send(self, message):
        current_app.logger.info(
            "[demo] %s payslip for item %s to %s recorded, not sent",
            self.channel, message.item_id, recipient_fingerprint(message.recipient),
        )
        return SendResult(ok=True, provider=self.provider)
