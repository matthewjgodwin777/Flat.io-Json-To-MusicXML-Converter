#!/usr/bin/env python3
"""
flat_to_musicxml.py — Flat.io JSON (score-partwise) → MusicXML 4.0 partwise.

Fixes in this version:
  * guard against empty `beam: {}`
  * normalize tie / slur / tied / beam / dynamics to lists
  * whitelist dynamic names
  * optional narration classifier (NARRATOR_PREFIXES)
  * optional swing preservation
  * emit trailing directions at last note timePos, not after measure
"""

import json
import sys
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom

NARRATOR_PREFIXES = ("Narrator:",)          # classify these as narration
VALID_DYNAMICS = {
    "p", "pp", "ppp", "pppp", "ppppp", "pppppp",
    "f", "ff", "fff", "ffff", "fffff", "ffffff",
    "mp", "mf", "sf", "sfp", "sfpp", "fp", "rf", "rfz", "sfz", "sffz",
    "fz", "n", "pf", "sfzp",
}


def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag)
    for k, v in attrs.items():
        el.set(k, str(v))
    if text is not None:
        el.text = str(text)
    return el


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _classify_words(text):
    """Return 'narration' or 'marking' or 'instruction'."""
    if not text:
        return "marking"
    for p in NARRATOR_PREFIXES:
        if text.startswith(p):
            return "narration"
    if re.match(r"^[A-Z]{2,4}:\s", text):    # "ATB: ...", "SA: ..."
        return "instruction"
    return "marking"


# --------------------------------------------------------------------------

def convert(score_data, separate_narration=False):
    sp = score_data["score-partwise"]
    root = ET.Element("score-partwise", version="4.0")

    if "work" in sp and "work-title" in sp["work"]:
        work = sub(root, "work")
        sub(work, "work-title", sp["work"]["work-title"])

    for credit in sp.get("credit", []):
        cr = sub(root, "credit", page="1")
        if "credit-type" in credit:
            sub(cr, "credit-type", credit["credit-type"])
        if "credit-words" in credit:
            sub(cr, "credit-words", credit["credit-words"])

    if "identification" in sp:
        ident_src = sp["identification"]
        ident = sub(root, "identification")
        enc = ident_src.get("encoding", {})
        enc_el = sub(ident, "encoding")
        if "software" in enc:
            sub(enc_el, "software", enc["software"])
        if "encoding-date" in enc:
            sub(enc_el, "encoding-date", enc["encoding-date"])
        if "source" in ident_src:
            sub(ident, "source", ident_src["source"])

    part_list = sub(root, "part-list")
    for pmeta in sp["part-list"]["score-part"]:
        sp_el = sub(part_list, "score-part", id=pmeta["$id"])
        sub(sp_el, "part-name", pmeta["part-name"])
        if "part-abbreviation" in pmeta:
            sub(sp_el, "part-abbreviation", pmeta["part-abbreviation"])
        if "score-instrument" in pmeta:
            si = pmeta["score-instrument"]
            si_el = sub(sp_el, "score-instrument", id=si["$id"])
            sub(si_el, "instrument-name", si["instrument-name"])
        if "midi-instrument" in pmeta:
            mi = pmeta["midi-instrument"]
            mi_el = sub(sp_el, "midi-instrument", id=mi["$id"])
            if "midi-channel" in mi:
                sub(mi_el, "midi-channel", mi["midi-channel"])
            if "midi-program" in mi:
                sub(mi_el, "midi-program", str(mi["midi-program"]))
            if "volume" in mi:
                sub(mi_el, "volume", mi["volume"])

    for part in sp["part"]:
        staves = 1
        for m in part["measure"]:
            for attr in m.get("attributes", []):
                if "staves" in attr:
                    staves = max(staves, int(attr["staves"]))
        part_el = sub(root, "part", id=part["$id"])
        state = {"divisions": None, "key": None, "time": None,
                 "clefs": {}, "staves": None}
        for measure in part["measure"]:
            convert_measure(part_el, measure, staves, state, separate_narration)

    return root


def convert_measure(part_el, measure, staves, state, separate_narration):
    measure_el = sub(part_el, "measure", number=measure["$number"])
    emit_attributes(measure_el, measure, staves, state)

    directions = sorted(
        (d for d in measure.get("direction", []) if "$adagio-location" in d),
        key=lambda d: d["$adagio-location"].get("timePos", 0),
    )
    harmonies = sorted(
        (h for h in measure.get("harmony", []) if "$adagio-location" in h),
        key=lambda h: h["$adagio-location"].get("timePos", 0),
    )

    cur = {"dir": 0, "harm": 0}

    def emit_pending(upto):
        while cur["dir"] < len(directions):
            d = directions[cur["dir"]]
            if d["$adagio-location"].get("timePos", 0) <= upto:
                emit_direction(measure_el, d, separate_narration)
                cur["dir"] += 1
            else:
                break
        while cur["harm"] < len(harmonies):
            h = harmonies[cur["harm"]]
            if h["$adagio-location"].get("timePos", 0) <= upto:
                emit_harmony(measure_el, h, staves)
                cur["harm"] += 1
            else:
                break

    snd = measure.get("sound")
    for s in _as_list(snd):
        emit_sound(measure_el, s)

    for note in measure.get("note", []):
        t = note.get("$adagio-location", {}).get("timePos", 0)
        if "chord" not in note:
            emit_pending(t)
        if "backup" in note:
            b = sub(measure_el, "backup")
            sub(b, "duration", str(note["backup"]["duration"]))
        emit_note(measure_el, note, staves)

    emit_pending(float("inf"))

    if "barline" in measure:
        bl = measure["barline"]
        bl_el = sub(measure_el, "barline", location=bl["$location"])
        sub(bl_el, "bar-style", bl["bar-style"])


def emit_attributes(parent, measure, staves, state):
    attrs_list = measure.get("attributes", [])
    if not attrs_list:
        return
    attrs_el = None

    def ensure():
        nonlocal attrs_el
        if attrs_el is None:
            attrs_el = sub(parent, "attributes")
        return attrs_el

    for attr in attrs_list:
        if "divisions" in attr:
            d = int(attr["divisions"])
            if d != state["divisions"]:
                sub(ensure(), "divisions", str(d))
                state["divisions"] = d
        if "key" in attr:
            k = str(attr["key"]["fifths"])
            if k != state["key"]:
                key_el = sub(ensure(), "key")
                sub(key_el, "fifths", k)
                state["key"] = k
        if "time" in attr:
            t = (attr["time"]["beats"], attr["time"]["beat-type"])
            if t != state["time"]:
                tm = sub(ensure(), "time")
                sub(tm, "beats", t[0])
                sub(tm, "beat-type", t[1])
                state["time"] = t
        if "staves" in attr:
            st = int(attr["staves"])
            if st != state["staves"]:
                sub(ensure(), "staves", str(st))
                state["staves"] = st
        for c in _as_list(attr.get("clef")):
            num = int(c.get("$number", 1))
            new = (c.get("sign"), c.get("line"), c.get("clef-octave-change"))
            if new != state["clefs"].get(num):
                a = {"number": str(num)} if staves > 1 else {}
                clef_el = sub(ensure(), "clef", **a)
                sub(clef_el, "sign", c["sign"])
                if "line" in c:
                    sub(clef_el, "line", c["line"])
                if "clef-octave-change" in c:
                    sub(clef_el, "clef-octave-change", c["clef-octave-change"])
                state["clefs"][num] = new


def emit_note(parent, note, staves):
    n = sub(parent, "note")
    if "chord" in note:
        sub(n, "chord")
    if "rest" in note:
        sub(n, "rest")
    elif "pitch" in note:
        p = note["pitch"]
        pe = sub(n, "pitch")
        sub(pe, "step", p["step"])
        if "alter" in p:
            sub(pe, "alter", p["alter"])
        sub(pe, "octave", p["octave"])
    sub(n, "duration", note["duration"])
    for t in _as_list(note.get("tie")):
        sub(n, "tie", type=t["$type"])
    if "voice" in note:
        sub(n, "voice", note["voice"])
    if "type" in note:
        sub(n, "type", note["type"])
    if "dot" in note:
        sub(n, "dot")
    if "accidental" in note:
        sub(n, "accidental", note["accidental"])
    if staves > 1 and "staff" in note:
        sub(n, "staff", note["staff"])
    for b in _as_list(note.get("beam")):
        if isinstance(b, dict) and "content" in b:
            sub(n, "beam", b["content"], number=b.get("$number", "1"))

    notations = note.get("notations")
    if notations:
        not_el = sub(n, "notations")
        wrote = False
        for t in _as_list(notations.get("tied")):
            sub(not_el, "tied", type=t["$type"])
            wrote = True
        for s in _as_list(notations.get("slur")):
            a = {"type": s["$type"], "number": s.get("$number", "1")}
            if "$placement" in s:
                a["placement"] = s["$placement"]
            sub(not_el, "slur", **a)
            wrote = True
        if "fermata" in notations:
            sub(not_el, "fermata")
            wrote = True
        if not wrote:
            n.remove(not_el)

    for lyric in _as_list(note.get("lyric")):
        le = sub(n, "lyric", number=lyric.get("$number", "1"))
        if "syllabic" in lyric:
            sub(le, "syllabic", lyric["syllabic"])
        if "text" in lyric:
            sub(le, "text", lyric["text"])
        if "extend" in lyric:
            a = {}
            if "$type" in lyric["extend"]:
                a["type"] = lyric["extend"]["$type"]
            sub(le, "extend", **a)


