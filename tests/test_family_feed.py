import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import family_feed as ff  # noqa: E402

NOW = dt.datetime(2026, 10, 13, 1, 30, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 10, 13)


def row(ts, **event):
    return {"version": "v1", "timestamp": ts, "event": event}


def users(n, prefix="u"):
    return [f"{prefix}{i:02d}" + "0" * 6 for i in range(n)]


def responses(device_count=8, sermon_devices=6, qt_devices=2):
    """합성 응답. 기호 →는 7대, ✝는 5대, θ는 4대(미달). 말씀 (43,3)은 5대, (19,23)은 4대."""
    u = users(device_count)
    sym = [row("2026-09-20T00:00:00.000+09:00", u=u[i], glyph="→", n=2) for i in range(7)]
    sym += [row("2026-09-20T00:00:00.000+09:00", u=u[i], glyph="✝︎", n=1) for i in range(5)]
    sym += [row("2026-09-20T00:00:00.000+09:00", u=u[i], glyph="θ", n=9) for i in range(4)]
    notes = []
    for i in range(sermon_devices):  # 일요일(2026-10-04) 오전 11시
        notes.append(row("2026-10-04T11:00:00.000+09:00", u=u[i], kind="sermon", n=2))
    notes.append(row("2026-10-07T20:00:00.000+09:00", u=u[0], kind="sermon", n=1))  # 수요일 저녁
    for i in range(qt_devices):
        notes.append(row("2026-10-05T06:00:00.000+09:00", u=u[i], kind="qt", n=1))
    verses = [row("2026-10-04T00:00:00.000+09:00", u=u[i], book="43", chapter="3", n=1) for i in range(5)]
    verses += [row("2026-10-04T00:00:00.000+09:00", u=u[i], book="19", chapter="23", n=3) for i in range(4)]
    songs = [row("2026-10-04T00:00:00.000+09:00", u=u[i], song="101", n=1) for i in range(5)]
    types = [row("2026-10-04T00:00:00.000+09:00", u=u[i], type="ESTJ", n=1) for i in range(5)]
    types.append(row("2026-10-06T00:00:00.000+09:00", u=u[0], type="INFP", n=1))  # 나중 값이 이긴다
    return {"symbols": {"result": sym}, "notes": {"result": notes}, "verses": {"result": verses},
            "songs": {"result": songs}, "types": {"result": types}}


def build(resp, exclude=()):
    return ff.build({k: ff.events(v) for k, v in resp.items()}, TODAY, list(exclude), NOW)


