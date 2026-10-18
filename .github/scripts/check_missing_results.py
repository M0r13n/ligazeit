import datetime as dt
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# ── Konfiguration ─────────────────────────────────────────────────────────
LEAGUES = [
    # (Anzeigename, OpenLigaDB-Shortcuts in Reihenfolge, TheSportsDB-Liga-ID)
    ("Frauen-Bundesliga", ["ffb1", "fbl1"], 5204),
]
TSDB_FREE_KEY = "123"           # öffentlicher Test-Key laut TheSportsDB-Doku
GRACE = dt.timedelta(hours=2, minutes=30)   # Anpfiff + Spieldauer + Puffer
LOOKBACK = dt.timedelta(days=21)            # ältere Lücken nicht mehr melden
DATE_TOLERANCE = dt.timedelta(days=1)       # Zeitzonen-/Datumsabweichungen
ISSUE_TITLE = "Fehlende Ergebnisse bei OpenLigaDB (Frauen)"
ISSUE_MARKER = "<!-- ligazeit:missing-results -->"
BOT_LOGIN = "github-actions[bot]"

ALLOWED_HOSTS = {"api.openligadb.de", "www.thesportsdb.com", "api.github.com"}
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 20
USER_AGENT = "ligazeit-missing-results-check (+https://github.com/M0r13n/ligazeit)"


# ── HTTP ──────────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None  # Redirects nicht folgen; 3xx wird zum Fehler


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_json(url, method="GET", token=None, body=None):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"Host nicht erlaubt: {parts.scheme}://{parts.hostname}")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if token is not None:
        if parts.hostname != "api.github.com":
            raise RuntimeError("Token darf nur an api.github.com gehen")
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=TIMEOUT) as resp:
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise RuntimeError(f"Antwort zu groß: {parts.hostname}")
    if not raw.strip():
        return None
    return json.loads(raw.decode("utf-8"))


# ── Validierung & Bereinigung von Fremddaten ──────────────────────────────
_SAFE_NAME = re.compile(r"[^\w .\-'’]", re.UNICODE)


def safe_name(value):
    """Vereinsname für die Anzeige: nur Buchstaben, Ziffern, Leerzeichen, . - '"""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = _SAFE_NAME.sub("", text).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()[:60]
    return text or "?"