def emit_direction(parent, direction, separate_narration):
    d = sub(parent, "direction")
    if "$placement" in direction:
        d.set("placement", direction["$placement"])
    dt = direction.get("direction-type", {})
    dt_el = sub(d, "direction-type")

    if "dynamics" in dt:
        dyn = sub(dt_el, "dynamics")
        for name in dt["dynamics"]:
            if name in VALID_DYNAMICS:
                sub(dyn, name)

    if "words" in dt:
        w = sub(dt_el, "words", dt["words"])
        if separate_narration and _classify_words(dt["words"]) == "narration":
            w.set("font-style", "italic")
            w.set("enclosure", "none")
            d.set("placement", "above")

    if "wedge" in dt:
        sub(dt_el, "wedge", type=dt["wedge"]["$type"])
    if "metronome" in dt:
        m = dt["metronome"]
        me = sub(dt_el, "metronome")
        sub(me, "beat-unit", m["beat-unit"])
        sub(me, "per-minute", m["per-minute"])
    if "$adagio-tempoChange" in dt:
        sub(dt_el, "words", dt["$adagio-tempoChange"].get("$type", ""))

    if "staff" in direction:
        sub(d, "staff", direction["staff"])
    if "sound" in direction and "$dynamics" in direction["sound"]:
        sub(d, "sound", dynamics=direction["sound"]["$dynamics"])


def emit_harmony(parent, harmony, staves):
    h = sub(parent, "harmony")
    if "$placement" in harmony:
        h.set("placement", harmony["$placement"])
    if "root" in harmony:
        r = sub(h, "root")
        sub(r, "root-step", harmony["root"]["root-step"])
        if "root-alter" in harmony["root"]:
            sub(r, "root-alter", str(harmony["root"]["root-alter"]))
    kind = harmony.get("kind") or harmony.get("$adagio-kind") or "major"
    sub(h, "kind", kind)
    if "bass" in harmony:
        b = sub(h, "bass")
        sub(b, "bass-step", harmony["bass"]["bass-step"])
        if "bass-alter" in harmony["bass"]:
            sub(b, "bass-alter", str(harmony["bass"]["bass-alter"]))
    if "degree" in harmony:
        deg = harmony["degree"]
        de = sub(h, "degree")
        sub(de, "degree-value", str(deg["degree-value"]))
        sub(de, "degree-alter", str(deg["degree-alter"]))
        sub(de, "degree-type", deg["degree-type"])
    if staves > 1 and "staff" in harmony:
        sub(h, "staff", harmony["staff"])


def emit_sound(parent, sound):
    a = {}
    if "$tempo" in sound:
        a["tempo"] = str(sound["$tempo"])
    if "$dynamics" in sound:
        a["dynamics"] = sound["$dynamics"]
    if a:
        sub(parent, "sound", **a)


# --------------------------------------------------------------------------

def to_musicxml_string(root):
    xml_bytes = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding=None)
    lines = pretty.split("\n")
    body = "\n".join(lines[1:]) if lines and lines[0].startswith("<?xml") else pretty
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
        '"http://www.musicxml.org/dtds/partwise.dtd">\n'
    )
    return header + body


def main():
    if len(sys.argv) < 2:
        print("Usage: python flat_to_musicxml.py input.json [output.musicxml] [--narration]")
        sys.exit(1)
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") \
          else inp.rsplit(".", 1)[0] + ".musicxml"
    separate = "--narration" in sys.argv

    with open(inp, "r", encoding="utf-8") as f:
        data = json.load(f)
    root = convert(data, separate_narration=separate)
    with open(out, "w", encoding="utf-8") as f:
        f.write(to_musicxml_string(root))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()