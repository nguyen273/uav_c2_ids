#!/bin/bash
set -e
echo "=== USER AND HOME ==="
whoami
pwd
ls -la /home/nguyendo

echo "=== DOCKER IMAGES ==="
docker images

echo "=== PYTHON PACKAGES ==="
python3 -c "
for mod in ['docker', 'mininet', 'scapy', 'mavsdk', 'numpy', 'pandas', 'scipy']:
    try:
        m = __import__(mod)
        print(f'{mod}: OK ({getattr(m, \"__file__\", \"builtin\")})')
    except Exception as e:
        print(f'{mod}: MISSING ({e})')
"

echo "=== CHECK CONTAINERS / MININET ==="
mn --version || true
which mn || true
which containernet || true
