"""SMS delivery settings, and the boot guards that keep them honest.

Called once from ``create_app``. Kept out of ``app/__init__.py`` because that
file is long enough already, and kept free of app imports so it can run before
anything else is wired.

Every guard fails closed, the same way the SECRET_KEY and PAYSLIP_TOKEN_KEY
guards do: a deployment that would send broken or unauthorised SMS refuses to
boot, loudly, instead of sending them. ``render.yaml`` is inert (the live
service is configured in the Render dashboard), so each variable these guards
check must be set in the dashboard *before* the deploy that adds the guard, or
the service crash-loops the way it did over PAYSLIP_TOKEN_KEY.
"""
import os


def _flag(name, default):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def load_sms_config(app, *, is_production, is_desktop):
    cfg = app.config
    cfg["SASUSYNC_API_KEY"] = os.getenv("SASUSYNC_API_KEY")
    cfg["SASUSYNC_BASE_URL"] = os.getenv("SASUSYNC_BASE_URL")
    # Sandbox unless told otherwise: it takes the identical request, delivers
    # nothing and charges nothing. Going live is a deliberate SASUSYNC_SANDBOX=false.
    cfg["SASUSYNC_SANDBOX"] = _flag("SASUSYNC_SANDBOX", "true")
    cfg["SASUSYNC_WEBHOOK_SECRET"] = os.getenv("SASUSYNC_WEBHOOK_SECRET")
    # Lifetime of the short link an SMS carries. The SMS text quotes this
    # number, so the wording and the real expiry cannot drift apart.
    cfg["PAYSLIP_SMS_LINK_DAYS"] = int(os.getenv("PAYSLIP_SMS_LINK_DAYS", "30"))
    # Requests per minute per IP on /s/<code>.
    cfg["PAYSLIP_LINK_RATE_PER_MIN"] = int(os.getenv("PAYSLIP_LINK_RATE_PER_MIN", "30"))
    check_sms_config(cfg, is_production=is_production, is_desktop=is_desktop)


def check_sms_config(cfg, *, is_production, is_desktop):
    """Raise RuntimeError if this SMS configuration must not boot."""
    backend = cfg.get("SMS_BACKEND") or "console"
    if backend == "console":
        return

    # Desktop never sends SMS (decision 11): the provider account and sender ID
    # are Payrolla's, not the firm's running the install.
    if is_desktop:
        raise RuntimeError(
            f"SMS_BACKEND={backend} is not allowed on a desktop install — SMS is "
            "never sent from the desktop app. Unset SMS_BACKEND or set it to console."
        )

    # The worker thread builds messages outside any request, so without a
    # configured host the payslip link silently drops out of every SMS.
    base_url = (cfg.get("PUBLIC_BASE_URL") or "").strip()
    if not base_url:
        raise RuntimeError(
            f"PUBLIC_BASE_URL must be set when SMS_BACKEND={backend} — the "
            "distribution worker runs outside a request, so without it every "
            "SMS would go out with no payslip link."
        )
    if is_production and not base_url.lower().startswith("https://"):
        raise RuntimeError(
            "PUBLIC_BASE_URL must start with https:// in production — it is the "
            "payslip link sent to every worker."
        )

    if is_production and backend == "sasusync":
        missing = [
            name
            for name in ("SASUSYNC_API_KEY", "SASUSYNC_BASE_URL", "SMS_SENDER_ID")
            if not cfg.get(name)
        ]
        if missing:
            raise RuntimeError(
                f"SMS_BACKEND=sasusync needs {', '.join(missing)} in production — "
                "refusing to start rather than fail every SMS at send time."
            )
