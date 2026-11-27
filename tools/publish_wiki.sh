#!/bin/sh
# Publie Documentations/ dans le wiki GitHub du dépôt : clone le wiki, y remplace les pages
# par celles du dépôt (les pages absentes du dépôt sont retirées), commit et pousse.
#
#   tools/publish_wiki.sh              # wiki de « origin » (…/gdbtrace.wiki.git)
#   WIKI_URL=… tools/publish_wiki.sh   # un autre wiki
#   tools/publish_wiki.sh --dry-run    # montre les changements sans pousser
#
# À lancer là où git a accès au wiki (identifiants GitHub de l'utilisateur). Le wiki doit
# exister : sur GitHub, onglet Wiki, « Create the first page » une fois.
set -eu
cd "$(dirname "$0")/.."
dry=0
[ "${1:-}" = "--dry-run" ] && dry=1
origin=$(git remote get-url origin)
url=${WIKI_URL:-$(printf '%s' "$origin" | sed -e 's#\.git$##' -e 's#$#.wiki.git#')}
rev=$(git rev-parse --short HEAD)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
if ! git clone -q "$url" "$tmp/wiki"; then
    echo "wiki introuvable : $url" >&2
    echo "le créer sur GitHub (onglet Wiki, « Create the first page »), puis relancer" >&2
    exit 1
fi
find "$tmp/wiki" -maxdepth 1 -name '*.md' -delete
cp Documentations/*.md "$tmp/wiki/"
cd "$tmp/wiki"
git add -A
if git diff --cached --quiet; then
    echo "wiki déjà à jour ($rev)"
    exit 0
fi
git diff --cached --stat
if [ "$dry" = 1 ]; then
    echo "--dry-run : rien n'est poussé"
    exit 0
fi
git commit -q -m "Documentation du dépôt ($rev)"
git push -q origin HEAD
echo "wiki publié : $rev"
