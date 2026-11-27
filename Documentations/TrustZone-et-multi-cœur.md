Cœurs ARMv8-M à extension de sécurité (TrustZone : Cortex-M23, M33, M35P, M55, M85) et puces à
plusieurs cœurs. Démos sous QEMU, sans matériel : `examples/tz-an505` (deux mondes) et
`examples/multicore-an521` (deux Cortex-M33).

## TrustZone

### La démo sous QEMU

`examples/tz-an505` (QEMU `mps2-an505`, Cortex-M33) a deux images :

- `secure.elf`, le monde sécurisé, chargé par QEMU au reset : il rend non sécurisées une partie
  des SSRAM (MPC), déclare au SAU le code et la RAM non sécurisés et la zone NSC des veneers,
  puis démarre le monde non sécurisé (BLXNS) ;
- `nonsecure.elf`, le monde non sécurisé : il appelle en boucle `secure_add`, fonction d'entrée
  du monde sécurisé, par son veneer (instruction SG en zone NSC).

```bash
make -C examples/tz-an505
.venv/bin/python -m gdbtrace examples/tz-an505/secure.elf --machine mps2-an505 \
    --qemu-load examples/tz-an505/nonsecure.elf --add-symbols examples/tz-an505/nonsecure.elf
```

`--qemu-load ELF` fait charger une autre image par QEMU (`-device loader`, rechargée à chaque
reset) ; `--add-symbols ELF` en donne les symboles à gdb. Dans une configuration :
`serveur.qemu-load` (liste) et `cible.add-symbols`.

`ns_fault_kind` à 1 (écrit après l'arrivée dans `ns_main`) fait appeler au monde non sécurisé
une adresse sécurisée qui n'est pas une entrée : SecureFault INVEP.

### État du cœur

À chaque arrêt, GdbTrace dit dans quel monde est le cœur :

- **barre d'état** : « S » (sécurisé) ou « NS » à côté de l'état ;
- **vue Registres** : ligne « état » (monde, mode, pile en service, et d'où vient
  l'information), puis les registres bancaires groupés **Sécurisé** (`msp_s`, `psp_s`,
  `msplim_s`, `psplim_s`, `control_s`, `primask_s`, `basepri_s`, `faultmask_s`) et
  **Non sécurisé** (`_ns`) ; CONTROL décodé (privilège, pile, FPCA, SFPA).

Le monde vient de DSCSR.CDS (0xE000EE08) ; QEMU le lit toujours à 0. GdbTrace compare donc
d'abord `sp` aux pointeurs de pile bancaires (dans le monde courant, `sp` est l'un d'eux) ;
DSCSR départage s'ils sont égaux.

### Pile à travers les deux mondes

gdb s'arrête au changement de monde :

- **arrêté dans le sécurisé, appelé par le non sécurisé** : gdb donne le premier cadre non
  sécurisé, puis s'arrête (« previous frame inner to this frame » : les deux piles ne sont pas
  ordonnées) ;
- **arrêté dans le non sécurisé, appelé par le sécurisé** : gdb s'arrête au pseudo-cadre
  FNC_RETURN (0xFEFFFFFF) sans lire la pile sécurisée.

La vue Pile, `gdbtrace.bt()` et l'outil MCP `security` donnent la pile entière, chaque cadre
avec son monde (colonne **Monde**) et les changements de monde annotés :

```
#0 [S] 1000004a secure_add secure.c:43
#1 [NS] 0020005a ns_step nonsecure.c:16  (appel depuis le monde non sécurisé (SG))
#2 [NS] 00200074 ns_main nonsecure.c:22
#3 [NS] 002000e4 NS_Reset_Handler nonsecure.c:38
#4 [NS] feffffff <signal handler called>   (retour vers le monde sécurisé (FNC_RETURN))
#5 [S] 100002a8 __gnu_cmse_nonsecure_call …  (appelant sécurisé (cadre FNC_RETURN à 0x380fff20))
#6 [S] 100001ba start_nonsecure secure.c:78
#7 [S] 100001f8 main secure.c:83
```

Pour dérouler l'autre côté, GdbTrace écrit dans le cœur les registres du cadre où gdb s'est
arrêté, avec le pointeur de pile de l'autre monde, demande la pile à gdb, puis remet les
registres. L'appelant sécurisé d'un appel BLXNS est pris dans le cadre FNC_RETURN, en haut de la
pile sécurisée. Arrêté dans le non sécurisé, QEMU refuse au débogueur la mémoire sécurisée :
GdbTrace la lit par le moniteur de QEMU (espace physique), et ne donne que le premier cadre
sécurisé.

### SAU, MPU, fautes

