#!/bin/bash
set -e
echo "=== DOCKER IMAGE INSPECTION ==="
docker image inspect --format='{{.Id}}' uav-cas-sitl:latest

echo "=== CHECKING TOOLS INSIDE IMAGE ==="
docker run --rm uav-cas-sitl:latest bash -c '
    echo "make:" $(make --version | head -n 1)
    echo "tcpdump:" $(tcpdump --version | head -n 1)
    echo "hping3:" $(hping3 -v 2>&1 | head -n 1)
    echo "mavlink-routerd:" $(mavlink-routerd --version 2>&1 | head -n 1)
    test -f /PX4-Autopilot/Makefile && echo "px4: OK"
    python3 -c "import mavsdk; print(\"mavsdk: OK\")"
'
echo "=== ALL CHECKS PASSED ==="
