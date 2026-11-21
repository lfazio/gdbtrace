"""Campagne de couverture de code (COVerage), sans instrumentation, par la trace ETM.

    python -m gdbtrace examples/firmware/demo.elf --etm-sim --batch \\
        --script examples/scripts/coverage.py --coverage cov.html --coverage cov.info

Ajouter `--coverage-merge run1.json` pour cumuler avec une campagne précédente
(enregistrée avec `--coverage run1.json`).
"""
import time

t32.break_("animate", temporary=True)     # un premier passage complet dans la boucle
t32.go()
t32.wait()
t32.go()                                   # puis la cible tourne un moment
time.sleep(0.5)
t32.halt()
t32.cov.summary()
t32.cov.list(8)                            # les fonctions les moins couvertes
