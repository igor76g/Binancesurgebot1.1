"""Окно приложения SurgeBot: запускает фоновую службу и показывает интерфейс бота (WebView или браузер)."""
import os
import sys
import time
import urllib.request

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label

URL = "http://127.0.0.1:8765"
PKG = "org.surgebot.surgebot"


def err_files():
    base = os.environ.get("ANDROID_PRIVATE", ".")
    out = []
    for name in ("crash.txt", "service_error.txt"):
        try:
            t = open(os.path.join(base, name), encoding="utf-8").read().strip()
            if t:
                out.append(f"[{name}]\n{t[-700:]}")
        except OSError:
            pass
    return "\n\n".join(out)


def on_android():
    return "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")


class Root(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", padding=24, spacing=16, **kw)
        prev = err_files()
        self.info = Label(text=("ОШИБКА ПРОШЛОГО ЗАПУСКА:\n" + prev) if prev else "SurgeBot\nЗапуск службы...",
                          font_size="12sp" if prev else "18sp", halign="left" if prev else "center", valign="middle")
        self.info.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self.info)
        for text, cb in (("Открыть интерфейс (в браузере)", self.open_browser),
                         ("Остановить службу и выйти", self.stop_all)):
            self.add_widget(Button(text=text, size_hint_y=None, height=64, on_release=cb))
        self.webview = None
        self.service = None


class SurgeApp(App):
    def build(self):
        self.root_w = Root()
        Window.bind(on_keyboard=self.on_key)
        Clock.schedule_once(self.boot, 0.5)
        Clock.schedule_interval(self.check, 3)
        return self.root_w

    # ---- служба ----
    def boot(self, dt):
        if not on_android():
            self.root_w.info.text = "SurgeBot\nЗапустите web_app.py на компьютере"
            return
        try:
            from android.permissions import request_permissions, Permission
            request_permissions([Permission.POST_NOTIFICATIONS] if hasattr(Permission, "POST_NOTIFICATIONS") else [])
        except Exception:
            pass
        try:
            from jnius import autoclass
            service = autoclass(PKG + ".ServiceBot")
            mActivity = autoclass("org.kivy.android.PythonActivity").mActivity
            service.start(mActivity, "")
            self.root_w.service = service
        except Exception as e:
            self.root_w.info.text = f"Не удалось запустить службу:\n{e}"
            self.root_w.keep_text = True

    def check(self, dt):
        if getattr(self.root_w, "keep_text", False) or err_files():
            return
        try:
            urllib.request.urlopen(URL + "/state", timeout=1).read()
            self.root_w.info.text = "SurgeBot\nСлужба работает\n(бот живёт в фоне, пока видно уведомление)"
        except Exception:
            if on_android():
                self.root_w.info.text = "SurgeBot\nЖдём запуск службы..."

    # ---- интерфейс ----
    def open_browser(self, *a):
        if not on_android():
            import webbrowser
            webbrowser.open(URL)
            return
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(URL)))

    def open_webview(self, *a):
        if not on_android():
            return self.open_browser()
        from android.runnable import run_on_ui_thread
        from jnius import autoclass

        @run_on_ui_thread
        def create():
            if self.root_w.webview is not None:
                self.root_w.webview.setVisibility(0)
                return
            WebView = autoclass("android.webkit.WebView")
            Client = autoclass("android.webkit.WebViewClient")
            LP = autoclass("android.view.ViewGroup$LayoutParams")
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            wv = WebView(act)
            wv.getSettings().setJavaScriptEnabled(True)
            wv.getSettings().setDomStorageEnabled(True)
            wv.setWebViewClient(Client())
            wv.loadUrl(URL)
            act.addContentView(wv, LP(-1, -1))
            self.root_w.webview = wv
        create()

    def on_key(self, window, key, *a):
        if key == 27 and self.root_w.webview is not None:      # кнопка «назад» закрывает WebView
            from android.runnable import run_on_ui_thread

            @run_on_ui_thread
            def hide():
                self.root_w.webview.setVisibility(8)
            hide()
            return True
        return False

    def stop_all(self, *a):
        if on_android():
            try:
                from jnius import autoclass
                autoclass(PKG + ".ServiceBot").stop(autoclass("org.kivy.android.PythonActivity").mActivity)
            except Exception:
                pass
        self.stop()

    def on_pause(self):
        return True


if __name__ == "__main__":
    try:
        SurgeApp().run()
    except BaseException:
        import traceback
        try:
            with open(os.path.join(os.environ.get("ANDROID_PRIVATE", "."), "crash.txt"), "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise
