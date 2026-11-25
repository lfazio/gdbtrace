#!/bin/sh
# Vérification de release : toute la suite de tests, tests sur cartes compris.
#
#   tools/release_check.sh [options de pytest…]
#
# Les cartes de test doivent être branchées (Nucleo-F746ZG : tests/test_board_f746.py ;
# Nucleo-N657X0-Q : tests/test_board_n6.py ; Nucleo-G070RB et F072RB : tests/test_board_m0.py) :
# --boards=require fait échouer la release si l'une manque, au lieu de l'ignorer. L'oracle
# OpenCSD est construit s'il manque (sinon les tests ETM qui s'y comparent seraient ignorés).
# Les relevés des vues des tests sur cartes restent dans tests/artifacts/.
set -eu
cd "$(dirname "$0")/.."
[ -x .cache/opencsd/decoder/tests/bin/builddir/trc_pkt_lister ] || tools/build_opencsd.sh
exec xvfb-run -a -s "-screen 0 1400x1000x24" .venv/bin/python -m pytest -q -rs --boards=require "$@"
