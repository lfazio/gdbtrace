Un breakpoint se pose d'un double-clic dans la marge de la vue Source ou Désassemblage, ou dans la vue Breakpoints. Il peut s'arrêter sous condition, au Nᵉ passage, ou exécuter une action sans arrêter la cible.

## Poser un breakpoint

- **Marge de la vue Source** : breakpoint sur la ligne C.
- **Marge du Désassemblage** : sur une ligne C, breakpoint source ; sur une instruction, breakpoint à l'adresse.
- **Vue Breakpoints** : un emplacement comme dans gdb (`update_sensor`, `main.c:59`, `*0x080001f4`), puis **Ajouter**. Cocher **Matériel** pour un comparateur du FPB, indispensable en flash quand le serveur ne sait pas y écrire. **Watchpoint** surveille une variable en écriture.

Breakpoints matériels et watchpoints sont en nombre limité : typiquement 6 et 4 sur un Cortex-M3/M4, 8 et 4 sur un M7, 4 et 2 sur un M0+.

## Vue Breakpoints

Chaque ligne donne le numéro, le type, l'adresse, le nombre de passages (Hits), l'emplacement et une colonne **Condition / action**, par exemple `si tick > 2 · au 5ᵉ passage (reste 3) · « n={counter} » → continue`.

- **On/Off** active ou désactive sans supprimer ; **Supprimer** retire.
- Double-clic : la source s'ouvre sur le breakpoint.
- **Options…** ou clic droit › Options… ouvre le dialogue d'options.

## Options d'un breakpoint ou d'un watchpoint

| Option | Effet |
| --- | --- |
| Condition C | arrêt seulement si l'expression est vraie (vide : toujours) |
| Arrêt au passage n° | gdb compte les passages sans arrêter la cible, s'arrête à celui-ci, puis à chaque passage ; le compte part du moment où l'option est posée |
| Message | à chaque passage, le texte s'écrit dans la console, puis la cible repart |
| S'arrêter après le message | la cible reste arrêtée après le message |

Dans le message, `{expression}` est remplacé par la valeur de l'expression C, et `{expression:hex}` par la même valeur dans un format (`hex`, `dec`, `oct`, `bin`, `ascii`). Exemple : `capteur n={counter} tick={tick:hex}` donne `capteur n=7 tick=0x7`.

Un passage avec message est un arrêt interne : les vues ne se rafraîchissent pas et la bannière « Breakpoint N » de gdb est masquée. Chaque passage coûte un aller-retour avec la cible, de l'ordre de la milliseconde : sur une fonction appelée des milliers de fois par seconde, la cible ralentit d'autant. Un script peut aussi poser une action Python qui décide de l'arrêt ([Console et scripts Python](Console-et-scripts) et [Scripting Python](Scripting)).

## Go.Till

**Run › Go.Till (jusqu'à une condition)…** lance la cible jusqu'à ce qu'une condition C devienne vraie, par exemple `counter >= 20`.

- La cible tourne à pleine vitesse : GdbTrace pose un watchpoint matériel sur les variables de l'expression, avec l'expression pour condition.
- Le watchpoint est retiré à la reprise suivante et n'apparaît pas dans la vue Breakpoints.
- Un registre ou une valeur sans adresse ne peut pas être suivi ainsi : GdbTrace le refuse et renvoie vers Step.Till.

## Step.Till et Step.Change

**Run › Step.Till / Step.Change…** avance pas à pas jusqu'à ce qu'une expression soit vraie (Step.Till) ou que sa valeur change (Step.Change).

- Les pas sont des lignes source, ou des instructions si **Pas assembleur** est coché.
- Toute expression convient, registres compris. Un nombre de pas maximum borne la boucle (1000 par défaut).
- La boucle compte comme une seule exécution : les vues ne se rafraîchissent qu'à la fin, et **Break** l'interrompt.
- Un breakpoint ou un signal l'arrête aussi.
- La barre d'état donne le bilan, par exemple `Step.Change last_char, 39 pas, 71 'G' → 72 'H'`.
