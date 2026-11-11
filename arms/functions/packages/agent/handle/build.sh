#!/bin/bash
# Remote build hook (DO Functions Python): install deps into ./virtualenv
set -e
PYV=$(python3 -c 'import sys; print(f"python{sys.version_info[0]}.{sys.version_info[1]}")')
virtualenv --without-pip virtualenv
pip install -r requirements.txt --target "virtualenv/lib/$PYV/site-packages"
