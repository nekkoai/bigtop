#!/bin/sh
# Assemble the private Capsa CPython runtime in a Noble amd64 builder.
# Inputs are downloaded only here, checked against the recipe's immutable
# records, and cached below build/capsa-inputs for transfer to later builders.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
recipe=$here/pkg/capsa/python-runtime
inputs=$recipe/runtime-inputs.json
wheels=$recipe/wheel-inputs.json
runtime_lock=$recipe/runtime-production.hashed.lock
bootstrap_lock=$recipe/bootstrap.hashed.lock
cache=$here/build/capsa-inputs/capsa-python-runtime
work=$here/build/capsa-runtime

fail() {
    echo "build-capsa-runtime: $*" >&2
    exit 1
}

[ -r /etc/os-release ] || fail "missing /etc/os-release"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] ||
    fail "only Ubuntu 24.04 builders are supported"
[ "$(dpkg --print-architecture)" = amd64 ] || fail "only amd64 builders are supported"
for tool in curl dpkg dpkg-deb dpkg-parsechangelog dpkg-shlibdeps fakeroot find \
    patchelf readelf sha256sum tar python3; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing required build tool: $tool"
done
for file in "$inputs" "$wheels" "$runtime_lock" "$bootstrap_lock" \
    "$recipe/debian/control.in" "$recipe/debian/copyright" \
    "$here/pkg/capsa/upgrade-guard"; do
    [ -f "$file" ] || fail "missing recipe input: $file"
done

# Companion package versions deliberately derive from the owning Capsa source
# changelog instead of maintaining another release record.
debversion=$(dpkg-parsechangelog -l "$here/pkg/capsa/debian/changelog" -S Version)
version=${debversion%-*}
[ "$version" != "$debversion" ] || fail "Capsa changelog version lacks a Debian revision"

# The static checker also makes the deliberate 29-package production subset
# and its source-record hashes explicit before a network request is made.
python3 "$recipe/tests/validate-inputs.py"

umask 022
mkdir -p "$cache" "$work"
downloads=$work/downloads.tsv
python3 - "$inputs" "$wheels" > "$downloads" <<'PY'
import json
import sys

inputs = json.load(open(sys.argv[1], encoding="utf-8"))
wheels = json.load(open(sys.argv[2], encoding="utf-8"))
artifacts = [inputs["python_archive"], *wheels]
seen = set()
for artifact in artifacts:
    filename = artifact["filename"]
    if filename in seen:
        raise SystemExit(f"duplicate input filename: {filename}")
    seen.add(filename)
    print("\t".join((filename, artifact["sha256"], artifact["url"])))
PY

fetch_checked() {
    filename=$1
    expected=$2
    url=$3
    destination=$cache/$filename
    if [ -f "$destination" ]; then
        actual=$(sha256sum "$destination" | awk '{print $1}')
        if [ "$actual" = "$expected" ]; then
            return
        fi
        echo "build-capsa-runtime: replacing bad cached input $filename" >&2
        rm -f "$destination"
    fi
    temporary=$destination.tmp.$$
    rm -f "$temporary"
    # No package-install path calls this: all network access is limited to this
    # builder driver, before a .deb exists.
    curl --fail --location --retry 3 --connect-timeout 30 --output "$temporary" "$url" || {
        rm -f "$temporary"
        fail "could not fetch $filename"
    }
    actual=$(sha256sum "$temporary" | awk '{print $1}')
    [ "$actual" = "$expected" ] || {
        rm -f "$temporary"
        fail "SHA-256 mismatch for $filename"
    }
    mv "$temporary" "$destination"
}

tab=$(printf '\t')
while IFS="$tab" read -r filename expected url; do
    [ -n "$filename" ] || continue
    fetch_checked "$filename" "$expected" "$url"
done < "$downloads"

