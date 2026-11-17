"""Optional SMTP delivery. Keep ambiguous sends for manual reconciliation."""
from email.message import EmailMessage
from hashlib import sha256
import os
import smtplib
import ssl


def deliver(db, row):
    recipient = os.getenv("AUTOSEARCH_MAIL_TO")
    if not recipient:
        return
    digest = sha256((recipient + row["body"]).encode()).hexdigest()
    ident = f"{row['cycle']}-{digest}"
    existing = db.execute("select state from mail where id=?", (ident,)).fetchone()
    if existing and existing[0] in ("sent", "uncertain"):
        return
    message = EmailMessage()
    message["From"] = os.environ["AUTOSEARCH_MAIL_FROM"]
    message["To"] = recipient
    message["Subject"] = f"[AutoSearch · 周期 {row['cycle']}] {row['label']}"
    message["Message-ID"] = f"<{ident}@autosearch.local>"
    message.set_content(row["body"])
    with smtplib.SMTP_SSL(
        os.environ["AUTOSEARCH_SMTP_HOST"],
        int(os.getenv("AUTOSEARCH_SMTP_PORT", "465")),
        timeout=20,
        context=ssl.create_default_context(),
    ) as smtp:
        if os.getenv("AUTOSEARCH_SMTP_USER"):
            smtp.login(os.environ["AUTOSEARCH_SMTP_USER"], os.environ["AUTOSEARCH_SMTP_PASSWORD"])
        with db:
            db.execute("insert or replace into mail values(?,?,'uncertain')", (ident, row["label"]))
        smtp.send_message(message)
        with db:
            db.execute("update mail set state='sent' where id=?", (ident,))
