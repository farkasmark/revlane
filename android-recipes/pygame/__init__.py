"""pygame recipe for python-for-android, bumped to 2.6.1.

python-for-android v2026.05.09's pygame recipe builds pygame 2.1.0, whose
Cython-0.29-generated C cannot compile against this build's CPython 3.14
target (``longintrepr.h``, opaque frames, dict internals - all gone). Rather
than patch generated C, we build pygame 2.6.1: same ``Setup.Android.SDL2.in``
placeholders, active maintenance, and it cythonizes its _sdl2 modules at
build time with the Cython 3.1.x provided by android-recipes/cython.

Changes vs the stock v2026.05.09 recipe:

* ``version`` 2.1.0 -> 2.6.1
* ``cython`` added to ``depends`` (2.6.1 must cythonize at build time)

Wired in through ``p4a.local_recipes`` in buildozer.spec. Drop this file when
p4a's pygame recipe moves past 2.6.x.
"""

from os.path import join

from pythonforandroid.recipe import CompiledComponentsPythonRecipe
from pythonforandroid.toolchain import current_directory


class Pygame2Recipe(CompiledComponentsPythonRecipe):
    """
    Recipe to build apps based on SDL2-based pygame.

    .. warning:: Some pygame functionality is still untested, and some
        dependencies like freetype, postmidi and libjpeg are currently
        not part of the build. It's usable, but not complete.
    """

    version = '2.6.1'
    url = 'https://github.com/pygame/pygame/archive/{version}.tar.gz'

    site_packages_name = 'pygame'
    name = 'pygame'

    depends = [
        'sdl2', 'sdl2_image', 'sdl2_mixer', 'sdl2_ttf',
        'setuptools', 'jpeg', 'png', 'cython',
    ]
    # pygame 2.6.x cythonizes its _sdl2 modules at build time, so the build
    # python (hostpython) needs Cython importable - p4a's own cython recipe
    # only installs into the target environment.
    hostpython_prerequisites = ['cython==3.1.8']
    call_hostpython_via_targetpython = False  # Due to setuptools
    install_in_hostpython = False

    def prebuild_arch(self, arch):
        super().prebuild_arch(arch)
        with current_directory(self.get_build_dir(arch.arch)):
            setup_template = open(join("buildconfig", "Setup.Android.SDL2.in")).read()
            env = self.get_recipe_env(arch)
            env['ANDROID_ROOT'] = join(self.ctx.ndk.sysroot, 'usr')

            png = self.get_recipe('png', self.ctx)
            png_lib_dir = join(png.get_build_dir(arch.arch), '.libs')
            png_inc_dir = join(png.get_build_dir(arch))

            jpeg = self.get_recipe('jpeg', self.ctx)
            jpeg_inc_dir = jpeg_lib_dir = jpeg.get_build_dir(arch.arch)

            sdl_mixer_includes = ""
            sdl2_mixer_recipe = self.get_recipe('sdl2_mixer', self.ctx)
            for include_dir in sdl2_mixer_recipe.get_include_dirs(arch):
                sdl_mixer_includes += f"-I{include_dir} "

            sdl2_image_includes = ""
            sdl2_image_recipe = self.get_recipe('sdl2_image', self.ctx)
            for include_dir in sdl2_image_recipe.get_include_dirs(arch):
                sdl2_image_includes += f"-I{include_dir} "

            setup_file = setup_template.format(
                sdl_includes=(
                    " -I" + join(self.ctx.bootstrap.build_dir, 'jni', 'SDL', 'include') +
                    " -L" + join(self.ctx.bootstrap.build_dir, "libs", str(arch)) +
                    " -L" + png_lib_dir + " -L" + jpeg_lib_dir + " -L" + arch.ndk_lib_dir_versioned),
                sdl_ttf_includes="-I"+join(self.ctx.bootstrap.build_dir, 'jni', 'SDL2_ttf'),
                sdl_image_includes=sdl2_image_includes,
                sdl_mixer_includes=sdl_mixer_includes,
                jpeg_includes="-I"+jpeg_inc_dir,
                png_includes="-I"+png_inc_dir,
                freetype_includes=""
            )
            setup_file = setup_file.replace(
                "surface src_c/surface.c src_c/alphablit.c src_c/surface_fill.c ",
                "surface src_c/surface.c src_c/alphablit.c src_c/surface_fill.c "
                "src_c/simd_blitters_sse2.c src_c/simd_blitters_avx2.c ",
            )
            # [revlane] pygame 2.6.x's Android template omits the SIMD blitter
            # sources that surface.c calls (upstream template bug: the normal
            # build appends them from setup.py). On ARM the sse2 file compiles
            # via sse2neon; the avx2 file provides runtime-guarded stubs.
            open("Setup", "w").write(setup_file)

    def get_recipe_env(self, arch):
        env = super().get_recipe_env(arch)
        env['USE_SDL2'] = '1'
        env["PYGAME_CROSS_COMPILE"] = "TRUE"
        env["PYGAME_ANDROID"] = "TRUE"
        return env


recipe = Pygame2Recipe()
