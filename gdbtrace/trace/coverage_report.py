"""Rapports de couverture (COVerage.EXPORT) : LCOV, HTML autonome, base JSON.

- LCOV (`.info`) : lu par genhtml, GitLab, Jenkins, SonarQube, Codecov... Lignes (DA),
  fonctions (FN/FNDA) et branches (BRDA : issue « pris » puis « non pris » de chaque
  branchement conditionnel du code objet) ;
- HTML (`.html`) : rapport autonome, sans dépendance : résumé, tableau fichiers/fonctions,
  source annotée ligne par ligne (état écrit en toutes lettres, pas seulement en couleur) ;
- JSON (`.json`) : la base elle-même, rechargeable et fusionnable (COVerage.LOAD /Add).
"""
from __future__ import annotations

import datetime
import html
import os

from .coverage import (I_OK, NEVER, OK, PARTIAL, TAG_HELP, CoverageDB, Counts)

FORMATS = {".info": "lcov", ".lcov": "lcov", ".html": "html", ".htm": "html", ".json": "json"}


def export(db: CoverageDB, path: str, note: str = "") -> str:
    """Écrit la couverture au format donné par l'extension ; retourne le format."""
    fmt = FORMATS.get(os.path.splitext(path)[1].lower())
    if fmt is None:
        raise ValueError(f"{path} : extension inconnue ({', '.join(sorted(FORMATS))})")
    if fmt == "lcov":
        write_lcov(db, path)
    elif fmt == "html":
        write_html(db, path, note)
    else:
        db.save(path)
    return fmt


# ====================================================================== LCOV
def write_lcov(db: CoverageDB, path: str, test_name: str = "") -> None:
    with db._lock:
        e, n = dict(db.e), dict(db.n)
    out = []
    for fc in db.files():
        if fc.path == "?":
            continue
        out.append(f"TN:{test_name}")
        out.append(f"SF:{fc.path}")
        for f in fc.functions:
            name = f.name.split(" = ")[0]
            out.append(f"FN:{f.line or 0},{name}")
        for f in fc.functions:
            out.append(f"FNDA:{f.entries},{f.name.split(' = ')[0]}")
        out.append(f"FNF:{len(fc.functions)}")
        out.append(f"FNH:{sum(1 for f in fc.functions if f.covered)}")
        lines = db.lines(fc.path)
        brf = brh = 0
        for ln in sorted(lines):
            block = 0
            for ins in db.instructions(fc.path, ln):
                if not ins.branch:
                    continue
                ne, nn = e.get(ins.addr, 0), n.get(ins.addr, 0)
                reached = ne + nn > 0
                out.append(f"BRDA:{ln},{block},0,{ne if reached else '-'}")
                out.append(f"BRDA:{ln},{block},1,{nn if reached else '-'}")
                brf += 2
                brh += (ne > 0) + (nn > 0)
                block += 1
        out.append(f"BRF:{brf}")
        out.append(f"BRH:{brh}")
        for ln in sorted(lines):
            c = lines[ln]
            out.append(f"DA:{ln},{c.hits if c.covered else 0}")
        out.append(f"LF:{len(lines)}")
        out.append(f"LH:{sum(1 for c in lines.values() if c.covered)}")
        out.append("end_of_record")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


# ====================================================================== HTML
MARK = {OK: "✔", PARTIAL: "½", NEVER: "✘"}

CSS = """
:root { --bg:#fcfcfb; --fg:#0b0b0b; --fg2:#52514e; --muted:#898781; --line:#e1e0d9;
  --ok:#e3f2e5; --ok-ink:#1d6b2f; --part:#fdf1d6; --part-ink:#8a5a00; --never:#fbe4e4; --never-ink:#a01f1f;
  --bar:#2a78d6; --bar-bg:#e1e0d9; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ececea; --fg2:#b9b8b3; --muted:#8a8984;
  --line:#2e2e2c; --ok:#17301c; --ok-ink:#8fd49c; --part:#3a2d0d; --part-ink:#f0c060; --never:#3b1717;
  --never-ink:#f29a9a; --bar:#5b9be6; --bar-bg:#2e2e2c; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 32px 0 8px; }
.sub { color: var(--fg2); margin-bottom: 20px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.tile { border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.tile .v { font-size: 26px; font-weight: 600; } .tile .l { color: var(--fg2); font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line); vertical-align: middle; }
th { color: var(--fg2); font-weight: 600; font-size: 13px; }
td.num { text-align: right; white-space: nowrap; }
tr.file td { font-weight: 600; background: color-mix(in srgb, var(--line) 35%, transparent); }
tr.func td:first-child { padding-left: 24px; }
.bar { display: inline-block; width: 90px; height: 8px; background: var(--bar-bg); border-radius: 4px;
  vertical-align: middle; margin-right: 6px; overflow: hidden; }
.bar > span { display: block; height: 100%; background: var(--bar); }
.st { white-space: nowrap; font-weight: 600; }
.st.ok { color: var(--ok-ink); } .st.partiel { color: var(--part-ink); } .st.jamais { color: var(--never-ink); }
.src { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
.src table { font: 12.5px/1.5 ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; }
.src td { border: 0; padding: 0 8px; white-space: pre; }
.src td.n, .src td.m, .src td.c { width: 1%; }
.wide { overflow-x: auto; }
h2 .h2sub { font-weight: 400; color: var(--fg2); font-size: 14px; }
.src td.n, .src td.c { color: var(--muted); text-align: right; user-select: none; }
.src td.m { width: 1.5em; text-align: center; font-weight: 700; }
.src tr.ok { background: var(--ok); } .src tr.ok td.m { color: var(--ok-ink); }
.src tr.partiel { background: var(--part); } .src tr.partiel td.m { color: var(--part-ink); }
.src tr.jamais { background: var(--never); } .src tr.jamais td.m { color: var(--never-ink); }
.src tr.note td { color: var(--part-ink); font-size: 12px; background: var(--part); }
.legend { color: var(--fg2); font-size: 13px; margin: 8px 0 0; }
a { color: inherit; }
.muted { color: var(--muted); }
"""


