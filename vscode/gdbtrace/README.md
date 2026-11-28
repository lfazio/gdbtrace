# GdbTrace pour VS Code

Le type de débogage **gdbtrace** lance l'adaptateur DAP de GdbTrace (`python -m gdbtrace.dap`)
: une session GdbTrace sans interface, pilotée par les vues de débogage de VS Code. Mêmes cibles
que GdbTrace : QEMU (défaut), OpenOCD, ST-LINK_gdbserver, serveur gdb déjà lancé, programme de
l'hôte ; mêmes options, ou une configuration de session (YAML).

## Installation

1. GdbTrace installé dans un Python : `python3 -m venv .venv && .venv/bin/pip install -e '.[etm]'`
   depuis le dépôt (l'extension prend le `.venv` du dossier de travail, sinon le réglage
   `gdbtrace.python`, sinon `python3`).
2. L'extension, empaquetée avec Node.js :

   ```bash
   cd vscode/gdbtrace
   npx @vscode/vsce package --skip-license   # → gdbtrace-0.1.0.vsix (pas de fichier de licence)
   code --install-extension gdbtrace-0.1.0.vsix
   ```

   Pour la modifier : ouvrir `vscode/gdbtrace` dans VS Code, F5 (fenêtre « Extension
   Development Host »).

## launch.json

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "type": "gdbtrace", "request": "launch", "name": "Démo sous QEMU",
      "program": "${workspaceFolder}/examples/firmware/demo.elf",
      "args": ["--etm-sim"]
    },
    {
      "type": "gdbtrace", "request": "launch", "name": "Nucleo-F746ZG",
      "config": "${workspaceFolder}/configs/nucleo-f746zg.yaml"
    },
    {
      "type": "gdbtrace", "request": "launch", "name": "Programme de l'hôte",
      "program": "${workspaceFolder}/examples/native/pthreads", "args": ["--args", "100"]
    }
  ]
}
```

| Attribut | Rôle |
| --- | --- |
| `program` | ELF : firmware Cortex-M ou programme de l'hôte |
| `config` | configuration de session (YAML) |
| `args` | autres options de la ligne de commande de GdbTrace (liste ou texte) |
| `cwd` | répertoire de travail (chemins relatifs des options) |
| `stopOnEntry` | rester arrêté après la connexion (défaut : Go) |
| `python` | interpréteur de cette configuration |

## Ce que donne la session

- Breakpoints de ligne (condition, Nᵉ passage, message `{expr}` sans arrêt), de fonction,
  d'instruction (vue Désassemblage) ; arrêt sur faute Cortex-M (case « Fautes Cortex-M »).
- Pile, locales (structures et tableaux dépliables), registres de la frame, périphériques du SVD
  (registres décodés, champs).
- Threads : threads de gdb (pthreads), autres cœurs (`--core`), tâches FreeRTOS (leur pile).
- Console de débogage : une expression C ; `>code` exécute du Python avec l'objet `gdbtrace`
  (`>gdbtrace.coverage()`, `>gdbtrace.task_times()`…) ; `-exec cmd` une commande de gdb.
- Mémoire (éditeur hexadécimal de VS Code), désassemblage, pas d'instruction.

## Autres éditeurs

Tout client DAP lance `python -m gdbtrace.dap` (ou `gdbtrace-dap`) sur stdio. nvim-dap :

```lua
local dap = require("dap")
dap.adapters.gdbtrace = { type = "executable", command = "gdbtrace-dap" }
dap.configurations.c = {
  { type = "gdbtrace", request = "launch", name = "GdbTrace",
    program = "${workspaceFolder}/build/firmware.elf", args = { "--etm-sim" } },
}
```

Emacs (dap-mode) :

```elisp
(dap-register-debug-provider "gdbtrace"
  (lambda (conf) (plist-put conf :dap-server-path '("gdbtrace-dap"))))
(dap-register-debug-template "GdbTrace QEMU"
  (list :type "gdbtrace" :request "launch" :name "GdbTrace"
        :program "build/firmware.elf" :args ["--etm-sim"]))
```
