"""World skills — weather, headlines, arithmetic, units, definitions.

The small questions an assistant is asked constantly. Three principles:

  * No API keys. Weather comes from wttr.in and headlines from Google News' RSS
    feed, both keyless, so these work on a bare clone like everything else.
  * Arithmetic is evaluated, not guessed. A language model doing mental
    arithmetic is a liability, so `calculate` parses the expression into an AST
    and walks it with a whitelist. `eval` is never called.
  * Unit conversion is a table of exact factors, not a prompt. 0.45359237 kg per
    pound is a defined constant; there is no reason to approximate it.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .base import Context, Result, Risk, Skill

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Black Number/0.2"


def _fetch(url: str, timeout: int = 15, limit: int = 400_000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", errors="replace")


def _where(a: dict, ctx: Context) -> str:
    """The place a question means. An explicit argument wins; otherwise the
    configured home city; otherwise empty, and wttr.in geolocates by IP."""
    return (a.get("location") or "").strip() or (getattr(ctx.cfg, "city", "") or "").strip()


# ── weather ─────────────────────────────────────────────────────────────────
def _weather(a: dict, ctx: Context) -> Result:
    place = _where(a, ctx)
    url = f"https://wttr.in/{urllib.parse.quote(place)}?format=j1"
    try:
        data = json.loads(_fetch(url))
    except Exception as e:
        return Result.fail(f"I couldn't get the weather: {e}")
    try:
        cur = data["current_condition"][0]
        area = data["nearest_area"][0]
        today = data["weather"][0]
    except (KeyError, IndexError):
        return Result.fail("The weather service returned something I couldn't read.")

    name = area["areaName"][0]["value"]
    region = area.get("country", [{}])[0].get("value", "")
    desc = cur["weatherDesc"][0]["value"].strip()
    temp, feels = cur["temp_C"], cur["FeelsLikeC"]
    hi, lo = today["maxtempC"], today["mintempC"]
    astro = today["astronomy"][0]

    speech = f"{desc} and {temp} degrees in {name}"
    if feels != temp:
        speech += f", feels like {feels}"
    speech += f". High {hi}, low {lo}."
    rows = [
        ("place", f"{name}{', ' + region if region else ''}"),
        ("now", f"{desc}, {temp}°C (feels {feels}°C)"),
        ("today", f"high {hi}°C / low {lo}°C"),
        ("humidity", f"{cur['humidity']}%"),
        ("wind", f"{cur.get('windspeedKmph', '?')} km/h {cur.get('winddir16Point', '')}".strip()),
        ("cloud", f"{cur.get('cloudcover', '?')}%"),
        ("uv index", str(cur.get("uvIndex", "?"))),
        ("sun", f"{astro.get('sunrise', '?')} → {astro.get('sunset', '?')}"),
    ]
    rain = sum(float(h.get("chanceofrain", 0) or 0) for h in today.get("hourly", []))
    if today.get("hourly"):
        peak = max(int(h.get("chanceofrain", 0) or 0) for h in today["hourly"])
        rows.append(("rain chance", f"peaks at {peak}% today"))
        if peak >= 60:
            speech += f" Rain likely — up to {peak} percent."
    return Result.say(
        speech,
        detail="\n".join(f"{k:12} {v}" for k, v in rows),
        data={"place": name, "temp_c": temp, "desc": desc, "high_c": hi, "low_c": lo},
    )


def _forecast(a: dict, ctx: Context) -> Result:
    place = _where(a, ctx)
    days = max(1, min(int(a.get("days") or 3), 3))     # wttr.in serves three
    try:
        data = json.loads(_fetch(f"https://wttr.in/{urllib.parse.quote(place)}?format=j1"))
    except Exception as e:
        return Result.fail(f"I couldn't get the forecast: {e}")
    name = data["nearest_area"][0]["areaName"][0]["value"]
    lines = []
    for day in data["weather"][:days]:
        noon = next((h for h in day.get("hourly", []) if h.get("time") == "1200"),
                    (day.get("hourly") or [{}])[0])
        desc = (noon.get("weatherDesc") or [{"value": "?"}])[0]["value"].strip()
        rain = max((int(h.get("chanceofrain", 0) or 0) for h in day.get("hourly", [])), default=0)
        lines.append(f"{day['date']}  {day['mintempC']:>3}–{day['maxtempC']:>3}°C  "
                     f"{desc:<22} rain up to {rain}%")
    first = data["weather"][0]
    return Result.say(
        f"{name}: {first['mintempC']} to {first['maxtempC']} degrees today, "
        f"{len(lines)} days ahead in detail.",
        detail="\n".join(lines),
    )


# ── headlines ───────────────────────────────────────────────────────────────
def _news(a: dict, ctx: Context) -> Result:
    topic = (a.get("topic") or "").strip()
    count = max(1, min(int(a.get("count") or 6), 15))
    if topic:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(topic) + "&hl=en-US&gl=US&ceid=US:en")
    else:
        url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    try:
        xml = _fetch(url, timeout=15)
        root = ET.fromstring(xml)
    except ET.ParseError:
        return Result.fail("The news feed returned something I couldn't parse.")
    except Exception as e:
        return Result.fail(f"I couldn't reach the news: {e}")

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title:
            items.append({"title": title, "source": source, "url": link})
        if len(items) >= count:
            break
    if not items:
        return Result.say(f"No headlines for {topic or 'today'}.")
    detail = "\n".join(f"{i+1}. {it['title']}" + (f"\n   {it['url']}" if it["url"] else "")
                       for i, it in enumerate(items))
    lead = items[0]["title"]
    label = f" on {topic}" if topic else ""
    return Result.say(f"Top headline{label}: {lead}", detail=detail, data=items)


# ── arithmetic, evaluated rather than guessed ───────────────────────────────
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor,
    "ceil": math.ceil, "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "hypot": math.hypot, "min": min, "max": max, "sum": sum, "pow": math.pow,
    "degrees": math.degrees, "radians": math.radians, "factorial": math.factorial,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
MAX_EXPONENT = 1000        # 2**10**9 would hang the process; refuse instead


class _Unsafe(ValueError):
    pass


def _eval_node(node):
    """Walk a parsed expression. Anything not explicitly allowed is refused,
    so there is no path from user text to attribute access, names or calls
    outside the whitelist."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise _Unsafe("only numbers are allowed")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise _Unsafe("that operator isn't allowed")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if op is operator.pow and (abs(right) > MAX_EXPONENT or abs(left) > 1e12):
            raise _Unsafe("that power is too large to compute")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise _Unsafe("that operator isn't allowed")
        return op(_eval_node(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise _Unsafe(f"I don't know the value '{node.id}'")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise _Unsafe("that function isn't allowed")
        if node.keywords:
            raise _Unsafe("keyword arguments aren't supported")
        args = [_eval_node(x) for x in node.args]
        if node.func.id == "factorial" and (args and (args[0] > 1000 or args[0] < 0)):
            raise _Unsafe("that factorial is too large")
        return _FUNCS[node.func.id](*args)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval_node(x) for x in node.elts]
    raise _Unsafe("I can't evaluate that expression")


def calculate(expression: str) -> float | int:
    """Evaluate arithmetic safely. Raises _Unsafe or SyntaxError on bad input."""
    expr = expression.strip().rstrip("=").strip()
    # Accept spoken and typed forms: '×', '÷', '^', 'x' between numbers.
    expr = (expr.replace("×", "*").replace("÷", "/").replace("^", "**")
                .replace("−", "-"))
    # Strip thousands separators only. A blanket comma removal would turn
    # max(3,9,2) into max(392), so the comma must be flanked by a digit and a
    # group of exactly three.
    expr = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expr)
    expr = re.sub(r"(?<=\d)\s*x\s*(?=[\d(])", "*", expr, flags=re.I)
    expr = re.sub(r"\b(plus|and)\b", "+", expr, flags=re.I)
    expr = re.sub(r"\bminus\b", "-", expr, flags=re.I)
    expr = re.sub(r"\b(times|multiplied by)\b", "*", expr, flags=re.I)
    expr = re.sub(r"\b(divided by|over)\b", "/", expr, flags=re.I)
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", expr, flags=re.I)
    expr = re.sub(r"(?<=\d)%", "/100", expr)
    if len(expr) > 400:
        raise _Unsafe("that expression is too long")
    return _eval_node(ast.parse(expr, mode="eval"))


