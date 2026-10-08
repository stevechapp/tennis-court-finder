"""
Tennis court finder - v2 (ClubSpark + Better)

Examples:
  py court_finder.py                         today, all venues
  py court_finder.py --days 7 --after 17:00  next week, evenings only
  py court_finder.py --area irene --min 90   near Irene's, 90+ min runs
  py court_finder.py --debug highbury        dump raw data for one venue
  py court_finder.py --html                  open a week-ahead page in your browser

Better login: if Better refuses anonymous requests, paste your bearer token
(the long string after "Bearer " in DevTools) into better_token.txt next to
this script. Treat that file like a password; tokens expire, so refresh it
when the script says so.
"""
import argparse
import json
import os
import webbrowser
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- venues
# areas: "home" = Tottenham side, "irene" = Newington Green side
VENUES = [
    # ClubSpark (LTA)
    {"name": "Bruce Castle Park", "platform": "clubspark", "slug": "BruceCastlePark",         "areas": ["home"]},
    {"name": "Chestnuts Park",    "platform": "clubspark", "slug": "chestnutspark",           "areas": ["home"]},
    {"name": "Downhills Park",    "platform": "clubspark", "slug": "DownhillsParkTennisClub", "areas": ["home"]},
    {"name": "Finsbury Park",     "platform": "clubspark", "slug": "FinsburyPark",            "areas": ["home", "irene"]},
    {"name": "Clissold Park",     "platform": "clubspark", "slug": "ClissoldParkHackney",     "areas": ["irene"]},
    # Better (GLL)
    {"name": "Highbury Fields",   "platform": "better", "venue": "islington-tennis-centre",
     "activity": "highbury-tennis",      "areas": ["irene"]},
    {"name": "Islington Tennis Centre (outdoor)", "platform": "better", "venue": "islington-tennis-centre",
     "activity": "tennis-court-outdoor", "areas": ["irene"]},
    # To add Rosemary Gardens / ITC indoor: open the timetable on bookings.better.org.uk
    # and copy the activity slug from the URL (/location/<venue>/<activity>/<date>/...)
]

