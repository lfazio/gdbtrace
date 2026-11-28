#!/bin/sh
# Documentations/ est le wiki GitHub du dépôt (sous-module, branche master). Ce script publie
# les pages modifiées dans le dépôt : commit dans le sous-module, rebase sur les pages
# modifiées entre-temps sur GitHub, push vers le wiki, puis pointeur du sous-module ajouté à
# l'index du dépôt (à committer avec le reste).
#
#   tools/publish_wiki.sh "message"    # pages modifiées : commit avec ce message, puis push
#   tools/publish_wiki.sh              # commits déjà faits dans le sous-module : push seul
#   tools/publish_wiki.sh --pull       # pages modifiées sur GitHub : sous-module mis à jour
#
# À lancer là où git a accès au wiki (identifiants GitHub de l'utilisateur ; pas depuis une
# session cloud).
set -eu
cd "$(dirname "$0")/.."
if ! git -C Documentations rev-parse --show-superproject-working-tree 2>/dev/null | grep -q .; then
    echo "sous-module Documentations/ absent : git submodule update --init Documentations" >&2
    exit 1
fi
wiki() { git -C Documentations "$@"; }
wiki fetch -q origin master
if [ "${1:-}" = "--pull" ]; then
    wiki rebase -q FETCH_HEAD
    git add Documentations
    echo "wiki relu : $(wiki rev-parse --short HEAD) ; pointeur ajouté à l'index (à committer)"
    exit 0
fi
if [ -n "$(wiki status --porcelain)" ]; then
    if [ -z "${1:-}" ]; then
        echo "pages modifiées : donner le message du commit du wiki" >&2
        wiki status --short >&2
        exit 1
    fi
    wiki add -A
    wiki commit -q -m "$1"
fi
wiki rebase -q FETCH_HEAD
if [ "$(wiki rev-parse HEAD)" = "$(wiki rev-parse FETCH_HEAD)" ]; then
    echo "wiki déjà à jour : $(wiki rev-parse --short HEAD)"
else
    wiki push -q origin HEAD:master
    echo "wiki publié : $(wiki rev-parse --short HEAD)"
fi
git add Documentations