def _calculate(a: dict, _c) -> Result:
    raw = (a.get("expression") or "").strip()
    if not raw:
        return Result.fail("Calculate what?")
    try:
        value = calculate(raw)
    except _Unsafe as e:
        return Result.fail(str(e))
    except SyntaxError:
        return Result.fail(f"I couldn't parse '{raw}' as arithmetic.")
    except ZeroDivisionError:
        return Result.fail("That's a division by zero.")
    except (ValueError, OverflowError) as e:
        return Result.fail(f"That doesn't compute: {e}")
    if isinstance(value, float):
        shown = f"{value:,.6f}".rstrip("0").rstrip(".") if abs(value) < 1e15 else f"{value:.6g}"
    else:
        shown = f"{value:,}"
    return Result.say(shown, detail=f"{raw} = {shown}", data=value)


# ── units, from defined constants ───────────────────────────────────────────
# (dimension, factor to the dimension's base unit)
UNITS: dict[str, tuple[str, float]] = {
    # length, base metre
    "m": ("length", 1.0), "km": ("length", 1000.0), "cm": ("length", 0.01),
    "mm": ("length", 0.001), "um": ("length", 1e-6), "nm": ("length", 1e-9),
    "mi": ("length", 1609.344), "yd": ("length", 0.9144), "ft": ("length", 0.3048),
    "in": ("length", 0.0254), "nmi": ("length", 1852.0), "ly": ("length", 9.4607304725808e15),
    # mass, base kilogram
    "kg": ("mass", 1.0), "g": ("mass", 0.001), "mg": ("mass", 1e-6),
    "lb": ("mass", 0.45359237), "oz": ("mass", 0.028349523125),
    "st": ("mass", 6.35029318), "t": ("mass", 1000.0),
    # volume, base litre
    "l": ("volume", 1.0), "ml": ("volume", 0.001), "cl": ("volume", 0.01),
    "gal": ("volume", 3.785411784), "qt": ("volume", 0.946352946),
    "pt": ("volume", 0.473176473), "cup": ("volume", 0.2365882365),
    "floz": ("volume", 0.0295735295625), "m3": ("volume", 1000.0),
    # time, base second
    "s": ("time", 1.0), "ms": ("time", 0.001), "min": ("time", 60.0),
    "h": ("time", 3600.0), "day": ("time", 86400.0), "week": ("time", 604800.0),
    # speed, base metre/second
    "mps": ("speed", 1.0), "kmh": ("speed", 1000 / 3600), "mph": ("speed", 0.44704),
    "kn": ("speed", 1852 / 3600), "fps": ("speed", 0.3048),
    # data, base byte (decimal and binary kept distinct on purpose)
    "byte": ("data", 1.0), "bit": ("data", 0.125),
    "kb": ("data", 1e3), "mb": ("data", 1e6), "gb": ("data", 1e9), "tb": ("data", 1e12),
    "kib": ("data", 1024.0), "mib": ("data", 1048576.0),
    "gib": ("data", 1073741824.0), "tib": ("data", 1099511627776.0),
    # area, base square metre
    "m2": ("area", 1.0), "km2": ("area", 1e6), "cm2": ("area", 1e-4),
    "ft2": ("area", 0.09290304), "in2": ("area", 0.00064516),
    "acre": ("area", 4046.8564224), "ha": ("area", 10000.0), "mi2": ("area", 2589988.110336),
}

