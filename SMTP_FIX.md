# SMTP fix — 2026-09-27

The outbound path was not a dead server. Three things were stacked:

1. Code defaulted `FROM_EMAIL` to `hello@garcar.io` while auth was `gwc2780@gmail.com`. Gmail SMTP rejects or silently fails SPF when From != authenticated user.
2. Repos expect `SMTP_PASS` to be a Gmail **App Password**, not the Google account password. Account passwords fail with `535 5.7.8` / `Username and Password not accepted`.
3. Friday 2026-09-25 blast hit dead domains and blocked inboxes. That is address hygiene, not SMTP.

## Working config (Gmail)

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=gwc2780@gmail.com
SMTP_PASS=<16-char App Password, no spaces>
FROM_EMAIL=gwc2780@gmail.com
FROM_NAME=Garrett Carroll
```

Do not set `FROM_EMAIL=hello@garcar.io` until that domain has SPF + DKIM + a mailbox that authenticates.

Create the App Password: Google Account → Security → 2-Step Verification → App passwords → Mail.
Paste it only in the host secret store (Railway / Render / Vercel). Never in chat or the repo.

## Preflight

```bash
python3 smtp_send.py --preflight
```

Expect: `STARTTLS ok`, `login ok`, `from aligned`.

## Bypass that already works

Gmail API (this session) sent the 27 Sep five without SMTP.
Use Gmail API or Resend for outreach. Keep SMTP only for fulfillment receipts after a paid $47.

## Address hygiene (do not send again)

| Address | Result |
|---|---|
| contact@allianceroofingdfw.com | NXDOMAIN |
| contact@tarrantroofing.com | blocked |
| atlas+cleburne-comfort@garcar.io | internal alias, not a shop |
| info@priorityroofs.com | 550 mailbox missing |
| office@reformed-roofing.com | 550 mailbox missing |
| marcus@lonestarroofingco.com | recipient server reject |

Live replacements used 27 Sep: `pete@allianceroofingus.com`, `info@tarrantroofing.com`, `hirecomfortpros@gmail.com`.
