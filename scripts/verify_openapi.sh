#!/usr/bin/env bash
# Verifica se a API em produção expõe os endpoints de sugestão de endereço do mobile.
set -euo pipefail

API_URL="${1:-https://track-saidas-api.onrender.com/api/openapi.json}"

echo "Consultando: $API_URL"
spec="$(curl -sf "$API_URL")"

check_path() {
  if echo "$spec" | grep -q "$1"; then
    echo "OK  $1"
  else
    echo "FALTA  $1"
    return 1
  fi
}

failed=0
check_path "/api/mobile/enderecos/sugestoes" || failed=1
check_path "/api/mobile/enderecos/place-details" || failed=1

mobile_count="$(echo "$spec" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len([p for p in d.get('paths',{}) if '/mobile/' in p]))" 2>/dev/null || echo "?")"
echo "Rotas /mobile/: $mobile_count (esperado >= 25 após PR #19)"

if [ "$failed" -ne 0 ]; then
  echo ""
  echo "Backend desatualizado. No Render: Manual Deploy → Deploy latest commit (branch main)."
  exit 1
fi

echo "Deploy OK — endpoints de sugestão presentes."
