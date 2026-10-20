# Capsa private Python runtime recipe inputs

This companion recipe builds `capsa-python-runtime` for Ubuntu 24.04 amd64.
It contains a private CPython 3.12.15 under `/usr/lib/capsa/python` plus the
29 production distributions required by the federation application. It does
not contain the federation application itself.

`runtime-inputs.json`, `wheel-inputs.json`, and `wheel-metadata.json` are the
production/build-bootstrap subset of the supplied runtime records.
`runtime-production.hashed.lock` is the corresponding hash-locked production
closure. Test packages and `packaging`, `setuptools`, and `wheel` are excluded;
the separately hash-locked `pip` wheel is used only while assembling the
package and is not shipped.

`ci/build-capsa-runtime.sh` obtains these immutable inputs only during the
build, verifies every SHA-256, and writes its artifact to
`build/capsa-runtime/`. At build time it copies the shared Capsa
`upgrade-guard` as `preinst`, so upgrades refuse to replace this private
interpreter while a Capsa service is active. The package has no service action,
virtual environment, user-home content, download, or other install-time hook.

The federation runtime is headless. The recipe removes CPython's optional
Tcl/Tk extension and libraries before ELF dependency analysis, so GUI/X11
packages are not brought in solely by unused interpreter components. The
remaining system dependencies are generated from the retained ELF files.

The standalone archive's private `libpython3.so` compatibility stub contains
a literal `$ORIGIN` entry in its dynamic-needed list. During the build only,
the recipe changes that entry to the sibling private `libpython3.12.so.1.0`
SONAME and sets the stub's `$ORIGIN` runtime search path. This preserves the
private ABI while allowing `dpkg-shlibdeps` to resolve it without treating
libpython as a system dependency. A package-local `DEBIAN/shlibs` entry is
used only to identify that staged private SONAME during dependency generation;
the generated self-dependency is explicitly excluded.
