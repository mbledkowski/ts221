# SPDX-License-Identifier: GPL-2.0-or-later
# shellcheck shell=sh
root=${FIRMWARE_ROOT:-}
config=$root/etc/firmware

identify_board() {
	compatible=$(tr '\000' '\n' < "$root/proc/device-tree/compatible" | head -n 1)
	case "$compatible" in
		fujitsu,q703) board=q703 ;;
		qnap,ts221) board=ts221 ;;
		*) echo 'Unsupported board' >&2; return 1 ;;
	esac
}

field() { jsonfilter -i "$manifest" -e "$1"; }

verify_manifest() {
	identify_board || return 1
	manifest=$1/manifest.json
	test "$(wc -c < "$manifest")" -le 65536 || return 1
	test "$(wc -c < "$1/manifest.sig")" -eq 64 || return 1
	openssl pkeyutl -verify -pubin -inkey "$config/release.pub" -rawin \
		-in "$manifest" -sigfile "$1/manifest.sig" >/dev/null || return 1
	repo=$(cat "$config/repository")
	printf '%s\n' "$repo" | grep -Eq '^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$' || return 1
	test "$(field '@.schema')" = 2 || return 1
	test "$(field '@.layout')" = 1 || return 1
	test "$(field '@.board')" = "$compatible" || return 1
	test "$(field '@.repository')" = "$repo" || return 1
	tag=$(field '@.tag')
	sequence=$(field '@.sequence')
	printf '%s\n' "$tag" | grep -Eq '^v[0-9][A-Za-z0-9._-]{0,63}$' || return 1
	printf '%s\n' "$sequence" | grep -Eq '^[1-9][0-9]{0,9}$'
}

file_info() {
	case "$1" in
		openwrt-"$board".tar.gz) limit=268435456 ;;
		u-boot-"$board".kwb) limit=524288 ;;
		*) return 1 ;;
	esac
	size=$(field "@.files['$1'].size")
	hash=$(field "@.files['$1'].sha256")
	printf '%s\n' "$size" | grep -Eq '^[1-9][0-9]{0,8}$' || return 1
	printf '%s\n' "$hash" | grep -Eq '^[0-9a-f]{64}$' || return 1
	test "$size" -le "$limit"
}

verify_file() {
	file_info "$1" || return 1
	test "$(wc -c < "${manifest%/*}/$1")" -eq "$size" || return 1
	printf '%s  %s\n' "$hash" "${manifest%/*}/$1" | sha256sum -c - >/dev/null
}
