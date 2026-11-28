// Extension VS Code de GdbTrace : le type de débogage « gdbtrace » lance l'adaptateur DAP de
// GdbTrace (python -m gdbtrace.dap) ; tout le reste (session, trace, vues de VS Code) passe
// par le protocole. Interpréteur : attribut « python » de la configuration, réglage
// gdbtrace.python, .venv du dossier de travail, sinon python3.
"use strict";
const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

function pythonFor(folder, config) {
  if (config.python) {
    return config.python;
  }
  const setting = vscode.workspace.getConfiguration("gdbtrace").get("python");
  if (setting) {
    return setting;
  }
  if (folder) {
    const venv = process.platform === "win32"
      ? path.join(folder.uri.fsPath, ".venv", "Scripts", "python.exe")
      : path.join(folder.uri.fsPath, ".venv", "bin", "python");
    if (fs.existsSync(venv)) {
      return venv;
    }
  }
  return process.platform === "win32" ? "python" : "python3";
}

class AdapterFactory {
  createDebugAdapterDescriptor(session) {
    const config = session.configuration;
    const folder = session.workspaceFolder;
    const cwd = config.cwd || (folder ? folder.uri.fsPath : undefined);
    return new vscode.DebugAdapterExecutable(pythonFor(folder, config), ["-m", "gdbtrace.dap"],
      { cwd });
  }
}

class ConfigurationProvider {
  resolveDebugConfiguration(folder, config) {
    if (!config.type && !config.request && !config.name) {
      // F5 sans launch.json : l'ELF ouvert dans l'éditeur, s'il y en a un
      const editor = vscode.window.activeTextEditor;
      const file = editor ? editor.document.fileName : "";
      if (!file.endsWith(".elf")) {
        return undefined;                      // VS Code propose alors de créer launch.json
      }
      config.type = "gdbtrace";
      config.request = "launch";
      config.name = "GdbTrace";
      config.program = file;
    }
    if (!config.program && !config.config && !config.args) {
      vscode.window.showErrorMessage("GdbTrace : donner « program », « config » ou « args ».");
      return undefined;
    }
    return config;
  }
}

function activate(context) {
  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory("gdbtrace", new AdapterFactory()),
    vscode.debug.registerDebugConfigurationProvider("gdbtrace", new ConfigurationProvider()));
}

function deactivate() {}

module.exports = { activate, deactivate };
