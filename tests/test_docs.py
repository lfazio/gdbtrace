"""La page Scripting Python du wiki (Documentations/Scripting.md) est exécutable : chaque bloc `python` précédé de
`<!-- test -->` (ou `<!-- test: OPTIONS -->`) tourne en --batch sur la démo et doit finir
sans erreur. Un exemple de la notice qui ne marche plus fait échouer la suite."""
import os
import re
import shlex
import subprocess
import sys
import unicodedata

import pytest
from conftest import ROOT, needs_qemu

DOC = os.path.join(ROOT, "Documentations", "Scripting.md")
_BLOCK = re.compile(r"<!-- test(?::\s*(?P<opts>.*?))?\s*-->\s*\n```python\n(?P<code>.*?)```", re.S)


def blocks() -> list[tuple[str, str, str]]:
    """[(identifiant, options, code)] ; identifiant = titre de la section + rang du bloc."""
    text = open(DOC, encoding="utf-8").read()
    out, seen = [], {}
    for m in _BLOCK.finditer(text):
        title = re.findall(r"^#{2,3} (.+)$", text[:m.start()], re.M)[-1]
        ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
        seen[slug] = seen.get(slug, 0) + 1
        out.append((f"{slug}-{seen[slug]}", m.group("opts") or "", m.group("code")))
    return out


def test_doc_has_runnable_examples():
    assert len(blocks()) >= 12


@needs_qemu
@pytest.mark.parametrize("name,opts,code", blocks(), ids=[b[0] for b in blocks()])
def test_doc_example(name, opts, code, elf, rtos_elf, tmp_path):
    args = shlex.split(opts)
    target = elf
    for k, a in enumerate(list(args)):
        if a.endswith(".elf") and k == 0:       # autre firmware que la démo (FreeRTOS...)
            target = rtos_elf if "freertos" in a else os.path.join(ROOT, a)
            if not os.path.exists(target) and os.path.exists(os.path.join(os.path.dirname(target), "Makefile")):
                subprocess.run(["make", "-s", "-C", os.path.dirname(target)], check=True)
            args.remove(a)
        elif a.startswith("examples/native/"):  # programme de l'hôte, construit ici
            subprocess.run(["make", "-s", "-C", os.path.join(ROOT, "examples", "native")],
                           check=True)
            target = os.path.join(ROOT, a)
            args.remove(a)
    args = [os.path.join(ROOT, a) if os.path.exists(os.path.join(ROOT, a)) else a for a in args]
    script = tmp_path / "exemple.py"
    script.write_text(code, encoding="utf-8")
    # dossier de travail temporaire : les fichiers écrits par l'exemple (CSV, rapports) y restent
    r = subprocess.run([sys.executable, "-m", "gdbtrace", target, *args, "--batch",
                        "--script", str(script)], cwd=tmp_path, capture_output=True, text=True,
                       timeout=120, env={**os.environ, "PYTHONPATH": ROOT})
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "Traceback" not in out and "erreur :" not in out, out


def test_wiki_pages_and_links():
    """Documentations/ : les pages du wiki. Chaque lien `[texte](Page)` mène à une page qui
    existe, et la barre latérale (_Sidebar) les donne toutes."""
    import glob
    wiki = os.path.join(ROOT, "Documentations")
    pages = {os.path.basename(p)[:-3] for p in glob.glob(os.path.join(wiki, "*.md"))}
    assert {"Home", "_Sidebar", "Scripting", "Installation"} <= pages
    broken = []
    for name in pages:
        text = open(os.path.join(wiki, f"{name}.md"), encoding="utf-8").read()
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if not target.startswith(("http", "#", "mailto:")) and target.split("#")[0] not in pages:
                broken.append(f"{name} → {target}")
    assert not broken, broken
    sidebar = open(os.path.join(wiki, "_Sidebar.md"), encoding="utf-8").read()
    missing = [p for p in pages - {"_Sidebar", "Home"} if f"]({p})" not in sidebar]
    assert not missing, missing
