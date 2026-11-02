"""Фоновая служба Android: держит бота и локальный веб-сервер (127.0.0.1:8765), пока работает уведомление."""
import os
import sys

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_app


def main():
    ctl = web_app.Controller()
    srv = web_app.serve(ctl)
    ctl.log("Служба запущена")
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        try:
            with open(os.path.join(os.environ.get("ANDROID_PRIVATE", "."), "service_error.txt"), "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise
