"""Arborescence de la vue Fichiers : fonctions pures, sans GUI."""
from gdbtrace.views.files import EXTERNAL, build_tree, racine_projet


def test_chemins_relatifs_sous_externes():
    # picolibc précompilée : des chemins relatifs à un dossier de construction absent
    files = ["/p/src/a.c", "/p/src/b.c", "../../libc/machine/arm/memcpy-armv7m.S"]
    root, tree = build_tree(files, "/p")
    assert root == "/p/src"
    assert set(tree) == {"a.c", "b.c", EXTERNAL}
    assert "memcpy-armv7m.S" in tree[EXTERNAL]["../../libc/machine/arm/"]


def test_que_des_chemins_relatifs():
    root, tree = build_tree(["../x/a.S", "../y/b.S"], None)
    assert set(tree) == {EXTERNAL}


def test_racine_projet_remonte_au_depot():
    # un ELF construit loin des sources : la racine est le dépôt, pas le dossier de l'ELF
    elf = "/r/build/f746/src/platform/stm32f746/emu.elf"
    files = ["/r/src/emu/bus.c", "/r/src/platform/host/board.c",
             "/r/build/f746/_deps/hal/stm32f7xx_hal.c", "/usr/lib/gcc/arm/libgcc.c",
             "../../libc/machine/arm/strcmp-armv7.S"]
    assert racine_projet(elf, files) == "/r"


def test_racine_projet_reste_dans_le_dossier_de_l_exemple():
    elf = "/g/examples/firmware/demo.elf"
    files = ["/g/examples/firmware/main.c", "/g/examples/firmware/startup.c",
             "/usr/lib/gcc/arm/libgcc.c"]
    assert racine_projet(elf, files) == "/g/examples/firmware"
    assert racine_projet(elf, ["/usr/lib/x.c"]) is None
