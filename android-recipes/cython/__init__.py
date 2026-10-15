"""Cython recipe for python-for-android, bumped to the 3.1 series.

python-for-android v2026.05.09 pins Cython 0.29.36, which cannot generate C
for CPython 3.14 (the target Python of this build). Cython 3.1.x is the first
line with official CPython 3.14 support. pygame 2.6.x needs Cython 3 at build
time anyway (it no longer ships pregenerated C for its _sdl2 modules), so
this override goes hand in hand with android-recipes/pygame.

Wired in through ``p4a.local_recipes`` in buildozer.spec. Drop when p4a bumps
its cython recipe.
"""

from pythonforandroid.recipe import CompiledComponentsPythonRecipe


class CythonRecipe(CompiledComponentsPythonRecipe):

    version = '3.1.8'
    url = 'https://github.com/cython/cython/archive/{version}.tar.gz'
    site_packages_name = 'cython'
    depends = ['setuptools']
    call_hostpython_via_targetpython = False
    install_in_hostpython = True


recipe = CythonRecipe()