- **Vue Système, onglet SAU** : SAU_CTRL (activé, ALLNS), nombre de régions, chaque région
  (début, fin, NS ou NSC). Le SAU ne se lit que depuis le monde sécurisé. Sous QEMU 8.2, le
  débogueur ne peut pas choisir la région (SAU_RNR) : seule celle que le firmware a choisie en
  dernier est décodée, la note le dit.
- **Vue Système, onglet MPU** : la MPU du monde courant ; la case **MPU non sécurisée** la lit
  par l'alias du SCS (0xE002ED90) depuis le monde sécurisé.
- **Vue Faute** : SFSR et SFAR décodés (SecureFault : INVEP, INVIS, INVER, AUVIOL, INVTRAN…),
  CFSR du monde non sécurisé par l'alias (`CFSR_NS`), monde du cœur ; le cadre empilé est lu
  sur la pile du monde interrompu (EXC_RETURN.S : `MSP_NS` pour une SecureFault venue du non
  sécurisé). INVEP : le PC empilé est l'adresse appelée, l'appelant est donné.

### En script

```python
st = gdbtrace.security()      # « état : sécurisé, Thread, pile MSP_S (pile : sp = msp_s) »
st.label, st.banked["msp_ns"]
gdbtrace.sau()                # régions du SAU
gdbtrace.stack()              # [WorldFrame(frame, world, note)]
gdbtrace.mpu(nonsecure=True)  # MPU non sécurisée
```

## Plusieurs cœurs

### Déclarer les cœurs

`--core NOM[=SOURCE][,ELF]`, répétable (clé `cible.coeurs` d'une configuration, champ Cœurs de
sa boîte de dialogue) :

| Forme | Cœur |
| --- | --- |
| `--core NOM` | le cœur de la session, nommé (défaut : « cœur 0 ») |
| `--core NOM=N` | le processus N du même serveur gdb : QEMU présente chaque cluster d'une machine comme un processus (`mps2-an521` : le second Cortex-M33 est le processus 2) |
| `--core NOM=HÔTE:PORT` | un autre serveur gdb : second port d'un OpenOCD qui sert deux cœurs (3334), un autre ST-LINK_gdbserver… |
| `,ELF` | les symboles du cœur (défaut : l'ELF de la session) |

```bash
make -C examples/multicore-an521
.venv/bin/python -m gdbtrace examples/multicore-an521/multicore.elf --machine mps2-an521 \
    --core cpu0 --core cpu1=2
# STM32H7 à deux cœurs, OpenOCD sur 3333 (M7) et 3334 (M4) :
.venv/bin/python -m gdbtrace cm7.elf --openocd board.cfg --core cm7 --core cm4=127.0.0.1:3334,cm4.elf
```

### Comment ils se pilotent

- **Cœurs d'un même serveur** : un seul gdb, chaque cœur est un « inférieur ». Un arrêt (d'un
  breakpoint de n'importe quel cœur) les arrête tous, Go les relance tous ; un pas n'avance que
  le cœur choisi. À l'arrêt, le cœur qui s'est arrêté devient le cœur choisi ; après Break, le
  cœur choisi le reste.
- **Cœurs d'autres serveurs** : ni QEMU ni OpenOCD n'ont le mode non-stop qu'exigerait un seul
  gdb pour deux connexions ; chaque cœur a sa session gdb. Go, Break et les pas visent le cœur
  choisi ; Run › **Go (tous les cœurs)** et **Break (tous les cœurs)** les visent tous.
- **Interface** : le sélecteur **Cœur** de la barre d'état (avec l'état de chaque cœur) choisit
  le cœur que montrent les vues (registres, pile, source, variables, mémoire).
- **Trace** : la trace (ITM, ETM, MTB), le chrono et les compteurs restent ceux du cœur de la
  session. Sous QEMU, l'ETM simulé ne garde que les instructions du cœur 0 (les lignes du
  journal de QEMU portent le numéro du CPU).

### Arrêt synchrone par les CTI

Par gdb, l'arrêt des autres cœurs est logiciel : ils exécutent encore des milliers
d'instructions. `gdbtrace.cti_sync()` programme les CTI (Cross Trigger Interface) trouvés dans
les tables ROM : l'arrêt d'un cœur passe par le canal 0 et devient une demande d'arrêt pour les
autres, à quelques cycles près ; la demande est acquittée avant chaque reprise. Prêt pour une
STM32H7, pas encore passé sur carte ; QEMU n'a pas de CTI (`cti_sync` le dit).

### En script et par le MCP

```python
gdbtrace.cores()              # « * cpu1  arrêté  processus 2 du même serveur »
gdbtrace.core("cpu0")         # le cœur des commandes suivantes ; rend sa pile
gdbtrace.go_all(); gdbtrace.halt_all()
gdbtrace.cti_sync()
```

Outil MCP `core` : sans `name`, la liste ; avec `name`, le cœur choisi.
