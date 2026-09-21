#!/usr/bin/env bash
# One-command local test + package for emergency payments
set -euo pipefail

echo "=== Garcar Emergency Payments ==="
echo "Building local test..."

python3 -c "
import main
print('Import OK')
print('Offers:', list(main.OFFERS.keys()))
"

echo ""
echo "To run locally:"
echo "  PORT=8080 python3 main.py"
echo ""
echo "Then in another terminal:"
echo "  curl http://localhost:8080/health"
echo "  stripe listen --forward-to localhost:8080/stripe-webhook"
echo ""
echo "Deploy package is ready in this folder."
echo "See ACTIVATE.md for Railway / Render / Docker steps."
echo "See outreach_batch_1.txt for the demand side."
