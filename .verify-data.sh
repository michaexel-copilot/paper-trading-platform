#!/usr/bin/env bash
set -euo pipefail
B=http://127.0.0.1:8000
cd /root
rm -f jar

echo "=== register"
curl -fsS -c jar -X POST "$B/api/auth/register" -H 'Content-Type: application/json' \
  -d '{"email":"verify@example.com","password":"longenoughpassword"}'
echo

echo "=== create portfolio"
curl -fsS -b jar -X POST "$B/api/portfolios" -H 'Content-Type: application/json' \
  -d '{"name":"Verify","base_currency":"EUR","starting_cash":10000}'
echo

echo "=== find BTC asset id"
BTC_ID=$(curl -fsS -b jar "$B/api/assets" | python3 -c '
import json,sys
assets=json.load(sys.stdin)
btc=[a for a in assets if a.get("symbol","").startswith("BTC")]
print(btc[0]["id"] if btc else "")')
echo "BTC_ID=$BTC_ID"
[ -n "$BTC_ID" ] || { echo "no BTC asset found"; exit 1; }

echo "=== preview market buy"
curl -fsS -b jar -X POST "$B/api/portfolios/1/orders/preview" -H 'Content-Type: application/json' \
  -d "{\"asset_id\":$BTC_ID,\"side\":\"buy\",\"type\":\"market\",\"quantity\":0.001}"
echo

echo "=== place market buy"
curl -fsS -b jar -X POST "$B/api/portfolios/1/orders" -H 'Content-Type: application/json' \
  -d "{\"asset_id\":$BTC_ID,\"side\":\"buy\",\"type\":\"market\",\"quantity\":0.001,\"client_order_id\":\"verify-$(date +%s)\"}"
echo

echo "=== trades"
curl -fsS -b jar "$B/api/portfolios/1/trades"
echo
