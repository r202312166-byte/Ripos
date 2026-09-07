# -*- coding: utf-8 -*-
import sys, pathlib
class _Kern:
    def __init__(self): self.log = []
    def write(self, s): self.log.append(s)
sys.modules["kern"] = _Kern()
sys.path.insert(0, str(pathlib.Path("os/kernel/initramfs_extra").resolve()))
import i18n, settings, appbar
# default language: en
i18n.set_language("en")
print("en appbar.apps:", i18n.tr("appbar.apps"))
print("en appbar.running:", i18n.tr("appbar.running"))
# switch to zh through the same path the WM Ctrl+L uses
i18n.set_language("zh")
print("settings language now:", settings.get("language"))
print("zh appbar.apps:", i18n.tr("appbar.apps"))
print("zh appbar.running:", i18n.tr("appbar.running"))
print("zh fm.up:", i18n.tr("fm.up"))
print("zh ed.hint:", i18n.tr("ed.hint"))
# sync_from_settings must keep zh (settings now says zh)
i18n.sync_from_settings()
print("after sync current:", i18n.current)
# and back to en persists too
i18n.set_language("en")
print("settings language after en:", settings.get("language"))