def safe_int(value, lo, hi):
    """Ganzzahl im Bereich [lo, hi], sonst None (auch für '3' als String)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"\d{1,10}", value.strip()):
            return None
        value = int(value.strip())
    if not isinstance(value, int) or not lo <= value <= hi:
        return None
    return value


def parse_utc(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)


# ── Vereinsnamen vergleichen ──────────────────────────────────────────────
_STOP = {
    "fc", "vfl", "vfb", "sv", "sc", "tsg", "tsv", "fsv", "rb", "sgs", "spvgg", "bv",
    "bsc", "ssv", "msv", "sg", "vfr", "tus", "ffc", "bayer", "borussia", "eintracht",
    "union", "turbine", "frauen", "women", "ii", "u23", "1899", "1846",
}
_ALIASES = {"munich": "munchen", "cologne": "koln", "nuremberg": "nurnberg"}


def club_tokens(name):
    text = unicodedata.normalize("NFKD", str(name or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = re.split(r"[^a-z0-9]+", text)
    toks = {_ALIASES.get(w, w) for w in words if w and not w.isdigit() and w not in _STOP}
    return {t[:5] for t in toks if len(t) >= 3}


def same_club(a, b):
    ta, tb = club_tokens(a), club_tokens(b)
    return bool(ta and tb and ta & tb)


# ── Datenquellen ──────────────────────────────────────────────────────────
def season_for(now):
    return now.year if now.month >= 8 else now.year - 1


def load_openligadb(keys, season):
    for key in keys:
        if not re.fullmatch(r"[a-z0-9]{2,10}", key):
            raise RuntimeError("ungültiger Shortcut in der Konfiguration")
        data = http_json(f"https://api.openligadb.de/getmatchdata/{key}/{season}")
        if isinstance(data, list) and data:
            return key, data
    return None, []


def missing_matches(matches, now):
    out = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        kickoff = parse_utc(m.get("matchDateTimeUTC"))
        if kickoff is None or not (now - LOOKBACK <= kickoff <= now - GRACE):
            continue
        finals = [r for r in (m.get("matchResults") or [])
                  if isinstance(r, dict) and r.get("resultTypeID") == 2]
        if m.get("matchIsFinished") is True and finals:
            continue
        group = m.get("group") if isinstance(m.get("group"), dict) else {}
        t1 = m.get("team1") if isinstance(m.get("team1"), dict) else {}
        t2 = m.get("team2") if isinstance(m.get("team2"), dict) else {}
        out.append({
            "matchday": safe_int(group.get("groupOrderID"), 1, 99),
            "kickoff": kickoff,
            "home": safe_name(t1.get("teamName")),
            "away": safe_name(t2.get("teamName")),
        })
    out.sort(key=lambda x: x["kickoff"])
    return out


def load_tsdb_results(league_id):
    lid = safe_int(league_id, 1, 10**7)
    url = f"https://www.thesportsdb.com/api/v1/json/{TSDB_FREE_KEY}/eventspastleague.php?id={lid}"
    data = http_json(url)
    events = data.get("events") if isinstance(data, dict) else None
    results = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        hs = safe_int(e.get("intHomeScore"), 0, 99)
        as_ = safe_int(e.get("intAwayScore"), 0, 99)
        when = parse_utc(e.get("strTimestamp")) or parse_utc(f"{e.get('dateEvent')}T12:00:00")
        eid = safe_int(e.get("idEvent"), 1, 10**10)
        if hs is None or as_ is None or when is None or eid is None:
            continue
        results.append({"id": eid, "when": when, "home": e.get("strHomeTeam"),
                        "away": e.get("strAwayTeam"), "hs": hs, "as": as_})
    return results


def find_result(match, results):
    hits = [r for r in results
            if abs(r["when"] - match["kickoff"]) <= DATE_TOLERANCE
            and same_club(match["home"], r["home"]) and same_club(match["away"], r["away"])]
    return hits[0] if len(hits) == 1 else None


# ── Issue-Text ────────────────────────────────────────────────────────────
def render(report, season):
    if not report:
        return None
    lines = [ISSUE_MARKER,
             "Diese Spiele sind vorbei, haben bei OpenLigaDB aber noch kein Endergebnis.",
             "Vorschläge stammen von TheSportsDB (ebenfalls Community-Daten) – bitte vor "
             "dem Eintragen kurz gegenprüfen, z. B. bei kicker oder dfb.de.", ""]
    for league, key, items in report:
        lines.append(f"### {league} (`{key}`)")
        for it in items:
            md = it["matchday"]
            when = it["kickoff"].strftime("%d.%m. %H:%M UTC")
            # Namen als Code-Span: dort wirken weder Links noch Markdown oder
            # Erwähnungen; Backticks sind durch safe_name() bereits entfernt.
            line = f"- {md or '?'}. Spieltag, {when}: `{it['home']}` – `{it['away']}`"
            r = it.get("result")
            if r:
                line += (f" → **{r['hs']}:{r['as']}** "
                         f"([Quelle](https://www.thesportsdb.com/event/{r['id']}))")
            else:
                line += " → kein Ergebnis gefunden"
            if md:
                line += f" · [eintragen](https://www.openligadb.de/Matches/{key}/{season}/{md})"
            lines.append(line)
        lines.append("")
    lines.append("_Automatisch erstellt; schließt sich, sobald alles eingetragen ist._")
    return "\n".join(lines)


def item_keys(body):
    return {l for l in (body or "").splitlines() if l.startswith("- ")}


# ── GitHub-Issue pflegen ──────────────────────────────────────────────────
def sync_issue(body):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        print("Kein Token/Repo – Trockenlauf:\n" + (body or "Nichts fehlt."))
        return
    api = f"https://api.github.com/repos/{repo}"
    q = urllib.parse.urlencode({"state": "open", "creator": BOT_LOGIN, "per_page": 100})
    issues = http_json(f"{api}/issues?{q}", token=token) or []
    mine = [i for i in issues if isinstance(i, dict)
            and (i.get("user") or {}).get("login") == BOT_LOGIN
            and "pull_request" not in i
            and ISSUE_MARKER in (i.get("body") or "")]
    issue = mine[0] if mine else None

    if body is None:
        if issue:
            n = safe_int(issue.get("number"), 1, 10**9)
            http_json(f"{api}/issues/{n}/comments", "POST", token,
                      {"body": "Alle Ergebnisse sind eingetragen. ✔"})
            http_json(f"{api}/issues/{n}", "PATCH", token,
                      {"state": "closed", "state_reason": "completed"})
            print(f"Issue #{n} geschlossen.")
        else:
            print("Nichts fehlt.")
        return

    if issue is None:
        created = http_json(f"{api}/issues", "POST", token, {"title": ISSUE_TITLE, "body": body})
        print(f"Issue #{created.get('number')} angelegt.")
        return

    n = safe_int(issue.get("number"), 1, 10**9)
    old = issue.get("body") or ""
    if old == body:
        print(f"Issue #{n} unverändert.")
        return
    http_json(f"{api}/issues/{n}", "PATCH", token, {"body": body})
    new = item_keys(body) - item_keys(old)
    if new:  # Kommentar löst eine Benachrichtigung aus, ein Edit nicht
        http_json(f"{api}/issues/{n}/comments", "POST", token,
                  {"body": "Neu hinzugekommen:\n" + "\n".join(sorted(new))})
    print(f"Issue #{n} aktualisiert.")


# ── Ablauf ────────────────────────────────────────────────────────────────
def main():
    now = dt.datetime.now(dt.timezone.utc)
    season = season_for(now)
    report = []
    for league, keys, tsdb_id in LEAGUES:
        key, matches = load_openligadb(keys, season)
        if not key:
            # Ausfall oder leere Antwort darf nie wie "alles eingetragen" wirken:
            # abbrechen, Issue unverändert lassen, Lauf rot markieren.
            raise RuntimeError(f"{league}: keine Daten bei OpenLigaDB ({', '.join(keys)})")
        items = missing_matches(matches, now)
        if items and tsdb_id:
            try:
                results = load_tsdb_results(tsdb_id)
            except (urllib.error.URLError, RuntimeError, ValueError) as exc:
                print(f"TheSportsDB nicht erreichbar: {type(exc).__name__}")
                results = []
            for it in items:
                it["result"] = find_result(it, results)
        if items:
            report.append((league, key, items))
    sync_issue(render(report, season))


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        # Keine Antwortinhalte ausgeben, nur den Fehlertyp und die Nachricht.
        print(f"Fehler: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