ALIASES = {
    "metre": "m", "meter": "m", "metres": "m", "meters": "m",
    "kilometre": "km", "kilometer": "km", "kilometres": "km", "kilometers": "km", "kms": "km",
    "centimetre": "cm", "centimeter": "cm", "millimetre": "mm", "millimeter": "mm",
    "mile": "mi", "miles": "mi", "yard": "yd", "yards": "yd",
    "foot": "ft", "feet": "ft", "inch": "in", "inches": "in",
    "nauticalmile": "nmi", "lightyear": "ly", "lightyears": "ly",
    "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg",
    "gram": "g", "grams": "g", "milligram": "mg", "pound": "lb", "pounds": "lb", "lbs": "lb",
    "ounce": "oz", "ounces": "oz", "stone": "st", "tonne": "t", "tonnes": "t", "ton": "t",
    "litre": "l", "liter": "l", "litres": "l", "liters": "l",
    "millilitre": "ml", "milliliter": "ml", "gallon": "gal", "gallons": "gal",
    "quart": "qt", "pint": "pt", "cups": "cup", "fluidounce": "floz", "ounces_fluid": "floz",
    "second": "s", "seconds": "s", "sec": "s", "minute": "min", "minutes": "min",
    "hour": "h", "hours": "h", "hr": "h", "days": "day", "weeks": "week",
    "mps_alias": "mps", "m/s": "mps", "km/h": "kmh", "kph": "kmh", "kmph": "kmh",
    "mileperhour": "mph", "milesperhour": "mph", "knot": "kn", "knots": "kn", "ft/s": "fps",
    "bytes": "byte", "bits": "bit", "kilobyte": "kb", "megabyte": "mb",
    "gigabyte": "gb", "terabyte": "tb", "kibibyte": "kib", "mebibyte": "mib",
    "gibibyte": "gib", "tebibyte": "tib",
    "sqm": "m2", "m^2": "m2", "sqft": "ft2", "ft^2": "ft2", "sqkm": "km2",
    "hectare": "ha", "hectares": "ha", "acres": "acre", "sqmi": "mi2",
    "celsius": "c", "centigrade": "c", "°c": "c", "fahrenheit": "f", "°f": "f",
    "kelvin": "k", "°k": "k",
}
TEMPS = {"c", "f", "k"}


