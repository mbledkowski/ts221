#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Qualify the locked host environment before starting an upstream build.
set -euo pipefail
cd "$(dirname "$0")/.."

cross_compile=arm-linux-gnueabi-
temporary=$(mktemp -d work/.prepare.XXXXXX)
trap 'rm -rf "$temporary"' EXIT

check_native_toolchain() {
	local -a native_flags cpp_flags
	cat > "$temporary/native.c" <<'EOF'
#include <ncurses.h>
#include <openssl/evp.h>
#include <zlib.h>
int main(void) {
	return !(EVP_sha256() && zlibVersion() && initscr);
}
EOF
	read -r -a native_flags <<< "$(pkg-config --cflags --libs ncursesw openssl zlib)"
	gcc -Werror "$temporary/native.c" "${native_flags[@]}" -o "$temporary/native-c"

	cat > "$temporary/native.cc" <<'EOF'
#include <string>
#include <zlib.h>
int main() { return std::string(zlibVersion()).empty(); }
EOF
	read -r -a cpp_flags <<< "$(pkg-config --cflags --libs zlib)"
	g++ -Werror "$temporary/native.cc" "${cpp_flags[@]}" -o "$temporary/native-cpp"
}

check_python_toolchain() {
	local -a python_includes python_ldflags
	python3 -c 'import elftools, setuptools'
	command -v make swig git >/dev/null

	cat > "$temporary/python.c" <<'EOF'
#include <Python.h>
int main(void) { Py_Initialize(); Py_Finalize(); return 0; }
EOF
	read -r -a python_includes <<< "$(python3-config --includes)"
	read -r -a python_ldflags <<< "$(python3-config --embed --ldflags)"
	gcc -Werror "$temporary/python.c" "${python_includes[@]}" \
		"${python_ldflags[@]}" -o "$temporary/python-host"
}

check_arm_toolchain() {
	local compiler_target
	command -v "${cross_compile}gcc" >/dev/null || {
		echo "Cross compiler is unavailable: ${cross_compile}gcc" >&2
		return 1
	}
	compiler_target=$("${cross_compile}gcc" -dumpmachine)
	if [[ $compiler_target != arm-linux-gnueabi ]]; then
		echo "Unexpected cross-compiler target: $compiler_target" >&2
		return 1
	fi

	printf 'int main(void) { return 0; }\n' | \
		"${cross_compile}gcc" -march=armv5te -nostdlib -Wl,-e,main \
		-x c -o "$temporary/armv5.elf" -
	"${cross_compile}readelf" -h "$temporary/armv5.elf" | \
		grep -Eq '^[[:space:]]*Class:[[:space:]]+ELF32$'
	"${cross_compile}readelf" -h "$temporary/armv5.elf" | \
		grep -Eq '^[[:space:]]*Machine:[[:space:]]+ARM$'
	"${cross_compile}readelf" -h "$temporary/armv5.elf" | \
		grep -Eq '^[[:space:]]*Flags:.*Version5 EABI.*soft-float ABI$'
	"${cross_compile}readelf" -A "$temporary/armv5.elf" | \
		grep -Fq 'Tag_CPU_arch: v5TE'
}

check_native_toolchain
check_python_toolchain
check_arm_toolchain
