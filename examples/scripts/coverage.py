"""Campagne de couverture de code (COVerage), sans instrumentation, par la trace ETM.

    python -m gdbtrace examples/firmware/demo.elf --etm-sim --batch \\
        --script examples/scripts/coverage.py --coverage cov.html --coverage cov.info

Ajouter `--coverage-merge run1.json` pour cumuler avec une campagne précédente
(enregistrée avec `--coverage run1.json`).
"""
import time

gdbtrace.break_("animate", temporary=True)     # un premier passage complet dans la boucle
gdbtrace.go()
gdbtrace.wait()
gdbtrace.go()                                   # puis la cible tourne un moment
time.sleep(0.5)
gdbtrace.halt()
gdbtrace.cov.summary()
gdbtrace.cov.list(8)                            # les fonctions les moins couvertes