def normalise_unit(raw: str) -> str:
    u = raw.strip().lower().replace(" ", "")
    u = ALIASES.get(u, u)
    if u in UNITS or u in TEMPS:
        return u
    if u.endswith("s"):                       # plural not covered by an alias
        singular = ALIASES.get(u[:-1], u[:-1])
        if singular in UNITS or singular in TEMPS:
            return singular
    return u


def _to_celsius(v: float, unit: str) -> float:
    return {"c": v, "f": (v - 32) * 5 / 9, "k": v - 273.15}[unit]


def _from_celsius(c: float, unit: str) -> float:
    return {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[unit]


def convert(value: float, src: str, dst: str) -> tuple[float, str]:
    """Convert between units. Returns (value, ''); on failure (0, reason)."""
    a, b = normalise_unit(src), normalise_unit(dst)
    if a in TEMPS or b in TEMPS:
        if a not in TEMPS or b not in TEMPS:
            return 0.0, f"I can't convert {src} to {dst} — one is a temperature."
        return _from_celsius(_to_celsius(value, a), b), ""
    if a not in UNITS:
        return 0.0, f"I don't know the unit '{src}'."
    if b not in UNITS:
        return 0.0, f"I don't know the unit '{dst}'."
    dim_a, fa = UNITS[a]
    dim_b, fb = UNITS[b]
    if dim_a != dim_b:
        return 0.0, f"{src} measures {dim_a} and {dst} measures {dim_b}."
    return value * fa / fb, ""


def _convert_units(a: dict, _c) -> Result:
    raw = (a.get("value"), a.get("from"), a.get("to"))
    # Also accept one phrase: "12 miles to km".
    if a.get("expression") and not all(raw):
        m = re.match(r"\s*(-?[\d.]+)\s*([^\s]+)\s*(?:to|in|as|into)\s*([^\s]+)\s*$",
                     str(a["expression"]), re.I)
        if not m:
            return Result.fail("Say it like '12 miles to km'.")
        raw = (m.group(1), m.group(2), m.group(3))
    value, src, dst = raw
    if value in (None, "") or not src or not dst:
        return Result.fail("Give me a value, a unit to convert from, and one to convert to.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return Result.fail(f"'{value}' isn't a number.")
    out, why = convert(value, str(src), str(dst))
    if why:
        return Result.fail(why)
    shown = f"{out:,.6f}".rstrip("0").rstrip(".") if abs(out) < 1e12 else f"{out:.6g}"
    return Result.say(f"{shown} {normalise_unit(str(dst))}",
                      detail=f"{value} {src} = {shown} {dst}", data=out)


# ── definitions ─────────────────────────────────────────────────────────────
def _define(a: dict, _c) -> Result:
    word = (a.get("word") or "").strip()
    if not word or not re.fullmatch(r"[A-Za-z'\- ]{1,60}", word):
        return Result.fail("Define which word?")
    url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(word.strip())
    try:
        entries = json.loads(_fetch(url, timeout=12))
    except Exception:
        return Result.fail(f"I couldn't reach a dictionary for '{word}'.")
    if not isinstance(entries, list) or not entries:
        return Result.fail(f"No dictionary entry for '{word}'.")
    lines, first = [], ""
    for entry in entries[:2]:
        for meaning in entry.get("meanings", [])[:3]:
            part = meaning.get("partOfSpeech", "")
            for d in meaning.get("definitions", [])[:2]:
                text = d.get("definition", "").strip()
                if not text:
                    continue
                lines.append(f"({part}) {text}")
                first = first or text
    if not first:
        return Result.fail(f"That entry had no usable definition for '{word}'.")
    return Result.say(f"{word}: {first}", detail="\n".join(lines), data=lines)


def skills() -> list[Skill]:
    return [
        Skill(
            "weather", "Current weather for a place, or the configured home city.",
            _weather, parameters={"location": {"type": "string"}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "forecast", "Multi-day weather forecast.", _forecast,
            parameters={"location": {"type": "string"}, "days": {"type": "integer"}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "news_briefing", "Current headlines, optionally on a topic.", _news,
            parameters={"topic": {"type": "string"}, "count": {"type": "integer"}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "calculate",
            "Evaluate an arithmetic expression exactly. Supports + - * / // % **, "
            "percentages, and functions like sqrt, log, sin, min, max.",
            _calculate,
            parameters={"expression": {"type": "string", "required": True}},
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "convert_units",
            "Convert between units of length, mass, volume, time, speed, data, "
            "area or temperature.",
            _convert_units,
            parameters={
                "value": {"type": "number"}, "from": {"type": "string"},
                "to": {"type": "string"},
                "expression": {"type": "string", "description": "e.g. '12 miles to km'"},
            },
            risk=Risk.READ_ONLY,
        ),
        Skill(
            "define", "Look up a word's definition.", _define,
            parameters={"word": {"type": "string", "required": True}}, risk=Risk.READ_ONLY,
        ),
    ]
