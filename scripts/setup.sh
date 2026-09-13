#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail
cd "$(dirname "$0")/.."

: "${ARM_TOOLCHAIN_URL:?run through Pixi}" \
	"${ARM_TOOLCHAIN_SHA256:?run through Pixi}" \
	"${ARM_TOOLCHAIN_ARCHIVE:?run through Pixi}" \
	"${ARM_TOOLCHAIN_DIR:?run through Pixi}"
archive=$ARM_TOOLCHAIN_ARCHIVE
toolchain=$ARM_TOOLCHAIN_DIR
compiler="$toolchain/arm-linux-gnueabi/bin/arm-linux-gnueabi-gcc"
partial="$archive.tmp"

if [[ -x $compiler ]] && [[ $("$compiler" -dumpmachine) == arm-linux-gnueabi ]]; then
	exit
fi
if [[ -e $toolchain ]]; then
	echo "Remove incomplete cross compiler: $toolchain" >&2
	exit 1
fi
mkdir -p "$(dirname "$archive")" "$(dirname "$toolchain")"
trap 'rm -f "$partial"' EXIT
if [[ ! -f $archive ]]; then
	curl --fail --location --retry 3 --output "$partial" "$ARM_TOOLCHAIN_URL"
	printf '%s  %s\n' "$ARM_TOOLCHAIN_SHA256" "$partial" | sha256sum -c -
	mv "$partial" "$archive"
fi
printf '%s  %s\n' "$ARM_TOOLCHAIN_SHA256" "$archive" | sha256sum -c -
temporary=$(mktemp -d "$(dirname "$toolchain")/.cross.XXXXXX")
trap 'rm -rf "$temporary" "$partial"' EXIT
tar -C "$temporary" --strip-components=1 -xf "$archive"
candidate="$temporary/arm-linux-gnueabi/bin/arm-linux-gnueabi-gcc"
[[ -x $candidate ]] && [[ $("$candidate" -dumpmachine) == arm-linux-gnueabi ]]
mv -T "$temporary" "$toolchain"
trap - EXIT
