#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail
cd "$(dirname "$0")/.."

root=$(pwd -P)
jobs=${JOBS:-$(nproc)}
cross_compile=arm-linux-gnueabi-

usage() {
	echo 'build: u-boot|openwrt|linux' >&2
	exit 2
}

is_prepare_only() {
	[[ ${PREPARE_ONLY:-0} == 1 ]]
}

enter_safe_build_root() {
	local build_id build_mount safe_path status

	[[ $root != *[[:space:]]* ]] && return
	if ! command -v bwrap >/dev/null; then
		echo 'Bubblewrap is unavailable; run the build through Pixi.' >&2
		exit 1
	fi
	build_id=$(printf '%s\0' "$root" | sha256sum | cut -d ' ' -f1)
	build_mount=/tmp/q703-firmware-$(id -u)-$build_id
	if [[ -e $build_mount || -L $build_mount ]] &&
		{ [[ -L $build_mount ]] || [[ ! -d $build_mount ]] || [[ ! -O $build_mount ]]; }
	then
		echo "Unsafe build mount path: $build_mount" >&2
		exit 1
	fi
	install -d -m 700 -- "$build_mount"
	safe_path=${PATH//"$root"/"$build_mount"}
	echo "Checkout path contains whitespace; building through $build_mount." >&2
	if Q703_BUILD_NAMESPACE=1 PATH=$safe_path bwrap \
			--die-with-parent \
			--unshare-user \
			--unshare-pid \
			--dev-bind / / \
			--proc /proc \
			--bind "$root" "$build_mount" \
			--chdir "$build_mount" \
			-- \
			bash scripts/build.sh "$@"
	then
		status=0
	else
		status=$?
	fi
	if ! rmdir -- "$build_mount"; then
		echo "Build mount cleanup failed: $build_mount" >&2
		(( status == 0 )) && status=1
	fi
	exit "$status"
}

require_config() {
	local config=$1 setting=$2
	# -F: settings are literals; a regex would misread Kconfig escapes like \n.
	if ! grep -qxF "$setting" "$config"; then
		echo "Missing required setting in $config: $setting" >&2
		return 1
	fi
}

check_maximum_size() {
	local file=$1 maximum=$2 size
	size=$(stat -c %s "$file")
	if (( size > maximum )); then
		echo "$file is $size bytes; maximum is $maximum bytes." >&2
		return 1
	fi
}

apply_patches() {
	local component=$1 patch patch_hash
	shift
	patch_hash=$(
		for patch; do
			sha256sum "$patch" | cut -d ' ' -f1
		done | sha256sum | cut -d ' ' -f1
	)
	if [[ -f $src/.patches ]]; then
		if [[ $(< "$src/.patches") != "$patch_hash" ]]; then
			echo "Patch marker changed; refreshing generated $component source." >&2
			python3 scripts/sources.py refresh "$component"
			src=$root/work/$component
		else
			return 0
		fi
	fi
	for patch; do
		git -C "$src" apply --check "$patch"
		git -C "$src" apply "$patch"
	done
	printf '%s\n' "$patch_hash" > "$src/.patches"
}

prepare_openwrt_build_root() {
	local marker=$src/.q703-build-root previous stale=0
	local generated=(bin build_dir feeds logs staging_dir tmp)
	local directory

	if [[ -f $marker ]]; then
		previous=$(< "$marker")
		[[ $previous == "$root" ]] || stale=1
	else
		for directory in "${generated[@]}"; do
			[[ ! -e $src/$directory && ! -L $src/$directory ]] || stale=1
		done
	fi
	if (( stale )); then
		echo 'Build root changed; removing relocatable OpenWrt build products.' >&2
		for directory in "${generated[@]}"; do
			rm -rf -- "${src:?}/$directory"
		done
	fi
	printf '%s\n' "$root" > "$marker"
}

install_device_trees() {
	local destination=$1 pair source_name destination_name
	mkdir -p "$destination"
	for pair in \
		ts221-common.dtsi:ts221-common.dtsi \
		q703.dts:kirkwood-q703.dts \
		ts221.dts:kirkwood-ts221.dts
	do
		source_name=${pair%:*}
		destination_name=${pair#*:}
		if ! cmp -s "board/$source_name" "$destination/$destination_name"; then
			cp "board/$source_name" "$destination/$destination_name"
		fi
	done
}

create_release_archive() {
	local directory=$1 artifact=$2
	tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
		-C "$directory" -cf - . | gzip -n > "dist/$artifact.tmp"
	mv "dist/$artifact.tmp" "dist/$artifact"
}

prepare_build() {
	local component=$1 artifact
	python3 scripts/sources.py fetch "$component"
	src=$root/work/$component

	# Keep plain source archives independent of any enclosing Git repository.
	export GIT_CEILING_DIRECTORIES="$root/work"
	export SOURCE_DATE_EPOCH
	SOURCE_DATE_EPOCH=$(python3 -c \
		'import json,sys; print(json.load(open("work/sources.json"))[sys.argv[1]]["epoch"])' \
		"$component")
	export KBUILD_BUILD_USER=builder KBUILD_BUILD_HOST=firmware
	unset CFLAGS CXXFLAGS CPPFLAGS LDFLAGS LIBS CPATH LIBRARY_PATH \
		CC CXX LD AR AS NM OBJCOPY OBJDUMP STRIP RANLIB ARCH CROSS_COMPILE

	mkdir -p dist
	if [[ -f dist/sources.json ]]; then
		if ! cmp work/sources.json dist/sources.json; then
			echo 'Different lock: archive or remove dist/ deliberately before building.' >&2
			return 1
		fi
	else
		cp work/sources.json dist/sources.json
	fi
	case "$component" in
		u-boot) artifacts=(u-boot-q703.kwb u-boot-ts221.kwb) ;;
		openwrt) artifacts=(openwrt-q703.tar.gz openwrt-ts221.tar.gz) ;;
		linux) artifacts=(linux.tar.gz) ;;
	esac
	for artifact in "${artifacts[@]}"; do
		rm -f "dist/$artifact" "dist/$artifact.tmp"
	done
}

