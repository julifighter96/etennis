#!/usr/bin/env python3
"""
eTennis Buchung: Mittwoch 16:00-18:00 und 18:00-20:00 Uhr, Platz 2 (Padel P2 /
Gruener Daumen Court). Woechentlich automatisiert ausfuehren (z.B. per
Scheduler), um beide Mittwoch-Slots zu reservieren.
"""

import requests
import time
import logging
import sys
import os
from datetime import datetime, timedelta
import re
from urllib.parse import urljoin
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# KONFIGURATION
# ──────────────────────────────────────────────
CONFIG = {
    # URL-Layout:
    # - "site_root": nur Schema+Host (keine Pfade, keine Query)
    # - "club_id": entspricht dem "c=...." aus der Reservierungs-URL
    "site_root":      os.getenv("ETENNIS_SITE_ROOT", "https://b.tennisaue.de"),
    "club_id":        os.getenv("ETENNIS_CLUB_ID", "1551"),

    "username":       os.getenv("ETENNIS_USERNAME", ""),
    "password":       os.getenv("ETENNIS_PASSWORD", ""),

    "court_id":       "4857",       # Platz 2 (Padel P2 / Gruener Daumen Court)
    "user_id":        "284273",
    "co_players":     ["369927"],   # Franke Paul

    "slots":          [(16, 18), (18, 20)],  # (start_hour, end_hour) Paare
    "target_weekday": 2,            # Mittwoch (0=Mo ... 6=So), nur fuer Plausibilitaets-Check
    "lead_days":      8,            # Buchungsfenster oeffnet exakt 8 Tage im Voraus

    "max_retries":    5,
    "retry_interval": 5,
}

# ──────────────────────────────────────────────
# LOGGING (Windows-kompatibel, keine Emojis)
# ──────────────────────────────────────────────
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(fmt)

file_handler = logging.FileHandler("etennis_mittwoch.log", encoding="utf-8")
file_handler.setFormatter(fmt)

logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# URL helpers
# ──────────────────────────────────────────────
def url(path: str) -> str:
    # urljoin handles missing/extra slashes safely
    return urljoin(CONFIG["site_root"].rstrip("/") + "/", path.lstrip("/"))


# ──────────────────────────────────────────────
# TIMESTAMPS
# ──────────────────────────────────────────────
def get_target_date():
    today = datetime.now()
    target = today + timedelta(days=CONFIG["lead_days"])

    if target.weekday() != CONFIG["target_weekday"]:
        log.warning(
            f"  Achtung: Zieltag ({target.strftime('%A')}) ist nicht der konfigurierte "
            f"Wochentag (weekday={CONFIG['target_weekday']}). Skript sollte am dafuer "
            f"vorgesehenen Tag laufen (heute + {CONFIG['lead_days']} Tage muss auf den "
            f"Zielwochentag fallen)."
        )
    return target


def get_slot_timestamps(target: datetime, book_hour: int, book_end_hour: int):
    start = target.replace(hour=book_hour,     minute=0, second=0, microsecond=0)
    end   = target.replace(hour=book_end_hour, minute=0, second=0, microsecond=0)

    log.info(f"Buchungsziel: {start.strftime('%A %d.%m.%Y')} | {start.strftime('%H:%M')}-{end.strftime('%H:%M')} Uhr")
    log.info(f"starttime={int(start.timestamp())}  endtime={int(end.timestamp())}")
    return int(start.timestamp()), int(end.timestamp())


def players_str(delimiter: str = ";") -> str:
    return delimiter.join([CONFIG["user_id"]] + CONFIG["co_players"])