python_archive=$(python3 - "$inputs" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["python_archive"]["filename"])
PY
)
pip_wheel=$(python3 - "$wheels" <<'PY'
import json
import sys
for wheel in json.load(open(sys.argv[1], encoding="utf-8")):
    if wheel["name"].lower() == "pip":
        print(wheel["filename"])
        break
else:
    raise SystemExit("pip bootstrap wheel is missing")
PY
)
[ -f "$cache/$python_archive" ] || fail "missing verified Python archive"
[ -f "$cache/$pip_wheel" ] || fail "missing verified pip wheel"

rm -rf "$work/extract" "$work/debian" "$work/wheelhouse" "$work/empty-home"
stage=$work/debian/capsa-python-runtime
mkdir -p "$work/extract" "$stage/usr/lib/capsa" "$work/wheelhouse" "$work/empty-home"
tar -C "$work/extract" -xzf "$cache/$python_archive"
[ -x "$work/extract/python/bin/python3.12" ] ||
    fail "standalone archive did not contain python/bin/python3.12"
private=$stage/usr/lib/capsa/python
mv "$work/extract/python" "$private"
python=$private/bin/python3.12
[ "$($python --version 2>&1)" = "Python 3.12.15" ] ||
    fail "standalone archive did not provide CPython 3.12.15"

# The standalone archive's libpython3.so compatibility stub has a literal
# $ORIGIN path in DT_NEEDED. That is accepted by its original launcher but is
# not a library name dpkg-shlibdeps can resolve. Keep libpython private while
# changing the stub to the normal private SONAME and a sibling-library RUNPATH.
libpython_stub=$private/lib/libpython3.so
[ -f "$libpython_stub" ] || fail "standalone archive lacks libpython3.so"
[ -f "$private/lib/libpython3.12.so.1.0" ] ||
    fail "standalone archive lacks private libpython3.12.so.1.0"
readelf -d "$private/lib/libpython3.12.so.1.0" |
    grep -F 'Library soname: [libpython3.12.so.1.0]' >/dev/null ||
    fail "private libpython3.12 SONAME changed unexpectedly"
patchelf --replace-needed '$ORIGIN/../lib/libpython3.12.so.1.0' \
    libpython3.12.so.1.0 "$libpython_stub"
patchelf --set-rpath '$ORIGIN' "$libpython_stub"
readelf -d "$libpython_stub" | grep -F 'Shared library: [libpython3.12.so.1.0]' >/dev/null ||
    fail "libpython3.so private dependency patch did not apply"
if readelf -d "$libpython_stub" | grep -F '$ORIGIN/../lib/libpython3.12.so.1.0' >/dev/null; then
    fail "libpython3.so retained a literal DT_NEEDED path"
fi

# The original retained bundle carried copies of these Ubuntu libraries.  This
# companion instead relies on Noble's package-managed ABI and lets
# dpkg-shlibdeps derive the exact resulting dependency constraints from the
# staged ELF files. libpython remains intentionally private.
rm -f "$private/lib"/libcrypt.so.1 "$private/lib"/libcrypt.so.1.* \
    "$private/lib"/libgcc_s.so.1 "$private/lib"/libgcc_s.so.1.*
# Federation has no GUI interface. Drop the optional Tcl/Tk extension and its
# private libraries before dependency analysis, avoiding a spurious GUI/X11
# dependency closure while retaining the headless CPython runtime.
rm -rf "$private/lib/python3.12/tkinter" "$private/lib/python3.12/idlelib" \
    "$private/lib/python3.12/turtledemo"
rm -f "$private/lib/python3.12/turtle.py" \
    "$private/lib/python3.12/lib-dynload"/_tkinter*.so \
    "$private/lib"/libtcl*.so* "$private/lib"/libtk*.so*
rm -rf "$private/lib"/itcl* "$private/lib"/thread*

# A verified pip wheel is made importable only for this command. --target avoids
# creating a relocatable venv; --no-index, --no-deps and --require-hashes make
# the already complete production lock the sole installation input.
while IFS="$tab" read -r filename expected url; do
    case "$filename" in
        *.whl) ln -s "$cache/$filename" "$work/wheelhouse/$filename" ;;
    esac
