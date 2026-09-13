# SPDX-License-Identifier: GPL-2.0-only
KERNEL_LOADADDR := 0x02000000

define Device/nas
  KERNEL_IN_UBI :=
  KERNEL_INITRAMFS := $$(KERNEL)
  IMAGES := kernel.bin
  IMAGE/kernel.bin := append-kernel
endef

define Device/fujitsu_q703
  $(call Device/nas)
  DEVICE_VENDOR := Fujitsu
  DEVICE_MODEL := CELVIN Q703
  DEVICE_DTS := kirkwood-q703
endef
TARGET_DEVICES += fujitsu_q703

define Device/qnap_ts221
  $(call Device/nas)
  DEVICE_VENDOR := QNAP
  DEVICE_MODEL := TS-221
  DEVICE_DTS := kirkwood-ts221
endef
TARGET_DEVICES += qnap_ts221
