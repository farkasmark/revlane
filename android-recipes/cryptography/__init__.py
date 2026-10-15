"""cryptography recipe for python-for-android, with a libpython link fix.

python-for-android v2026.05.09's recipe builds ``_rust.abi3.so`` via pyo3 in
abi3 (stable ABI) mode. pyo3 does not link libpython for abi3 extension
modules - on the desktop the interpreter executable exports the symbols, but
on Android extensions can only resolve symbols from their own DT_NEEDED
libraries, so the module fails to load with::

    dlopen failed: cannot locate symbol "PyExc_TypeError" referenced by
    ".../_rust.abi3.so"

Every other extension in a p4a build (stdlib modules, pygame, ...) links
``libpython3.14.so`` explicitly; this override does the same for the Rust
extension by appending a ``-lpython3.14`` link argument.

Wired in through ``p4a.local_recipes`` in buildozer.spec. Drop this file when
p4a's cryptography recipe links libpython itself.
"""

from os.path import join

from pythonforandroid.recipe import RustCompiledComponentsRecipe


class CryptographyRecipe(RustCompiledComponentsRecipe):

    name = 'cryptography'
    version = '46.0.3'
    url = 'https://github.com/pyca/cryptography/archive/refs/tags/{version}.tar.gz'
    depends = ['openssl', 'cffi']

    def get_recipe_env(self, arch, **kwargs):
        env = super().get_recipe_env(arch, **kwargs)
        openssl_build_dir = self.get_recipe('openssl', self.ctx).get_build_dir(arch.arch)
        build_target = self.RUST_ARCH_CODES[arch.arch].upper().replace("-", "_")
        openssl_include = "{}_OPENSSL_INCLUDE_DIR".format(build_target)
        openssl_libs = "{}_OPENSSL_LIB_DIR".format(build_target)
        env[openssl_include] = join(openssl_build_dir, 'include')
        env[openssl_libs] = join(openssl_build_dir)

        # [revlane] abi3 + Android: resolve python API symbols from the
        # shared libpython, like every other extension module in the build.
        env["RUSTFLAGS"] = env.get("RUSTFLAGS", "") + " -Clink-args=-lpython3.14"
        return env


recipe = CryptographyRecipe()
