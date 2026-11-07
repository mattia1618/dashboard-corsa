#!/usr/bin/env python3
"""
pubblica.py — Aggiorna la dashboard e la mette online, protetta da password.

Lo lancia Windows da solo (Utilità di pianificazione) ogni ora e all'accesso.
Garmin blocca i server di GitHub, quindi i dati li scarica questo PC.

Passi: dashboard.py -> StatiCrypt (password) -> push sul ramo gh-pages.
La password sta in ~/.dashboard-corsa-password, mai nel repository.
Cosa è successo all'ultimo giro: vedi pubblica.log.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
PASSWORD_FILE = os.path.expanduser("~/.dashboard-corsa-password")
REPO_URL = "https://github.com/mattia1618/dashboard-corsa.git"
LOG = os.path.join(DIR, "pubblica.log")
SALT = "3fd544d63d00c2613c8982f5d9cc5f5b"  # fisso: "Ricordami" resta valido tra un aggiornamento e l'altro
PWA_TAGS = (
    '<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icon.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="Corsa"><meta name="theme-color" content="#000000">'
)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # niente finestre nere che lampeggiano


def run(cmd, log, **kw):
    log.write(f"$ {' '.join(cmd[:3])} ...\n")
    log.flush()
    # un passaggio bloccato (es. login GitHub invisibile) non deve fermare i giri dopo
    kw.setdefault("timeout", 15 * 60)
    subprocess.run(cmd, check=True, stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, **kw)


def main():
    with open(LOG, "w", encoding="utf-8") as log:
        log.write(f"== {datetime.now():%d/%m/%Y %H:%M} ==\n")
        try:
            with open(PASSWORD_FILE, encoding="utf-8-sig") as f:
                password = f.read().strip()
        except FileNotFoundError:
            log.write(f"ERRORE: manca il file della password {PASSWORD_FILE}\n")
            return 1
        if len(password) < 14:
            log.write("ERRORE: la password deve avere almeno 14 caratteri\n")
            return 1

        try:
            python = sys.executable.replace("pythonw.exe", "python.exe")
            # -u: il log mostra fin dove arriva
            run([python, "-u", "-X", "utf8", "dashboard.py"], log, cwd=DIR)

            # i file .git sono in sola lettura su Windows: la pulizia può fallire senza danni
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                site = os.path.join(tmp, "site")
                run(["npx.cmd", "--yes", "staticrypt@3.5.4", os.path.join(DIR, "index.html"), "-d", site,
                     "--salt", SALT, "--remember", "60",
                     "--template-title", "Corsa", "--template-instructions", "Inserisci la password della dashboard",
                     "--template-button", "Apri", "--template-placeholder", "Password",
                     "--template-remember", "Ricordami su questo telefono", "--template-error", "Password sbagliata",
                     "--template-color-primary", "#ff6b2c", "--template-color-secondary", "#000000"],
                    log, env={**os.environ, "STATICRYPT_PASSWORD": password})

                page = os.path.join(site, "index.html")
                with open(page, encoding="utf-8") as f:
                    html = f.read()
                if "GENERATED_ISO" in html:  # sicurezza: codice della dashboard in chiaro = non cifrata
                    raise RuntimeError("la pagina non risulta cifrata, non la pubblico")
                with open(page, "w", encoding="utf-8") as f:
                    f.write(re.sub(r"</head>", PWA_TAGS + "</head>", html, count=1))
                for name in ("manifest.json", "icon.png"):
                    shutil.copy(os.path.join(DIR, "app", name), site)
                shutil.copy(os.path.join(DIR, "version.txt"), site)  # la pagina lo controlla per ricaricarsi

                # ramo gh-pages "usa e getta": ogni volta un solo commit, niente storico che cresce
                git = ["git", "-C", site, "-c", "user.name=Mattia", "-c", "user.email=mattia.spazzatura05@gmail.com"]
                run(git[:3] + ["init", "-q", "-b", "gh-pages"], log)
                run(git + ["add", "-A"], log)
                run(git + ["commit", "-q", "-m", f"Dati del {datetime.now():%d/%m %H:%M}"], log)
                run(git[:3] + ["push", "-f", "-q", REPO_URL, "gh-pages"], log)
        except Exception as err:
            log.write(f"ERRORE: {err}\n")
            return 1
        log.write("OK: pagina pubblicata\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
