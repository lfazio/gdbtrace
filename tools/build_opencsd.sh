#!/bin/sh
# Construit OpenCSD (décodeur de trace de référence ARM/Linaro) dans .cache/opencsd.
# Sert d'oracle aux tests ETM (tests/test_etm.py) ; inutile pour utiliser gdbtrace.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST="$ROOT/.cache/opencsd"
VERSION=v1.4.1
if [ ! -d "$DEST/decoder" ]; then
    mkdir -p "$ROOT/.cache"
    git clone --depth 1 --branch "$VERSION" https://github.com/Linaro/OpenCSD "$DEST"
fi
make -C "$DEST/decoder/build/linux" -j"$(nproc)"
echo "trc_pkt_lister : $DEST/decoder/tests/bin/builddir/trc_pkt_lister"
