[app]
title = SurgeBot
package.name = surgebot
package.domain = org.surgebot
source.dir = .
source.include_exts = py,html
source.exclude_dirs = .github,bin,.buildozer,android_extra,tests
version = 1.0.0
requirements = hostpython3==3.11.5,python3==3.11.5,kivy,pyjnius,android,openssl,certifi
orientation = portrait
fullscreen = 0
services = Bot:service.py:foreground:sticky:foregroundServiceType=dataSync
android.permissions = INTERNET,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,WAKE_LOCK,POST_NOTIFICATIONS
android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.extra_manifest_application_arguments = ./android_extra/app_args.xml
android.allow_backup = False
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 1
