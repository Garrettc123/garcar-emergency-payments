#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

echo "=== Garcar Emergency Payments: preflight ==="
python3 - <<'PY'
import main
required = ('init_db', 'verify_stripe_signature', 'extract_payment', 'record_event', 'notify_fulfillment', 'main')
missing = [name for name in required if not callable(getattr(main, name, None))]
if missing:
    raise SystemExit('Missing payment functions: ' + ', '.join(missing))
print('Payment module import and entry points OK')
PY

python3 -m unittest discover -s tests -v

echo "Preflight passed. No service was deployed and no payment was processed."
echo "Production still requires persistent storage, a Stripe signing secret, and verified fulfillment delivery."
echo "See ACTIVATE.md for deployment steps."
