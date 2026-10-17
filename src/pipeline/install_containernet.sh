#!/bin/bash
set -e

echo "=== CHECKING CONTAINNERNET ==="
if python3 -c "from mininet.net import Containernet; print('containernet already installed')" 2>/dev/null; then
    echo "Containernet is ready!"
else
    echo "Containernet not found in python. Installing..."
    if [ ! -d "/home/nguyendo/containernet" ]; then
        echo "Cloning containernet..."
        git clone https://github.com/containernet/containernet.git /home/nguyendo/containernet
    fi
    cd /home/nguyendo/containernet
    echo "Running python3 setup.py install..."
    python3 setup.py install
    echo "Containernet installed successfully!"
fi

python3 -c "from mininet.net import Containernet; print('CONTAINERNET IMPORT TEST: SUCCESS')"