activate_cross_compiler() {
	if ! command -v "${cross_compile}gcc" >/dev/null; then
		echo "Cross compiler is unavailable: ${cross_compile}gcc" >&2
		return 1
	fi
	if [[ $("${cross_compile}gcc" -dumpmachine) != arm-linux-gnueabi ]]; then
		echo "Unexpected cross-compiler target: $("${cross_compile}gcc" -dumpmachine)" >&2
		return 1
	fi
	export ARCH=arm CROSS_COMPILE=$cross_compile
}

build_uboot() (
	local model setting ident
	prepare_build u-boot
	apply_patches u-boot "$root"/patches/u-boot/*.patch
	install_device_trees "$src/dts/upstream/src/arm/marvell"
	cp board/boot.env "$src/board/qnap/ts221/ts221.env"
	is_prepare_only && return

	activate_cross_compiler
	for model in q703 ts221; do
		make -C "$src" qnap_ts221_defconfig
		case "$model" in
			q703) ident='Fujitsu CELVIN Q703' ;;
			ts221) ident='QNAP TS-221' ;;
		esac
		# scripts/config passes the value through sed: escape the backslash
		# so .config receives the Kconfig escape \n (version.c adds none).
		"$src/scripts/config" --file "$src/.config" \
			--set-str DEFAULT_DEVICE_TREE "marvell/kirkwood-$model" \
			--set-str IDENT_STRING '\\n'"$ident" \
			--set-val SYS_BOOTM_LEN 0x1000000 \
			--set-val PHY_ANEG_TIMEOUT 15000 \
			--enable CMD_MII \
			--enable CMD_MDIO \
			--enable CMD_PING
		make -C "$src" olddefconfig
		require_config "$src/.config" \
			"CONFIG_DEFAULT_DEVICE_TREE=\"marvell/kirkwood-$model\""
		require_config "$src/.config" "CONFIG_IDENT_STRING=\"\\n$ident\""
		# The recovery initramfs exceeds upstream's 8 MiB bootm default.
		require_config "$src/.config" 'CONFIG_SYS_BOOTM_LEN=0x1000000'
		require_config "$src/.config" 'CONFIG_PHY_ANEG_TIMEOUT=15000'
		make -C "$src" -j"$jobs"
		check_maximum_size "$src/u-boot.kwb" 524288
		for setting in \
			ENV_IS_IN_SPI_FLASH=y ENV_REDUNDANT=y ENV_SIZE=0x1000 \
			ENV_SECT_SIZE=0x10000 ENV_OFFSET=0xe80000 ENV_OFFSET_REDUND=0xe90000 \
			CMD_SAVEENV=y BOOTCOUNT_LIMIT=y BOOTCOUNT_ENV=y HUSH_PARSER=y \
			CMD_IMI=y CMD_BOOTM=y CMD_SATA=y CMD_MII=y CMD_MDIO=y CMD_PING=y
		do
			require_config "$src/.config" "CONFIG_$setting"
		done
		"$src/tools/mkimage" -l "$src/u-boot.kwb"
		cp "$src/.config" "dist/u-boot-$model.config"
		cp "$src/u-boot.kwb" "dist/u-boot-$model.kwb.tmp"
		mv "dist/u-boot-$model.kwb.tmp" "dist/u-boot-$model.kwb"
	done
	printf 'Built dist/u-boot-q703.kwb and dist/u-boot-ts221.kwb\n'
)

build_linux() (
	prepare_build linux
	apply_patches linux "$root/patches/linux/board.patch"
	install_device_trees "$src/arch/arm/boot/dts/marvell"
	is_prepare_only && return

	activate_cross_compiler
	make -C "$src" mvebu_v5_defconfig
	(cd "$src"; scripts/kconfig/merge_config.sh -m .config "$root/board/kernel.config")
	make -C "$src" olddefconfig
	make -C "$src" -j"$jobs" \
		zImage marvell/kirkwood-q703.dtb marvell/kirkwood-ts221.dtb
	mkdir -p work/linux-release
	cp "$src/arch/arm/boot/zImage" work/linux-release/
	cp "$src/arch/arm/boot/dts/marvell/kirkwood-q703.dtb" work/linux-release/q703.dtb
	cp "$src/arch/arm/boot/dts/marvell/kirkwood-ts221.dtb" work/linux-release/ts221.dtb
	cp "$src/.config" work/linux-release/linux.config
	create_release_archive work/linux-release linux.tar.gz
	printf 'Built dist/linux.tar.gz\n'
)

prepare_openwrt_source() {
	local kernel_version config line key
	: "${GITHUB_REPOSITORY:?set OWNER/repository}"
	RELEASE_PUBKEY=${RELEASE_PUBKEY:-$root/keys/release.pub}
	[[ $GITHUB_REPOSITORY =~ ^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$ ]]
	openssl pkey -pubin -in "$RELEASE_PUBKEY" -text -noout | grep -q ED25519

	mkdir -p "$src/files/etc/firmware" "$src/package/ts221"
	cp -a openwrt/files/. "$src/files/"
	chmod 755 "$src/files/usr/sbin/firmware-update" "$src/files/usr/sbin/firmware-install"
	chmod 600 "$src/files/etc/crontabs/root"
	printf '%s\n' "$GITHUB_REPOSITORY" > "$src/files/etc/firmware/repository"
	cp "$RELEASE_PUBKEY" "$src/files/etc/firmware/release.pub"
	cp board/boot.env "$src/files/etc/firmware/boot.env"
	cp -a openwrt/package/ts221/. "$src/package/ts221/"
	install_device_trees "$src/target/linux/kirkwood/files/arch/arm/boot/dts/marvell"

	kernel_version=$(sed -n 's/^KERNEL_PATCHVER:=//p' "$src/target/linux/kirkwood/Makefile")
	if [[ ! $kernel_version =~ ^[0-9]+\.[0-9]+$ ]]; then
		echo "Invalid OpenWrt kernel version: $kernel_version" >&2
		return 1
	fi
	cp patches/openwrt/ts221.patch \
		"$src/target/linux/kirkwood/patches-$kernel_version/119-ts221.patch"
	config=$src/target/linux/kirkwood/config-$kernel_version
	while read -r line; do
		key=${line#\# }
		key=${key%%[= ]*}
		sed -i "/^$key=/d; /^# $key is not set$/d" "$config"
	done < board/kernel.config
	cat board/kernel.config >> "$config"

	cp openwrt/image.mk "$src/target/linux/kirkwood/image/ts221.mk"
	if ! grep -q 'include ./ts221.mk' "$src/target/linux/kirkwood/image/Makefile"; then
		sed -i '/call BuildImage/i include ./ts221.mk' \
			"$src/target/linux/kirkwood/image/Makefile"
	fi
	# These drivers are built in so md0 can be mounted before userspace starts.
	sed -i \
		's/ +kmod-md-mod//g; s/ +kmod-md-raid0//g; s/ +kmod-md-raid1\b//g; s/ +kmod-md-raid10//g' \
		"$src/package/utils/mdadm/Makefile"
	cp openwrt/config "$src/.config"
}

configure_openwrt() {
	(cd "$src"; ./scripts/feeds update -a; ./scripts/feeds install -a
		cp "$root/openwrt/config" .config
		make defconfig)
}

validate_openwrt_config() {
	local device option
	for device in fujitsu_q703 qnap_ts221; do
		require_config "$src/.config" \
			"CONFIG_TARGET_DEVICE_kirkwood_generic_DEVICE_$device=y"
	done
	require_config "$src/.config" '# CONFIG_TARGET_PER_DEVICE_ROOTFS is not set'
	for option in STAT FEATURE_STAT_FORMAT FEATURE_STAT_FILESYSTEM OD FEATURE_TAR_LONG_OPTIONS; do
		require_config "$src/.config" "CONFIG_BUSYBOX_CONFIG_$option=y"
	done
}

validate_openwrt_outputs() {
	local kernel_config=$1 manifest=$2 symbol package
	for symbol in SATA_MV BLK_DEV_MD MD_RAID1 MD_RAID10 EXT4_FS; do
		require_config "$kernel_config" "CONFIG_$symbol=y"
	done
	for package in \
		ts221 mdadm curl ca-bundle openssl-util jsonfilter flock blockdev \
		uboot-envtools mtd e2fsprogs
	do
		if ! grep -q "^$package " "$manifest"; then
			echo "Missing required OpenWrt package: $package" >&2
			return 1
		fi
	done
}

run_openwrt_make() {
	local -a make_options=("$@")

	if make -C "$src" -j"$jobs" "${make_options[@]}"; then
		return 0
	fi
	echo 'Parallel OpenWrt build failed; retrying serially with verbose output.' >&2
	make -C "$src" -j1 V=s "${make_options[@]}"
}

package_openwrt() {
	local bin=$src/bin/targets/kirkwood/generic
	local release=work/openwrt-release
	local model device pair
	mkdir -p "$release"
	cp "$bin"/*-kirkwood-generic-rootfs.tar.gz "$release/rootfs.tar.gz"
	cp "$bin"/*-kirkwood-generic.manifest "$release/packages.manifest"
	cp "$src/.config" "$release/openwrt.config"
	cp "$src"/build_dir/target-*/linux-kirkwood_generic/linux-*/.config \
		"$release/kernel.config"
	validate_openwrt_outputs "$release/kernel.config" "$release/packages.manifest"
	# Plain grep (no -q) so tar is not SIGPIPEd before the match is found;
	# pipefail would otherwise fail the gate on any real-sized archive.
	tar -tzf "$release/rootfs.tar.gz" | grep -Fx './etc/mke2fs.conf' > /dev/null || {
		echo 'Missing required rootfs file: /etc/mke2fs.conf' >&2
		return 1
	}

	for pair in q703:fujitsu_q703 ts221:qnap_ts221; do
		model=${pair%:*}
		device=${pair#*:}
		cp "$src"/build_dir/target-*/linux-kirkwood_generic/"$device"-uImage \
			"$release/kernel.uImage"
		check_maximum_size "$release/kernel.uImage" 16777216
		cp "$bin"/*-"$device"-initramfs-uImage "$release/recovery.uImage"
		create_release_archive "$release" "openwrt-$model.tar.gz"
		check_maximum_size "dist/openwrt-$model.tar.gz" 268435456
	done
	cp "$RELEASE_PUBKEY" dist/release.pub
}

build_openwrt() (
	local namespace_fakeroot
	prepare_build openwrt
	prepare_openwrt_build_root
	prepare_openwrt_source
	is_prepare_only && return
	configure_openwrt
	validate_openwrt_config
	if [[ ${Q703_BUILD_NAMESPACE:-0} == 1 ]]; then
		# Keep source extraction unprivileged, but map fakeroot's caller to
		# namespace root so APK can preserve root-owned payloads.
		namespace_fakeroot="bwrap --die-with-parent --unshare-user "
		namespace_fakeroot+="--uid 0 --gid 0 --dev-bind / / -- "
		namespace_fakeroot+="$src/staging_dir/host/bin/fakeroot"
		run_openwrt_make "FAKEROOT=$namespace_fakeroot"
	else
		run_openwrt_make
	fi
	package_openwrt
	printf 'Built dist/openwrt-q703.tar.gz and dist/openwrt-ts221.tar.gz\n'
)

build_all() {
	build_uboot
	build_openwrt
	build_linux
}

enter_safe_build_root "$@"

if [[ $# == 0 ]]; then
	build_all
else
	case $1 in
		u-boot) build_uboot ;;
		openwrt) build_openwrt ;;
		linux) build_linux ;;
		*) usage ;;
	esac
fi
