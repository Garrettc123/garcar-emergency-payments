#!/usr/bin/env python3
"""Gmail SMTP sender with From-alignment guard.

Fix applied 2026-09-27:
- FROM_EMAIL must equal SMTP_USER for Gmail.
- Port 587 + STARTTLS. Port 465 uses SMTP_SSL instead.
- Refuse to send if SMTP_PASS looks like a normal password length without app-password shape.
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _cfg() -> dict:
    user = os.environ.get("SMTP_USER", "").strip()
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    password = os.environ.get("SMTP_PASS", "").strip().replace(" ", "")
    from_email = os.environ.get("FROM_EMAIL", user).strip() or user
    from_name = os.environ.get("FROM_NAME", "Garrett Carroll").strip()
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_email": from_email,
        "from_name": from_name,
    }


def validate(cfg: dict) -> list[str]:
    errors = []
    if not cfg["user"]:
        errors.append("SMTP_USER missing")
    if not cfg["password"]:
        errors.append("SMTP_PASS missing — use a Gmail App Password")
    if cfg["from_email"] and cfg["user"] and cfg["from_email"].lower() != cfg["user"].lower():
        if cfg["host"].endswith("gmail.com"):
            errors.append(
                f"FROM_EMAIL {cfg['from_email']} != SMTP_USER {cfg['user']}. "
                "Gmail will fail or land in spam. Set FROM_EMAIL to the Gmail address."
            )
    if cfg["host"].endswith("gmail.com") and cfg["password"] and len(cfg["password"]) != 16:
        errors.append(
            "Gmail SMTP_PASS should be a 16-character App Password. "
            "Account passwords are rejected."
        )
    return errors


def connect(cfg: dict):
    context = ssl.create_default_context()
    if cfg["port"] == 465:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context, timeout=20)
        server.ehlo()
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    server.login(cfg["user"], cfg["password"])
    return server


def send(to_email: str, subject: str, body_text: str, body_html: str | None = None) -> dict:
    cfg = _cfg()
    errors = validate(cfg)
    if errors:
        return {"status": "blocked", "errors": errors}
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{cfg['from_name']} <{cfg['from_email']}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))
    try:
        with connect(cfg) as server:
            server.sendmail(cfg["from_email"], [to_email], msg.as_string())
        return {"status": "sent", "to": to_email, "from": cfg["from_email"]}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def preflight() -> int:
    cfg = _cfg()
    errors = validate(cfg)
    if errors:
        print("PREFLIGHT FAIL")
        for e in errors:
            print(" -", e)
        return 1
    try:
        with connect(cfg) as server:
            print("STARTTLS ok" if cfg["port"] != 465 else "SSL ok")
            print("login ok")
            print("from aligned", cfg["from_email"])
            server.quit()
        return 0
    except Exception as exc:
        print("PREFLIGHT FAIL", exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--to")
    parser.add_argument("--subject", default="Garcar SMTP test")
    parser.add_argument("--body", default="SMTP preflight body from smtp_send.py")
    args = parser.parse_args()
    if args.preflight:
        return preflight()
    if not args.to:
        print("pass --preflight or --to")
        return 2
    result = send(args.to, args.subject, args.body)
    print(result)
    return 0 if result.get("status") == "sent" else 1


if __name__ == "__main__":
    sys.exit(main())