done < "$downloads"
site=$private/lib/python3.12/site-packages
mkdir -p "$site"
HOME=$work/empty-home \
PIP_CONFIG_FILE=/dev/null \
PIP_DISABLE_PIP_VERSION_CHECK=1 \
PYTHONNOUSERSITE=1 \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=$cache/$pip_wheel \
"$python" -m pip --isolated install \
    --no-index --find-links "$work/wheelhouse" --require-hashes --no-deps \
    --no-compile --target "$site" -r "$runtime_lock"

# pip and its bootstrap wheel are build tools, never runtime payload. Remove
# any copy already present in a standalone archive too, including its launcher.
rm -rf "$site"/pip "$site"/pip-*.dist-info "$private/lib/python3.12/ensurepip"
rm -f "$private/bin"/pip "$private/bin"/pip3 "$private/bin"/pip3.12
if [ -d "$site/bin" ]; then
    for script in "$site/bin"/*; do
        [ -e "$script" ] || continue
        launcher=$private/bin/$(basename "$script")
        mv "$script" "$launcher"
        # Only pip-generated Python console launchers retain the staging
        # interpreter path. Leave any shell/native helper untouched.
        case "$(head -n 1 "$launcher" 2>/dev/null || true)" in
            '#!'*python*) sed -i '1c#!/usr/lib/capsa/python/bin/python3.12' "$launcher" ;;
        esac
    done
    rmdir "$site/bin"
fi

# Confirm that the final interpreter discovers exactly the production closure
# without PYTHONPATH, pip, a user site, or a virtual environment.
env -i HOME="$work/empty-home" PATH=/usr/bin:/bin \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    "$python" - "$runtime_lock" <<'PY'
import importlib.metadata
import sys

expected = {}
def canonical(name):
    import re
    return re.sub(r"[-_.]+", "-", name).lower()

for line in open(sys.argv[1], encoding="utf-8"):
    if not line or line.startswith("#"):
        continue
    requirement = line.split(" --hash=", 1)[0]
    name, version = requirement.split("==", 1)
    expected[canonical(name)] = version
installed = {
    canonical(distribution.metadata["Name"]): distribution.version
    for distribution in importlib.metadata.distributions()
}
if {name: installed.get(name) for name in expected} != expected:
    raise SystemExit("installed distributions do not match the production lock")
for forbidden in ("pip", "packaging", "setuptools", "wheel", "pytest", "pytest-asyncio"):
    if forbidden in installed:
        raise SystemExit(f"build-only or test distribution shipped: {forbidden}")
PY
if find "$stage" -name pyvenv.cfg -print -quit | grep -q .; then
    fail "a virtual environment leaked into the package"
fi
# This runs after pip-generated Python console shebangs have been rewritten;
# wheel RECORD entries are relative paths, so a remaining match is a real
# build-path leak. Existing private interpreter helper scripts are preserved.
if grep -R -I -F "$work" "$stage/usr" >/dev/null 2>&1; then
    fail "a builder path leaked into the runtime payload"
fi

# Retain license payloads actually available in the standalone archive and
# wheels. The supplied metadata records are useful attribution input but do not
# pretend to provide omitted upstream license files.
doc=$stage/usr/share/doc/capsa-python-runtime
mkdir -p "$doc/wheels"
install -m 0644 "$recipe/debian/copyright" "$doc/copyright"
if [ -d "$private/share/licenses" ]; then
    mkdir -p "$doc/python-build-standalone"
    cp -a "$private/share/licenses/." "$doc/python-build-standalone/"
fi
if [ -f "$private/lib/python3.12/LICENSE.txt" ]; then
    install -m 0644 "$private/lib/python3.12/LICENSE.txt" "$doc/CPython-LICENSE.txt"
fi
(
    cd "$site"
    find . -type f \( -path '*.dist-info/licenses/*' -o -iname 'LICENSE*' \
        -o -iname 'LICENCE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) \
        -exec cp --parents -t "$doc/wheels" {} +
)
install -m 0644 "$recipe/README.md" "$doc/README.Debian"

# Feed every retained, non-GUI staged ELF to dpkg-shlibdeps. The scoped Debian
# control file and package build directory let it identify this binary package.
# The explicit private libpython search directory prevents it being mistaken
# for a missing system library; only actual system library needs are emitted.
elfs=$work/elfs.nul
mkdir -p "$stage/DEBIAN"
cat > "$stage/DEBIAN/control" <<EOF
Package: capsa-python-runtime
Version: $debversion
Architecture: amd64
Maintainer: Roman <roman@nekko.ai>
Description: private CPython runtime for Capsa federation components
EOF
# This private library is a package-local provider, not a system Python ABI.
# Its metadata lets dpkg-shlibdeps resolve the staged SONAME; -x below removes
# the resulting self-dependency from the generated runtime dependency field.
printf 'libpython3.12 1.0 capsa-python-runtime (= %s)\n' "$debversion" \
    > "$stage/DEBIAN/shlibs"
cat > "$work/debian/control" <<'EOF'
Source: capsa-python-runtime

Package: capsa-python-runtime
Architecture: amd64
Description: private CPython runtime for Capsa federation components
EOF
(
    cd "$work"
    find debian/capsa-python-runtime/usr -type f -exec sh -c '
        for path do
            if readelf -h "$path" >/dev/null 2>&1; then
                printf "%s\\0" "$path"
            fi
        done
    ' sh {} +
) > "$elfs"
[ -s "$elfs" ] || fail "no ELF files found in the private runtime"
if ! shlib_output=$(cd "$work" && xargs -0 dpkg-shlibdeps -O \
    -l"debian/capsa-python-runtime/usr/lib/capsa/python/lib" \
    -xcapsa-python-runtime \
    < "$elfs" 2>"$work/dpkg-shlibdeps.stderr"); then
    cat "$work/dpkg-shlibdeps.stderr" >&2
    fail "dpkg-shlibdeps failed"
fi
case "$shlib_output" in
    shlibs:Depends=*) shlibdeps=${shlib_output#shlibs:Depends=} ;;
    *) fail "unexpected dpkg-shlibdeps output: $shlib_output" ;;
esac
[ -n "$shlibdeps" ] || fail "dpkg-shlibdeps found no system dependencies"
case "$shlibdeps" in *libcrypt1*) ;; *) fail "staged ELFs did not resolve libcrypt1" ;; esac
case "$shlibdeps" in *libgcc-s1*) ;; *) fail "staged ELFs did not resolve libgcc-s1" ;; esac
case "$shlibdeps" in *libpython*) fail "private libpython became a package dependency" ;; esac

install -m 0755 "$here/pkg/capsa/upgrade-guard" "$stage/DEBIAN/preinst"
python3 - "$recipe/debian/control.in" "$stage/DEBIAN/control" \
    "$debversion" "$shlibdeps" <<'PY'
import re
import sys
source, destination, version, shlibdeps = sys.argv[1:]
control = open(source, encoding="utf-8").read()
control = control.replace("@VERSION@", version).replace("@SHLIBDEPS@", shlibdeps)
if re.search(r"@[A-Z_]+@", control):
    raise SystemExit("unexpanded control template placeholder")
open(destination, "w", encoding="utf-8").write(control)
PY
size=$(du -sk "$stage/usr" | awk '{print $1}')
printf 'Installed-Size: %s\n' "$size" >> "$stage/DEBIAN/control"
(
    cd "$stage"
    find usr -type f -exec md5sum {} + > DEBIAN/md5sums
)

mkdir -p "$work"
deb=$work/capsa-python-runtime_${debversion}_amd64.deb
rm -f "$deb"
fakeroot dpkg-deb -Zzstd -z0 --build "$stage" "$deb" >/dev/null 2>&1 ||
    fakeroot dpkg-deb -Znone --build "$stage" "$deb"
dpkg-deb -I "$deb" | grep -F "Version: $debversion" >/dev/null ||
    fail "generated package version does not match the Capsa changelog"

# The final .deb is in the durable task-owned build output; inputs remain in
# build/capsa-inputs so an isolated main builder can transfer and reuse them.
echo "capsa-python-runtime $debversion: $shlibdeps"
ls -l "$work"/*.deb
