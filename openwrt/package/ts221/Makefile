# SPDX-License-Identifier: GPL-2.0-or-later
include $(TOPDIR)/rules.mk

PKG_NAME:=ts221
PKG_VERSION:=0.5.7
PKG_RELEASE:=1
PKG_SOURCE:=qcontrol_$(PKG_VERSION).orig.tar.gz
PKG_SOURCE_URL:=https://deb.debian.org/debian/pool/main/q/qcontrol
PKG_HASH:=ecc6977266531571e724146fe18478a3a4a168a8a766c47439c4a63b3f73aa3f
PKG_BUILD_DIR:=$(BUILD_DIR)/qcontrol-$(PKG_VERSION)
PKG_BUILD_DEPENDS:=lua
PKG_LICENSE:=GPL-2.0-or-later
PKG_LICENSE_FILES:=COPYING

include $(INCLUDE_DIR)/package.mk

define Package/ts221
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=TS-221/Q703 fan, buttons and LEDs
  DEPENDS:=@TARGET_kirkwood +libpthread +smartmontools
endef

define Build/Compile
	$(MAKE) -C $(PKG_BUILD_DIR) qcontrol-static \
		CC="$(TARGET_CC)" CFLAGS="-c $(TARGET_CFLAGS) -I$(STAGING_DIR)/usr/include" \
		LUA_LIBDIR="$(STAGING_DIR)/usr/lib" LUA_LIB="liblua.a" \
		LDFLAGS="$(TARGET_LDFLAGS)"
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra \
		-o $(PKG_BUILD_DIR)/q703-pic src/q703-pic.c
endef

define Package/ts221/install
	$(INSTALL_DIR) $(1)/usr/sbin $(1)/etc/init.d $(1)/etc/uci-defaults
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/qcontrol-static $(1)/usr/sbin/qcontrol
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/q703-pic $(1)/usr/sbin/
	$(INSTALL_BIN) files/q703-leds $(1)/usr/sbin/
	$(INSTALL_BIN) files/ts221.init $(1)/etc/init.d/ts221
	$(INSTALL_BIN) files/defaults $(1)/etc/uci-defaults/ts221
	$(INSTALL_CONF) files/qcontrol.conf $(1)/etc/qcontrol.conf
endef

$(eval $(call BuildPackage,ts221))