def _bar(pct: float | None) -> str:
    if pct is None:
        return '<span class="muted">—</span>'
    return f'<span class="bar"><span style="width:{pct:.0f}%"></span></span>{pct:.1f} %'


def _status(st: str) -> str:
    return f'<span class="st {st}">{MARK[st]} {st}</span>'


def _row(cls: str, label: str, c: Counts, entries: str) -> str:
    br = f"{c.outcomes}/{2 * c.branches}" if c.branches else ""
    return (f'<tr class="{cls}"><td>{label}</td><td>{_status(c.status)}</td>'
            f'<td class="num">{_bar(c.percent)}</td><td class="num">{c.covered}/{c.instrs}</td>'
            f'<td class="num">{_bar(c.branch_percent)}</td><td class="num">{br}</td>'
            f'<td class="num">{entries}</td></tr>')


def write_html(db: CoverageDB, path: str, note: str = "") -> None:
    total, called, nfuncs = db.total()
    files = [f for f in db.files() if f.path != "?"]
    nlines = {OK: 0, PARTIAL: 0, NEVER: 0}
    per_file_lines = {}
    for fc in files:
        lines = db.lines(fc.path)
        per_file_lines[fc.path] = lines
        for c in lines.values():
            nlines[c.status] += 1
    # chemins relatifs au projet (dossier parent de celui de l'ELF), bibliothèques en dernier
    hint = os.path.dirname(os.path.dirname(os.path.abspath(db.image.path))) + os.sep
    inside = lambda p: p.startswith(hint)   # noqa: E731
    rel = lambda p: os.path.relpath(p, hint) if inside(p) else p   # noqa: E731
    files.sort(key=lambda f: (not inside(f.path), f.path))
    elf = os.path.basename(db.image.path)
    h = html.escape
    parts = [f"<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
             f"<title>Couverture {h(elf)}</title><style>{CSS}</style></head><body><main>",
             f"<h1>Couverture de code — {h(elf)}</h1>",
             f"<div class='sub'>{h(datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))}"
             f"{' — ' + h(note) if note else ''} — mesurée sur le code objet par la trace "
             f"d'instructions (sans instrumentation)</div>",
             "<div class='tiles'>",
             f"<div class='tile'><div class='v'>{total.percent:.1f} %</div>"
             f"<div class='l'>instructions ({total.covered}/{total.instrs})</div></div>",
             f"<div class='tile'><div class='v'>"
             f"{'—' if total.branch_percent is None else f'{total.branch_percent:.1f} %'}</div>"
             f"<div class='l'>branches : issues vues ({total.outcomes}/{2 * total.branches})</div></div>",
             f"<div class='tile'><div class='v'>{nlines[OK]} / {nlines[PARTIAL]} / {nlines[NEVER]}</div>"
             f"<div class='l'>lignes ok / partielles / jamais</div></div>",
             f"<div class='tile'><div class='v'>{called}/{nfuncs}</div>"
             f"<div class='l'>fonctions entrées</div></div></div>",
             "<h2>Fichiers et fonctions</h2><div class='wide'><table><thead><tr><th>Fichier / fonction</th><th>État</th>"
             "<th class='num'>Instructions</th><th class='num'></th><th class='num'>Branches</th>"
             "<th class='num'>Issues</th><th class='num'>Entrées</th></tr></thead><tbody>"]
    for i, fc in enumerate(files):
        parts.append(_row("file", f"<a href='#f{i}'>{h(rel(fc.path))}</a>", fc, ""))
        for f in fc.functions:
            parts.append(_row("func", h(f.name), f, str(f.entries)))
    parts.append("</tbody></table></div>")
    parts.append("<p class='legend'>✔ ok : tout exécuté, chaque branchement pris et non pris — "
                 "◐ partiel : instruction jamais exécutée ou branchement d'un seul côté — "
                 "✘ jamais exécuté.</p>")
    for i, fc in enumerate(files):
        lines = per_file_lines[fc.path]
        parts.append(f"<h2 id='f{i}'>{h(rel(fc.path))} — {_status(fc.status)} "
                     f"<span class='h2sub'>{fc.percent:.1f} % des instructions</span></h2>")
        try:
            with open(fc.path, encoding="utf-8", errors="replace") as fh:
                src = fh.read().splitlines()
        except OSError:
            parts.append("<p class='sub'>source introuvable</p>")
            continue
        parts.append("<div class='src'><table>")
        for ln, text in enumerate(src, 1):
            c = lines.get(ln)
            cls, mark, count = (c.status, MARK[c.status], str(c.hits) if c.covered else "0") if c else ("", "", "")
            parts.append(f"<tr class='{cls}'><td class='n'>{ln}</td><td class='m'>{mark}</td>"
                         f"<td class='c'>{count}</td><td>{h(text)}</td></tr>")
            if c is not None and c.status == PARTIAL:
                for ins in db.instructions(fc.path, ln):
                    tag = db.tag(ins.addr)
                    if tag != I_OK:
                        parts.append(f"<tr class='note'><td></td><td></td><td></td><td>"
                                     f"    ↳ {ins.addr:08x}  {h(ins.text)} : {h(TAG_HELP[tag])}</td></tr>")
        parts.append("</table></div>")
    parts.append("</main></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
