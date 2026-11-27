La fenêtre principale range 24 vues en quatre zones à onglets, sous un menu, une barre d'outils et au-dessus d'une barre d'état. Chaque vue se détache ou se déplace, et la disposition est gardée d'une session à l'autre.

## Run-control

| Commande | Raccourci | Bouton | Effet |
| --- | --- | --- | --- |
| Go | `F5` | ▶ Go | lance la cible |
| Break | `F8` | ■ Break | arrête la cible |
| Step | `F11` | ↓ Step | une ligne source, en entrant dans les fonctions |
| Step Over | `F10` | → Over | une ligne source, par-dessus les appels |
| Step Out (Go.Return) | `Shift+F11` | ↑ Out | jusqu'au retour de la fonction |
| Step instruction | `F7` | ⇣ Asm | une instruction |
| Reset | — | ⟲ Reset | reset de la cible, arrêtée au vecteur de reset (programme natif : relancé au début de `main`) |
| Go.Till, Step.Till / Step.Change | menu Run | — | voir la page [Breakpoints, Go.Till et Step.Change](Breakpoints) |

## Menus

- **Fichier** : Programmer un ELF… (`Ctrl+O`, aussi une image HEX, S-record ou binaire), Reprogrammer l'ELF courant (`Ctrl+R`), ELF récents, Exécuter un script…, Charger un SVD…, Ajouter des symboles…, Enregistrer le layout, Déconnecter, Quitter.
- **Configuration** : Nouvelle…, Ouvrir…, Récentes, Modifier…, Enregistrer (`Ctrl+S`), Enregistrer sous…, Connecter, Fermer ([Lancer GdbTrace](Lancer-GdbTrace)).
- **Run** : les commandes ci-dessus, Go.Till, Step.Till / Step.Change, Arrêt sur faute, Remettre le chrono à zéro.
- **Trace** : ETM par SWO…, ETM par l'ETR (RAM, STM32N6), Filtre de trace ETM…, Arrêter la capture.
- **Vues** : ouvre n'importe quelle vue dans une nouvelle fenêtre.

## Vues

| Zone | Vues |
| --- | --- |
| Gauche | Fichiers, Symboles |
| Code | Source, Désassemblage, Trace, Timing, Couverture |
| Données | Registres, Watch, Périphériques, Système, RTOS, Threads, Pile, Breakpoints, Faute |
| Bas | Console, Mémoire, Terminal, ITM, Profil, Variables tracées, Temps des tâches, Compteurs |

Chaque zone est un jeu d'onglets ; le menu Vues ouvre n'importe quelle vue dans une fenêtre à part.

- **Fichiers** : les sources du programme par dossier, dépliées sur leurs fonctions ; le fichier où s'est arrêté le PC est en gras. Double-clic : la source s'ouvre.
- **Symboles** (sYmbol.Browse) : les fonctions et variables globales de l'ELF (et des ELF ajoutés), filtrées par nom (sous-chaîne, ou motif avec `*` et `?`), avec adresse, taille, fichier et ligne. Double-clic : la source d'une fonction, la mémoire d'une variable ; clic droit : Ajouter au Watch, Désassembler, Afficher en mémoire, Type (`ptype`, cible arrêtée).
- **Source** et **Désassemblage** suivent le PC. Le désassemblage intercale chaque ligne C avant ses instructions (source mêlée). Un double-clic dans la marge pose ou retire un breakpoint.
- Les autres vues sont décrites dans les pages [Breakpoints](Breakpoints), [Données et périphériques](Données-et-périphériques), [RTOS et threads](RTOS-et-threads) et [Trace](Trace-et-mesures).

## Barre d'état

Elle montre de gauche à droite :

- l'état de la cible : ARRÊTÉ, RUNNING, TERMINÉ (programme natif fini) ou DÉCONNECTÉ ;
- le voyant LIVE, allumé quand des vues se rafraîchissent cible en marche ;
- la tâche RTOS courante ;
- le dernier message : raison de l'arrêt, bilan d'un Step.Change, faute en cours (« Faute : BusFault : … »), erreur.

## Disposition

- Clic droit sur un onglet : **Détacher** la vue dans une fenêtre flottante, ou la **Déplacer** vers une autre zone.
- **Fichier › Enregistrer le layout** écrit `gdbtrace-layout.json` ; `--layout` en choisit un autre. Les vues apparues depuis la sauvegarde prennent leur place par défaut.