EXCLUDE_COURT_WORDS = ("pickleball", "mini")   # ClubSpark courts to ignore
CLUBSPARK_CHUNK_DAYS = 7

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CLUBSPARK_HEADERS = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json, text/javascript, */*; q=0.01"}
BETTER_HEADERS = {"User-Agent": UA, "Accept": "application/json",
                  "Origin": "https://bookings.better.org.uk",
                  "Referer": "https://bookings.better.org.uk/"}
TOKEN_FILE = Path(__file__).with_name("better_token.txt")


@dataclass
class Slot:
    venue: str
    court: str          # court name (ClubSpark) or "any court" (Better)
    day: date
    start: int          # minutes from midnight
    end: int
    cost: float
    link: str
    spaces: int | None = None   # Better only: courts free


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def days_between(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ================================================================ ClubSpark
def clubspark_link(v, day):
    return f"https://clubspark.lta.org.uk/{v['slug']}/Booking/BookByDate#?date={day.isoformat()}&role=guest"


def clubspark_fetch(v, start, end):
    out, chunk = [], start
    while chunk <= end:
        chunk_end = min(chunk + timedelta(days=CLUBSPARK_CHUNK_DAYS - 1), end)
        r = requests.get(
            f"https://clubspark.lta.org.uk/v0/VenueBooking/{v['slug']}/GetVenueSessions",
            params={"resourceID": "", "startDate": chunk.isoformat(), "endDate": chunk_end.isoformat(),
                    "roleId": "", "_": int(time.time() * 1000)},
            headers={**CLUBSPARK_HEADERS,
                     "Referer": f"https://clubspark.lta.org.uk/{v['slug']}/Booking/BookByDate"},
            timeout=20)
        r.raise_for_status()
        out.append(r.json())
        chunk = chunk_end + timedelta(days=1)
    return out


def clubspark_parse(v, responses):
    """Category 0 + Capacity > 0 = open window; any other session blocks its time."""
    found, seen_days = {}, set()
    for data in responses:
        for res in data.get("Resources", []):
            court = res.get("Name", "?")
            if any(w in court.lower() for w in EXCLUDE_COURT_WORDS):
                continue
            for d in res.get("Days", []):
                day = datetime.fromisoformat(d["Date"][:10]).date()
                sessions = d.get("Sessions", [])
                opens = [s for s in sessions if s.get("Category") == 0 and s.get("Capacity", 0) > 0]
                blocks = [s for s in sessions if s not in opens]
                if opens:
                    seen_days.add(day)
                for w in opens:
                    step = w.get("Interval") or 60
                    t = w["StartTime"]
                    while t + step <= w["EndTime"]:
                        if not any(b["StartTime"] < t + step and b["EndTime"] > t for b in blocks):
                            found[(court, day, t)] = Slot(v["name"], court, day, t, t + step,
                                                          w.get("Cost", 0.0), clubspark_link(v, day))
                        t += step
    return list(found.values()), seen_days


def clubspark_debug(v, responses):
    tally = {}
    for data in responses:
        for res in data.get("Resources", []):
            for d in res.get("Days", []):
                print(f"\n  {res.get('Name')}  {d['Date'][:10]}")
                for s in sorted(d.get("Sessions", []), key=lambda s: s["StartTime"]):
                    k = (s.get("Category"), s.get("SubCategory"), s.get("Capacity"))
                    tally[k] = tally.get(k, 0) + 1
                    print(f"    {hhmm(s['StartTime'])}-{hhmm(s['EndTime'])}  cat={s.get('Category')} "
                          f"sub={s.get('SubCategory')} cap={s.get('Capacity')} "
                          f"£{s.get('Cost', 0):.2f}  {s.get('Name', '')[:40]}")
    print("\n  Tally (category, subcategory, capacity): count")
    for k, n in sorted(tally.items(), key=lambda kv: str(kv[0])):
        print(f"    {k}: {n}")


# ================================================================ Better
class BetterAuthError(Exception):
    pass


def better_token():
    tok = os.environ.get("BETTER_TOKEN")
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if tok and tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return tok or None


def better_link(v, day):
    return f"https://bookings.better.org.uk/location/{v['venue']}/{v['activity']}/{day.isoformat()}/by-time"


def better_get(url, params):
    r = requests.get(url, params=params, headers=BETTER_HEADERS, timeout=20)
    if r.status_code in (401, 403):
        tok = better_token()
        if not tok:
            raise BetterAuthError("Better wants you logged in - put your token in better_token.txt")
        r = requests.get(url, params=params,
                         headers={**BETTER_HEADERS, "Authorization": f"Bearer {tok}"}, timeout=20)
        if r.status_code in (401, 403):
            raise BetterAuthError("Better token rejected (probably expired) - grab a fresh one")
    r.raise_for_status()
    return r.json()


def better_fetch(v, start, end):
    url = (f"https://better-admin.org.uk/api/activities/venue/{v['venue']}"
           f"/activity/{v['activity']}/v2/times")
    out = []
    for day in days_between(start, end):
        try:
            out.append((day, better_get(url, {"date": day.isoformat()})))
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                break   # Better's way of saying "date not released yet"
            raise
        time.sleep(0.3)
    return out


def better_items(data):
    items = data.get("data", []) if isinstance(data, dict) else data
    return list(items.values()) if isinstance(items, dict) else items


def better_parse(v, responses):
    slots, seen_days = [], set()
    now_utc = datetime.now(timezone.utc)
    for day, data in responses:
        for it in better_items(data):
            fba = (it.get("first_bookable_at") or {}).get("utc")
            if fba and datetime.fromisoformat(fba) > now_utc:
                continue                       # not released yet
            seen_days.add(day)
            spaces = it.get("spaces") or 0
            if spaces <= 0:
                continue
            price = (it.get("price") or {}).get("formatted_amount", "")
            try:
                cost = float(price.replace("£", "").replace(",", ""))
            except ValueError:
                cost = 0.0
            slots.append(Slot(v["name"], "any court", day,
                              parse_hhmm(it["starts_at"]["format_24_hour"]),
                              parse_hhmm(it["ends_at"]["format_24_hour"]),
                              cost, better_link(v, day), spaces))
    return slots, seen_days


def better_debug(v, responses):
    for day, data in responses:
        print(f"\n  {day}")
        for it in better_items(data):
            status = (it.get("action_to_show") or {}).get("status")
            print(f"    {it['starts_at']['format_24_hour']}-{it['ends_at']['format_24_hour']}  "
                  f"spaces={it.get('spaces')}  status={status}  "
                  f"{(it.get('price') or {}).get('formatted_amount', '')}  "
                  f"bookable from {(it.get('first_bookable_at') or {}).get('local', '?')}")


# ================================================================ dispatch
PLATFORMS = {
    "clubspark": (clubspark_fetch, clubspark_parse, clubspark_debug),
    "better":    (better_fetch,    better_parse,    better_debug),
}
LINKS = {"clubspark": clubspark_link, "better": better_link}
AREA_NAMES = {"home": "Tottenham", "irene": "Newington Green"}


def merge_runs(slots):
    """Join back-to-back slots on the same court into one run."""
    slots = sorted(slots, key=lambda s: (s.day, s.venue, s.court, s.start))
    runs = []
    for s in slots:
        last = runs[-1] if runs else None
        if last and (last.day, last.venue, last.court) == (s.day, s.venue, s.court) and last.end == s.start:
            last.end = s.end
            if s.spaces is not None:
                last.spaces = min(last.spaces, s.spaces)
        else:
            runs.append(replace(s))
    return runs


# ================================================================ HTML page
def write_html(venues, start, end, slots, lines, notes):
    hours_seen = [s.start // 60 for s in slots]
    h_from = min(hours_seen + [7])
    h_to = max(hours_seen + [21])

    days = []
    for day in days_between(start, end):
        rows = []
        for v in venues:
            cells = {}
            for s in slots:
                if s.venue != v["name"] or s.day != day:
                    continue
                c = cells.setdefault(str(s.start // 60), {"n": 0, "courts": [], "price": s.cost})
                if s.spaces is not None:
                    c["n"] = max(c["n"], s.spaces)
                elif s.court not in c["courts"]:
                    c["courts"].append(s.court)
                    c["n"] = len(c["courts"])
                c["price"] = min(c["price"], s.cost)
            rows.append({"venue": v["name"], "link": LINKS[v["platform"]](v, day), "cells": cells})
        runs = [{"venue": g["venue"], "start": g["start"], "end": g["end"], "price": g["cost"],
                 "label": g["label"], "link": g["link"]} for g in lines if g["day"] == day]
        days.append({"label": f"{day:%a %d}", "long": f"{day:%A %d %B}", "rows": rows, "runs": runs})

    data = {
        "generated": datetime.now().strftime("%A %d %B, %H:%M"),
        "hours": list(range(h_from, h_to + 1)),
        "areas": AREA_NAMES,
        "venues": [{"name": v["name"], "areas": v["areas"]} for v in venues],
        "days": days,
        "notes": notes,
    }
    payload = json.dumps(data).replace("</", "<\\/")
    path = Path(__file__).with_name("courts.html")
    path.write_text(HTML_TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    return path


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Free tennis courts</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#EDF1EA; --panel:#F8FAF6; --ink:#1D2F26; --muted:#5A6D61;
  --court:#3A7556; --line:#FFFFFF;
  --ball-1:#EEF5BE; --ball-2:#DDEC6E; --ball-3:#C9DF35;
  --focus:#1D2F26;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#121D17; --panel:#1A2820; --ink:#E2EBE4; --muted:#97AC9F;
    --court:#2C5A42; --line:#0E1712;
    --ball-1:#5E6B2A; --ball-2:#93A53A; --ball-3:#C9DF35;
    --focus:#C9DF35;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:400 16px/1.5 Barlow, "Segoe UI", system-ui, sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px 32px;margin-bottom:22px}
h1{font:700 44px/1 "Barlow Condensed", "Arial Narrow", sans-serif;margin:0;letter-spacing:-.01em}
.generated{color:var(--muted);font-size:14px;margin:6px 0 0}
.controls{display:flex;flex-wrap:wrap;gap:14px}
.seg{display:inline-flex;border:1.5px solid var(--ink);border-radius:999px;overflow:hidden}
.seg button{font:500 15px/1 Barlow, sans-serif;color:var(--ink);background:transparent;border:0;
  padding:9px 15px;cursor:pointer}
.seg button+button{border-left:1.5px solid var(--ink)}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--bg)}
button:focus-visible,a:focus-visible{outline:3px solid var(--focus);outline-offset:2px}

.days{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;margin-bottom:18px}
.days button{flex:0 0 auto;font:600 20px/1 "Barlow Condensed", sans-serif;color:var(--ink);
  background:var(--panel);border:1.5px solid transparent;border-radius:10px;padding:10px 16px 8px;cursor:pointer;text-align:left}
.days button small{display:block;font:500 13px/1.3 Barlow, sans-serif;color:var(--muted);margin-top:4px}
.days button[aria-selected="true"]{border-color:var(--ink)}

.court{overflow-x:auto;border-radius:6px;background:var(--court);padding:10px}
table{border-collapse:separate;border-spacing:2px;background:var(--line);min-width:100%}
th,td{padding:0;text-align:center}
thead th{background:var(--court);color:#fff;font:600 15px/1 "Barlow Condensed", sans-serif;padding:8px 4px;min-width:46px}
th.venue{position:sticky;left:0;z-index:1;background:var(--panel);color:var(--ink);text-align:left;
  font:600 15px/1.2 Barlow, sans-serif;padding:0 14px;white-space:nowrap;height:46px}
thead th.venue{background:var(--court)}
td{background:var(--court);height:46px}
td a{display:flex;align-items:center;justify-content:center;width:100%;height:100%;
  font:700 19px/1 "Barlow Condensed", sans-serif;color:#1D2F26;text-decoration:none}
td.f1{background:var(--ball-1)} td.f2{background:var(--ball-2)} td.f3{background:var(--ball-3)}
td a:hover{box-shadow:inset 0 0 0 3px #1D2F26}
.key{color:var(--muted);font-size:14px;margin:10px 2px 0}

h2{font:700 28px/1.1 "Barlow Condensed", sans-serif;margin:40px 0 4px}
.venue-block{margin-top:22px}
.venue-block h3{font:600 18px/1.3 Barlow, sans-serif;margin:0 0 6px}
.runs{list-style:none;margin:0;padding:0;border-top:1.5px solid var(--ink)}
.runs li{display:grid;grid-template-columns:7.5em 4em 4.5em 1fr auto;gap:12px;align-items:center;
  padding:9px 2px;border-bottom:1px solid color-mix(in srgb, var(--ink) 18%, transparent)}
.runs .t{font-weight:600;white-space:nowrap}
.runs .d,.runs .p{color:var(--muted)}
.runs a{color:var(--ink);font-weight:600}
.empty{background:var(--panel);border-radius:10px;padding:18px 20px;margin-top:14px}
.notes{color:var(--muted);font-size:14px;margin-top:40px}
.notes p{margin:4px 0}
@media (max-width:640px){
  h1{font-size:36px}
  .court{padding:6px}
  th.venue{white-space:normal;min-width:96px;max-width:118px;font-size:13px;padding:0 8px;height:44px}
  thead th{min-width:38px;font-size:14px}
  td{height:44px}
  .runs li{grid-template-columns:7.6em 3em 1fr auto;gap:8px;font-size:15px}
  .runs .c{grid-column:1 / -2;grid-row:2;color:var(--muted);font-size:14px;margin-top:-6px}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Free tennis courts</h1>
      <p class="generated" id="generated"></p>
    </div>
    <div class="controls">
      <div class="seg" id="area" role="group" aria-label="Area"></div>
      <div class="seg" id="min" role="group" aria-label="Minimum length"></div>
    </div>
  </header>

  <div class="days" id="days" role="tablist" aria-label="Day"></div>

  <div class="court"><table id="grid"></table></div>
  <p class="key">Numbers show how many courts are free that hour. Click one to open the booking page.</p>

  <h2 id="list-title"></h2>
  <div id="list"></div>

  <div class="notes" id="notes"></div>
</div>

<script>
const D = __DATA__;
const state = {day: 0, area: "all", min: 60};

const pad = n => String(n).padStart(2, "0");
const fmt = m => pad(Math.floor(m / 60)) + ":" + pad(m % 60);
const price = p => p === 0 ? "free" : "£" + p.toFixed(2);
const dur = m => (m % 60 ? (m / 60).toFixed(1) : m / 60) + "h";
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

const visible = () => new Set(D.venues
  .filter(v => state.area === "all" || v.areas.includes(state.area)).map(v => v.name));

function segButtons(el, options, key) {
  el.innerHTML = options.map(([val, label]) =>
    `<button type="button" data-v="${val}" aria-pressed="${state[key] === val}">${label}</button>`).join("");
  el.onclick = e => {
    const b = e.target.closest("button"); if (!b) return;
    state[key] = key === "min" ? Number(b.dataset.v) : b.dataset.v;
    render();
  };
}

function freeHours(day, vis) {
  return day.rows.filter(r => vis.has(r.venue))
    .reduce((n, r) => n + Object.values(r.cells).filter(c => c.n > 0).length, 0);
}

function render() {
  const vis = visible();
  const day = D.days[state.day];

  segButtons(document.getElementById("area"),
    [["all", "All"], ...Object.entries(D.areas)], "area");
  segButtons(document.getElementById("min"),
    [[60, "1h+"], [90, "1.5h+"], [120, "2h+"]], "min");

  const tabs = document.getElementById("days");
  tabs.innerHTML = D.days.map((d, i) => {
    const n = freeHours(d, vis);
    return `<button type="button" role="tab" data-i="${i}" aria-selected="${i === state.day}">
      ${esc(d.label)}<small>${n ? n + " court-hours" : "nothing free"}</small></button>`;
  }).join("");
  tabs.onclick = e => {
    const b = e.target.closest("button"); if (!b) return;
    state.day = Number(b.dataset.i); render();
  };

  const head = `<thead><tr><th class="venue" scope="col"><span hidden>Venue</span></th>` +
    D.hours.map(h => `<th scope="col">${pad(h)}</th>`).join("") + `</tr></thead>`;
  const body = day.rows.filter(r => vis.has(r.venue)).map(r => {
    const cells = D.hours.map(h => {
      const c = r.cells[h];
      if (!c || c.n <= 0) return `<td></td>`;
      const cls = c.n >= 3 ? "f3" : c.n === 2 ? "f2" : "f1";
      const who = c.courts.length ? c.courts.join(", ") : c.n + " court" + (c.n > 1 ? "s" : "");
      const tip = `${r.venue}, ${pad(h)}:00, ${who}, ${price(c.price)}`;
      return `<td class="${cls}"><a href="${esc(r.link)}" target="_blank" rel="noopener"
        title="${esc(tip)}" aria-label="${esc(tip)}">${c.n}</a></td>`;
    }).join("");
    return `<tr><th class="venue" scope="row">${esc(r.venue)}</th>${cells}</tr>`;
  }).join("");
  document.getElementById("grid").innerHTML = head + `<tbody>${body}</tbody>`;
  // on narrow screens, scroll the grid to the first free hour
  const court = document.querySelector(".court"), firstFree = court.querySelector("td a");
  const nameCol = court.querySelector("th.venue");
  court.scrollLeft = firstFree ? firstFree.parentElement.offsetLeft - nameCol.offsetWidth - 12 : 0;

  document.getElementById("list-title").textContent = day.long;
  const runs = day.runs.filter(r => vis.has(r.venue) && r.end - r.start >= state.min);
  const byVenue = {};
  runs.forEach(r => (byVenue[r.venue] ??= []).push(r));
  const list = document.getElementById("list");
  if (!runs.length) {
    list.innerHTML = `<div class="empty">No free runs of ${dur(state.min)} or more on this day.
      Try a shorter length, another day, or All areas.</div>`;
  } else {
    list.innerHTML = Object.entries(byVenue).map(([venue, rs]) => `
      <section class="venue-block"><h3>${esc(venue)}</h3><ul class="runs">` +
      rs.map(r => `<li><span class="t">${fmt(r.start)}–${fmt(r.end)}</span>
        <span class="d">${dur(r.end - r.start)}</span><span class="p">${price(r.price)}</span>
        <span class="c">${esc(r.label)}</span>
        <a href="${esc(r.link)}" target="_blank" rel="noopener">Book</a></li>`).join("") +
      `</ul></section>`).join("");
  }
}

document.getElementById("generated").textContent = "Checked " + D.generated;
document.getElementById("notes").innerHTML = D.notes.map(n => `<p>${esc(n)}</p>`).join("");
render();
</script>
</body>
</html>
"""


def court_label(rs):
    if rs[0].spaces is not None:
        n = rs[0].spaces
        return f"{n} court{'s' if n != 1 else ''} free"
    names = [x.court for x in rs]
    if all(nm.startswith("Court ") for nm in names):
        return ("Court " if len(names) == 1 else "Courts ") + ", ".join(nm[6:] for nm in names)
    return ", ".join(names)


def group_runs(runs):
    """Courts with identical free runs share one line."""
    groups = {}
    for r in runs:
        groups.setdefault((r.day, r.venue, r.start, r.end, r.cost), []).append(r)
    return [{"day": day, "venue": venue, "start": s, "end": e, "cost": cost,
             "label": court_label(rs), "link": rs[0].link}
            for (day, venue, s, e, cost), rs in sorted(groups.items(), key=lambda kv: kv[0][:4])]


def print_text(lines):
    cur_day = cur_venue = None
    for g in lines:
        if g["day"] != cur_day:
            cur_day, cur_venue = g["day"], None
            print(f"\n{g['day']:%A %d %b}")
        if g["venue"] != cur_venue:
            cur_venue = g["venue"]
            print(f"  {g['venue']}   {g['link']}")
        price = "free" if g["cost"] == 0 else f"£{g['cost']:.2f}"
        print(f"      {hhmm(g['start'])}-{hhmm(g['end'])}   {price:<7} {g['label']}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="Find free tennis courts.")
    p.add_argument("--days", type=int, help="days to look ahead (default: 1, or 7 with --html)")
    p.add_argument("--start", type=date.fromisoformat, default=date.today(), help="start date YYYY-MM-DD")
    p.add_argument("--after", default="00:00", help="earliest start, e.g. 17:00")
    p.add_argument("--before", default="23:59", help="latest finish, e.g. 21:00")
    p.add_argument("--min", type=int, default=60, help="minimum continuous minutes (default 60)")
    p.add_argument("--area", choices=["home", "irene", "all"], default="all")
    p.add_argument("--venue", help="only venues whose name contains this text")
    p.add_argument("--debug", metavar="VENUE", help="dump raw data for one venue and exit")
    p.add_argument("--html", action="store_true", help="write courts.html and open it in your browser")
    args = p.parse_args()

    days = args.days or (7 if args.html else 1)
    start, end = args.start, args.start + timedelta(days=days - 1)
    after, before = parse_hhmm(args.after), parse_hhmm(args.before)
    now = datetime.now()

    if args.debug:
        v = next((v for v in VENUES if args.debug.lower() in v["name"].lower()), None)
        if not v:
            sys.exit(f"No venue matching {args.debug!r}")
        fetch, _, dbg = PLATFORMS[v["platform"]]
        print(f"\nRAW DATA - {v['name']} ({v['platform']})")
        dbg(v, fetch(v, start, end))
        return

    venues = [v for v in VENUES if args.area == "all" or args.area in v["areas"]]
    if args.venue:
        venues = [v for v in venues if args.venue.lower() in v["name"].lower()]

    def work(v):
        fetch, parse, _ = PLATFORMS[v["platform"]]
        try:
            slots, seen = parse(v, fetch(v, start, end))
            return v, slots, seen, None
        except Exception as e:
            return v, [], set(), e

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, venues))

    all_slots, notes = [], []
    for v, slots, seen, err in results:
        if err:
            notes.append(f"{v['name']}: {err}")
            continue
        all_slots += [s for s in slots
                      if s.start >= after and s.end <= before
                      and not (s.day == now.date() and s.start < now.hour * 60 + now.minute)]
        if seen and max(seen) < end:
            notes.append(f"{v['name']}: nothing released after {max(seen):%a %d %b} yet")

    runs = [r for r in merge_runs(all_slots) if r.end - r.start >= args.min]

    print(f"\nFree courts {start:%a %d %b} - {end:%a %d %b}, "
          f"{args.after}-{args.before}, {args.min}+ min, area: {args.area}")
    lines = group_runs(runs)
    if args.html:
        path = write_html(venues, start, end, all_slots, lines, notes)
        print(f"  Page written to {path}")
        webbrowser.open(path.as_uri())
    else:
        if not runs:
            print("\n  Nothing found.")
        print_text(lines)

    for n in notes:
        print(f"\n  note: {n}")
    print()


if __name__ == "__main__":
    main()