class Parsing(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(ff.to_kst("2026-09-19T10:00:00.000+09:00").hour, 10)
        self.assertEqual(ff.to_kst("2026-09-19T01:00:00Z").hour, 10)
        self.assertEqual(ff.to_kst("2026-09-19T01:00:00+0000").hour, 10)
        with self.assertRaises(ValueError):
            ff.to_kst("어제")

    def test_response_shapes(self):
        a = {"result": [{"timestamp": "t", "event": {"u": "x"}}]}  # MCP 모양
        b = {"result": {"rows": [{"timestamp": "t", "event": {"u": "x"}}], "type": "groupByResult"}}  # REST 모양
        c = {"result": {"rows": [{"timestamp": "t", "result": [{"u": "x"}, {"u": "y"}]}]}}  # topN 모양
        self.assertEqual(len(ff.events(a)), 1)
        self.assertEqual(len(ff.events(b)), 1)
        self.assertEqual(len(ff.events(c)), 2)
        self.assertEqual(ff.events({}), [])


class Rules(unittest.TestCase):
    def test_five_device_minimum(self):
        v1, v2 = build(responses())
        self.assertEqual(v2["symbolsTop"], ["→", "✝︎"])  # θ 4대는 빠진다
        self.assertEqual(v2["versesTop"], [{"book": 43, "chapter": 3}])  # (19,23) 4대는 빠진다
        self.assertEqual(v2["songsTop"], [101])
        self.assertNotIn("qt", v2["hours"])  # QT 2대
        self.assertIn("sermon", v2["hours"])

    def test_exclusion_prefix_drops_below_minimum(self):
        # u00~u04 중 하나를 빼면 말씀 (43,3)이 4대가 된다
        _, v2 = build(responses(), exclude=["u00"])
        self.assertEqual(v2["versesTop"], [])
        self.assertEqual(v2["songsTop"], [])

    def test_ranking_ties(self):
        resp = responses()
        extra = [row("2026-09-20T00:00:00.000+09:00", u=u, glyph="★", n=1) for u in users(8)[:5]]
        resp["symbols"]["result"] += extra
        v1, _ = build(resp)
        self.assertEqual(v1["symbolsTop"], ["→", "✝︎", "★"])  # 5대 동률: ✝(횟수 5)·★(5) → 레일 순서 ✝ 먼저

    def test_hours_sum_to_one_and_weekday(self):
        v1, v2 = build(responses())
        row24 = v2["hours"]["sermon"]
        self.assertEqual(len(row24), 24)
        self.assertAlmostEqual(sum(row24), 1.0, places=6)
        self.assertEqual(v2["sermonTopWeekday"], "sun")
        self.assertEqual(v1["sermonTopWeekday"], "sun")

    def test_hours_largest_remainder(self):
        for counts in ([1] * 24, [1] + [0] * 23, [7, 3, 3] + [0] * 21, list(range(1, 25))):
            self.assertAlmostEqual(sum(ff.ratios_sum_to_one(counts)), 1.0, places=6)

    def test_types_latest_wins_and_minimum(self):
        _, v2 = build(responses())
        self.assertEqual(v2["types"]["devices"], 5)
        self.assertEqual(v2["types"]["top"], [])  # ESTJ 4대 + INFP 1대 → 5대 이상인 글자가 없다
        self.assertEqual(v2["types"]["axes"]["E"], 0.8)
        resp = responses()
        resp["types"]["result"] = resp["types"]["result"][:4]
        _, v2b = build(resp)
        self.assertNotIn("types", v2b)  # 4대면 키가 없다

    def test_window_and_month(self):
        v1, v2 = build(responses())
        self.assertEqual(v2["window"], {"from": "2026-09-13", "to": "2026-10-12", "days": 30})
        self.assertEqual(v2["month"], "2026-10")
        self.assertEqual(v1["schema"], 1)
        for key in ("versesTop", "songsTop", "types"):
            self.assertNotIn(key, v1)

    def test_sanity_gate(self):
        resp = responses()
        resp["notes"] = {"result": []}
        with self.assertRaises(ff.SanityError):
            build(resp)

    def test_queries_filter(self):
        queries = ff.feed_queries(TODAY)
        self.assertEqual(set(queries), {"symbols", "notes", "verses", "songs", "types"})
        for body in queries.values():
            dims = [f["dimension"] for f in body["filter"]["fields"]]
            self.assertEqual(dims[:2], ["appID", "isTestMode"])
            self.assertEqual(body["intervals"], ["2026-09-12T15:00:00Z/2026-10-12T15:00:00Z"])


class Validation(unittest.TestCase):
    def test_built_docs_pass(self):
        v1, v2 = build(responses())
        ff.validate(v1)
        ff.validate(v2)

    def test_bad_docs_fail(self):
        _, good = build(responses())
        for mutate in (
            lambda d: d.update(schema=3),
            lambda d: d.update(month="2026-13"),
            lambda d: d.update(symbolsTop=["a", "b", "c", "d"]),
            lambda d: d["hours"].update(sermon=[0.5, 0.5]),
            lambda d: d.update(versesTop=[{"book": 67, "chapter": 1}]),
            lambda d: d.update(songsTop=[0]),
            lambda d: d.update(types={"devices": 4, "axes": {"E": 0, "S": 0, "T": 0, "J": 0}, "top": []}),
        ):
            doc = json.loads(json.dumps(good))
            mutate(doc)
            with self.assertRaises(ValueError):
                ff.validate(doc)


class Cli(unittest.TestCase):
    def test_failure_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "v2.json")
            with open(old, "w") as handle:
                handle.write("어제 파일")
            fixture = os.path.join(tmp, "fixture.json")
            resp = responses()
            resp["notes"] = {"result": []}  # 비정상 조회
            with open(fixture, "w") as handle:
                json.dump(resp, handle)
            self.assertEqual(ff.main(["--fixture", fixture, "--out-dir", tmp, "--today", "2026-10-13"]), 1)
            with open(old) as handle:
                self.assertEqual(handle.read(), "어제 파일")

    def test_dry_run_writes_nothing_and_run_writes_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = os.path.join(tmp, "fixture.json")
            with open(fixture, "w") as handle:
                json.dump(responses(), handle)
            args = ["--fixture", fixture, "--out-dir", tmp, "--today", "2026-10-13"]
            self.assertEqual(ff.main(args + ["--dry-run"]), 0)
            self.assertEqual(sorted(os.listdir(tmp)), ["fixture.json"])
            self.assertEqual(ff.main(args), 0)
            self.assertEqual(sorted(os.listdir(tmp)), ["fixture.json", "v1.json", "v2.json"])

    def test_missing_token(self):
        os.environ.pop("TELEMETRYDECK_TOKEN", None)
        self.assertEqual(ff.main(["--today", "2026-10-13"]), 2)


if __name__ == "__main__":
    unittest.main()