# ──────────────────────────────────────────────
# BOT
# ──────────────────────────────────────────────
class ETennisBot:

    def __init__(self):
        self.session = requests.Session()
        self.base_params = {"c": CONFIG["club_id"]}
        self.reservation_url = f'{url("/reservierung")}?c={CONFIG["club_id"]}'
        self.user_id = str(CONFIG["user_id"])
        self.co_players = [str(p) for p in CONFIG["co_players"]]
        self.session.headers.update({
            "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "X-Requested-With": "XMLHttpRequest",
            "Origin":           CONFIG["site_root"],
            "Referer":          self.reservation_url,
        })

    def bootstrap_reservation_context(self) -> None:
        # Manche Installationen setzen reservation-spezifische Cookies/Token erst nach dem Login,
        # daher die Seite einmal "warm" laden.
        try:
            r = self.session.get(url("/reservierung"), params=self.base_params, timeout=10)
            log.info(f"  Reservierung-Context: HTTP {r.status_code}")
        except Exception as e:
            log.warning(f"  Reservierung-Context konnte nicht geladen werden: {e}")

    def detect_user_id_from_profile(self) -> None:
        try:
            r = self.session.get(url("/profil"), params=self.base_params, timeout=10)
            if r.status_code != 200:
                return
            html = r.text or ""

            patterns = [
                r'data-user-id="(\d+)"',
                r'data-user="(\d+)"',
                r'"user"\s*:\s*"(\d+)"',
                r'"userId"\s*:\s*"(\d+)"',
                r'"userId"\s*:\s*(\d+)',
                r'userId\s*[:=]\s*(\d+)',
            ]
            for pat in patterns:
                m = re.search(pat, html)
                if m:
                    detected = m.group(1)
                    if detected and detected.isdigit() and detected != self.user_id:
                        log.info(f"  user_id auto-detected: {detected} (war {self.user_id})")
                        self.user_id = detected
                    return
        except Exception as e:
            log.warning(f"  user_id konnte nicht erkannt werden: {e}")

    def _extract_hidden_form_fields(self, html: str) -> dict:
        # Very small helper to grab hidden inputs like CSRF tokens.
        fields: dict[str, str] = {}
        if not html:
            return fields

        for name, value in re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html, flags=re.I):
            fields[name] = value
        return fields

    def _extract_input_names(self, html: str) -> set[str]:
        if not html:
            return set()
        return set(re.findall(r'<input[^>]+name="([^"]+)"', html, flags=re.I))

    def _extract_submit_fields(self, html: str) -> dict[str, str]:
        if not html:
            return {}

        fields: dict[str, str] = {}

        # <input type="submit" name="..." value="...">
        for name, value in re.findall(
            r'<input[^>]+type="submit"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html, flags=re.I
        ):
            fields[name] = value or "1"

        # <button name="..." value="...">
        for name, value in re.findall(
            r'<button[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html, flags=re.I
        ):
            fields[name] = value or "1"

        return fields

    def _extract_post_form_action(self, html: str) -> str | None:
        if not html:
            return None
        # Prefer a POST form action if present, otherwise any form action.
        m = re.search(r"<form[^>]+method=['\"]post['\"][^>]+action=['\"]([^'\"]+)['\"]", html, flags=re.I)
        if not m:
            m = re.search(r"<form[^>]+action=['\"]([^'\"]+)['\"]", html, flags=re.I)
        if not m:
            return None
        return (m.group(1) or "").strip() or None

    def _extract_login_error_hint(self, html: str) -> str | None:
        if not html:
            return None

        # 1) Try common patterns for inline flash messages.
        for pat in (
            r'<div[^>]+class="[^"]*(?:alert|error|invalid)[^"]*"[^>]*>(.*?)</div>',
            r'<p[^>]+class="[^"]*(?:alert|error|invalid)[^"]*"[^>]*>(.*?)</p>',
        ):
            m = re.search(pat, html, flags=re.I | re.S)
            if m:
                msg = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', m.group(1))).strip()
                if msg:
                    return msg[:220]

        # 2) Fallback: strip tags and return context around keywords.
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        for kw in ("Passwort", "passwort", "ungueltig", "ungueltige", "invalid", "falsch", "fehlgeschlagen"):
            idx = text.find(kw)
            if idx != -1:
                start = max(0, idx - 80)
                end = min(len(text), idx + 160)
                return text[start:end]
        return None

    def _extract_modal_text(self, html: str) -> str | None:
        if not html:
            return None
        # crude HTML->text for modal snippets
        text = re.sub(r"<br\s*/?>", " ", html, flags=re.I)
        text = re.sub(r"</p\s*>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:280] if text else None

    def login(self) -> bool:
        log.info("Login...")
        try:
            # Startseite laden um Session-Cookies zu setzen
            r = self.session.get(url("/reservierung"), params=self.base_params, timeout=10)
            log.info(f"  Startseite: HTTP {r.status_code}")

            if not CONFIG["username"] or not CONFIG["password"]:
                log.error("  Fehlende Credentials. Bitte ETENNIS_USERNAME und ETENNIS_PASSWORD setzen (z.B. in .env).")
                return False

            # eTennis ist je nach Installation unterschiedlich: manche nutzen HTML-Login unter /reservierung/login,
            # andere /login oder /ajax/login. Wir probieren in sinnvoller Reihenfolge.
            login_payload_base = {"username": CONFIG["username"], "password": CONFIG["password"], "cookie": "1"}
            login_attempts = [
                ("HTML /reservierung/login", url("/reservierung/login")),
                ("HTML /login", url("/login")),
                ("AJAX /ajax/login", url("/ajax/login")),
                ("AJAX /ajax/u/login", url("/ajax/u/login")),
                ("AJAX /ajax/user/login", url("/ajax/user/login")),
                ("AJAX /ajax/users/login", url("/ajax/users/login")),
            ]

            for label, endpoint in login_attempts:
                login_payload = dict(login_payload_base)

                if "/reservierung/login" in endpoint or endpoint.rstrip("/").endswith("/login"):
                    try:
                        pre = self.session.get(endpoint, params=self.base_params, timeout=10)
                        pre_html = pre.text or ""
                        if "captcha" in pre_html.lower() or "recaptcha" in pre_html.lower():
                            log.warning("  Hinweis: Login-Seite enthaelt Captcha/Recaptcha (kann requests-Login blockieren).")
                        ajax_paths = sorted(set(re.findall(r'/(ajax/[^\"\'\\s<>]+)', pre_html)))[:10]
                        if ajax_paths:
                            log.info(f"  AJAX-Pfade (Auszug): {', '.join('/'+p for p in ajax_paths)}")
                        action = self._extract_post_form_action(pre_html)
                        post_endpoint = endpoint
                        if action:
                            post_endpoint = url(action)
                            log.info(f"  Form-Action erkannt: {action} -> {post_endpoint}")
                        else:
                            post_endpoint = endpoint

                        names = self._extract_input_names(pre_html)
                        if names:
                            log.info(f"  Login-Formfelder: {', '.join(sorted(names))}")

                        if "email" in names:
                            login_payload.pop("username", None)
                            login_payload["email"] = CONFIG["username"]
                        elif "login" in names:
                            login_payload.pop("username", None)
                            login_payload["login"] = CONFIG["username"]

                        if "cookie" in names and "cookie" not in login_payload:
                            login_payload["cookie"] = "1"
                        for opt in ("push_etennis", "push_news", "push_reservation", "calendar"):
                            if opt in names and opt not in login_payload:
                                login_payload[opt] = "0"

                        submits = self._extract_submit_fields(pre_html)
                        if submits:
                            log.info(f"  Submit-Felder gefunden: {', '.join(sorted(submits.keys()))}")
                            login_payload.update(submits)

                        hidden = self._extract_hidden_form_fields(pre_html)
                        if hidden:
                            log.info(f"  Hidden-Felder gefunden: {', '.join(sorted(hidden.keys()))}")
                            login_payload.update(hidden)
                    except Exception as e:
                        log.warning(f"  Login-Preflight fehlgeschlagen: {e}")
                        post_endpoint = endpoint
                else:
                    post_endpoint = endpoint

                r = self.session.post(
                    post_endpoint,
                    params=None if "/ajax/" in post_endpoint else self.base_params,
                    data=login_payload,
                    timeout=10,
                    allow_redirects=True,
                )
                log.info(f"  {label}: HTTP {r.status_code} | URL: {r.url}")

                if "/login" in (r.url or ""):
                    hint = self._extract_login_error_hint(r.text or "")
                    if hint:
                        log.info(f"  Login-Hinweis: {hint}")

                ct = (r.headers.get("Content-Type") or "").lower()
                if "application/json" in ct:
                    try:
                        resp = r.json()
                        if resp.get("success") == 1 or resp.get("login") == 1:
                            log.info("  Login erfolgreich (JSON)")
                            self.bootstrap_reservation_context()
                            self.detect_user_id_from_profile()
                            return True
                    except Exception:
                        pass

                body_lower = (r.text or "").lower()
                if r.status_code == 200 and (("logout" in body_lower) or ("abmelden" in body_lower) or ("/logout" in body_lower)):
                    log.info("  Login erfolgreich (Logout-Link gefunden)")
                    self.bootstrap_reservation_context()
                    self.detect_user_id_from_profile()
                    return True

                if r.status_code in (200, 302) and "/login" not in (r.url or ""):
                    log.info("  Login vermutlich erfolgreich (Redirect/URL)")
                    self.bootstrap_reservation_context()
                    self.detect_user_id_from_profile()
                    return True

            r3 = self.session.get(url("/profil"), params=self.base_params, timeout=10)
            if "/login" not in r3.url:
                log.info("  Session aktiv (Profilseite erreichbar)")
                self.bootstrap_reservation_context()
                self.detect_user_id_from_profile()
                return True

            log.error("  Login fehlgeschlagen!")
            return False

        except Exception as e:
            log.error(f"  Login-Fehler: {e}")
            return False

    def init(self, starttime: int) -> bool:
        try:
            r = self.session.post(
                url("/ajax/reservation/init"),
                data={
                    "c": CONFIG["court_id"],
                    "a": "",
                    "begin": starttime,
                    "id": "",
                    "slots": "",
                    "edit": "",
                    "prid": "",
                    "ruleExecutedOnInit": "",
                },
                timeout=10,
            )
            log.info(f"Init: HTTP {r.status_code} | {r.text[:200]}")
            try:
                resp = r.json()
                if resp.get("nores") == 1:
                    log.warning(f"  Init: nores=1 -> {resp.get('msg') or resp}")
                    return False
                if resp.get("html"):
                    modal = self._extract_modal_text(resp.get("html") or "")
                    if modal:
                        log.warning(f"  Init-Modal: {modal}")
                if resp.get("login") == 1:
                    self.bootstrap_reservation_context()
                    r2 = self.session.post(
                        url("/ajax/reservation/init"),
                        data={
                            "c": CONFIG["court_id"], "a": "", "begin": starttime,
                            "id": "",
                            "slots": "",
                            "edit": "",
                            "prid": "",
                            "ruleExecutedOnInit": "",
                        },
                        timeout=10,
                    )
                    log.info(f"Init(retry): HTTP {r2.status_code} | {r2.text[:200]}")
                    resp = r2.json()
                    if resp.get("html"):
                        modal = self._extract_modal_text(resp.get("html") or "")
                        if modal:
                            log.warning(f"  Init-Modal(retry): {modal}")
                return resp.get("login") != 1
            except Exception:
                return False
        except Exception as e:
            log.error(f"Init-Fehler: {e}")
            return False

    def check(self, starttime: int, endtime: int) -> str:
        try:
            required_players = [self.user_id] + (self.co_players[:1] if self.co_players else [])
            players_semicolon = ";".join(required_players)

            payload_variants = [
                {
                    "players": players_semicolon,
                    "court": CONFIG["court_id"],
                    "starttime": starttime,
                    "endtime": endtime,
                    "suser": self.user_id,
                    "playerSelector": "",
                    "promotioncode": "",
                    "addon[292]": "0",
                    "caption": "",
                    "note": "",
                },
                {
                    "players": ";".join([self.user_id] + self.co_players),
                    "court": CONFIG["court_id"],
                    "starttime": starttime,
                    "endtime": endtime,
                    "suser": self.user_id,
                    "playerSelector": "",
                    "promotioncode": "",
                    "addon[292]": "0",
                    "caption": "",
                    "note": "",
                },
            ]

            for idx, payload in enumerate(payload_variants, start=1):
                r = self.session.post(
                    url("/ajax/reservation/check"),
                    data=payload,
                    timeout=10,
                )
                log.info(f"Check(v{idx}): HTTP {r.status_code} | {r.text[:300]}")
                resp = r.json()

                if resp.get("error"):
                    log.warning(f"  Fehler(v{idx}): {resp['error']}")
                    continue

                if resp.get("submit") == 1:
                    log.info(f"  Slot verfuegbar! Preis: {resp.get('price', '?')} EUR")
                    return "bookable"

                log.info("  Nicht buchbar (submit != 1)")
                return "not_bookable"

            return "error"
        except Exception as e:
            log.error(f"Check-Fehler: {e}")
            return "error"

    def save(self, starttime: int, endtime: int) -> bool:
        try:
            payload_variants = [
                {
                    "players": ";".join([self.user_id] + self.co_players),
                    "court": CONFIG["court_id"],
                    "starttime": starttime,
                    "endtime": endtime,
                    "suser": self.user_id,
                    "playerSelector": "",
                    "promotioncode": "",
                    "addon[292]": "0",
                    "caption": "",
                    "note": "",
                    "path": "reservation/save",
                    "class": "reservation-data",
                },
                {
                    "players": self.user_id,
                    "court": CONFIG["court_id"],
                    "starttime": starttime,
                    "endtime": endtime,
                    "suser": self.user_id,
                    "playerSelector": "",
                    "promotioncode": "",
                    "addon[292]": "0",
                    "caption": "",
                    "note": "",
                    "path": "reservation/save",
                    "class": "reservation-data",
                },
                {
                    "players": self.user_id,
                    "court": CONFIG["court_id"],
                    "starttime": starttime,
                    "endtime": endtime,
                    "user": self.user_id,
                    "playerSelector": "",
                    "promotioncode": "",
                    "addon[292]": "0",
                    "caption": "",
                    "note": "",
                    "path": "reservation/save",
                    "class": "reservation-data",
                },
                {
                    "players": ",".join([self.user_id] + self.co_players),
                    "court": CONFIG["court_id"],
                    "starttime": starttime,
                    "endtime": endtime,
                    "user": self.user_id,
                    "playerSelector": "",
                    "promotioncode": "",
                    "addon[292]": "0",
                    "caption": "",
                    "note": "",
                    "path": "reservation/save",
                    "class": "reservation-data",
                },
            ]

            for idx, payload in enumerate(payload_variants, start=1):
                r = self.session.post(
                    url("/ajax/reservation/save"),
                    data=payload,
                    timeout=10,
                )
                log.info(f"Save(v{idx}): HTTP {r.status_code} | {r.text[:300]}")
                resp = r.json()
                if resp.get("success") == 1:
                    log.info("BUCHUNG ERFOLGREICH!")
                    return True

                if resp.get("error"):
                    log.warning(f"  Save-Fehler(v{idx}): {resp.get('error')}")
                    continue

                log.error(f"Save fehlgeschlagen(v{idx}): {resp}")

            return False
        except Exception as e:
            log.error(f"Save-Fehler: {e}")
            return False

    def book_slot(self, book_hour: int, book_end_hour: int, target: datetime) -> None:
        log.info("-" * 50)
        starttime, endtime = get_slot_timestamps(target, book_hour, book_end_hour)

        if not self.init(starttime):
            log.error("Abbruch: Init zeigt weiterhin Login/keine Session oder Slot nicht verfuegbar.")
            return

        for attempt in range(1, CONFIG["max_retries"] + 1):
            log.info(f"Check-Versuch {attempt}/{CONFIG['max_retries']}...")
            status = self.check(starttime, endtime)
            if status == "bookable":
                self.save(starttime, endtime)
                return
            if status == "not_bookable":
                log.info("Slot nicht buchbar (evtl. schon belegt).")
                return
            if attempt < CONFIG["max_retries"]:
                time.sleep(CONFIG["retry_interval"])

        log.warning("Buchung nicht moeglich nach allen Versuchen.")

    def run(self):
        target = get_target_date()

        if not self.login():
            log.error("Abbruch: Login nicht moeglich.")
            return

        for book_hour, book_end_hour in CONFIG["slots"]:
            self.book_slot(book_hour, book_end_hour, target)


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("BUCHUNG: Mittwoch 16:00-18:00 und 18:00-20:00 Uhr, Platz 2")
    log.info("=" * 50)
    ETennisBot().run()
