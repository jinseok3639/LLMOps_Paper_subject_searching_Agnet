"""4단계: key.json으로 조건을 되돌리고 판정 결과를 표로 모은다. 사람 채점이 있으면 일치율도 낸다.

예: python eval/baseline_comparison/aggregate.py --run pilot
"""
import argparse
from collections import defaultdict
from statistics import mean

import common


def fmt(x):
    return "-" if x is None else f"{x:.2f}"


def avg(xs):
    return mean(xs) if xs else None


def load(run):
    key = common.read_json(run / "key.json")
    results, missing = {}, []
    for jid, job in key.items():
        path = run / "jobs" / jid / "result.json"
        if path.exists():
            results[jid] = common.read_json(path)
        else:
            missing.append(jid)
    return key, results, missing


def scores_table(key, results, lines):
    # per[(sample, cond)][candidate] = {criterion: score}
    per = defaultdict(dict)
    for jid, job in key.items():
        if job["kind"] == "score" and jid in results:
            per[(job["sample"], job["condition"])][job["candidate"]] = {c: results[jid][c]["score"] for c in common.CRITERIA}
    samples = sorted({s for s, _ in per})
    lines += ["## 후보별 점수 (1~5)", "", "조건별로 모든 후보의 평균입니다. best와 worst는 세트 안 후보 평균 점수의 최고와 최저를 샘플별로 구해 평균했습니다.", "",
              "| 조건 | 후보 수 | " + " | ".join(common.CRITERIA) + " | 평균 | best-of-3 | worst-of-3 |", "|---" * (len(common.CRITERIA) + 5) + "|"]
    summary = {}
    for cond in common.CONDITIONS:
        cands = [sc for (s, c), d in per.items() if c == cond for sc in d.values()]
        if not cands:
            continue
        crit = {c: avg([x[c] for x in cands]) for c in common.CRITERIA}
        totals = {s: [avg(list(x.values())) for x in per[(s, cond)].values()] for s in samples if per.get((s, cond))}
        best, worst = avg([max(v) for v in totals.values()]), avg([min(v) for v in totals.values()])
        overall = avg([avg(list(x.values())) for x in cands])
        summary[cond] = {"n": len(cands), **crit, "mean": overall, "best": best, "worst": worst}
        lines.append(f"| {cond} | {len(cands)} | " + " | ".join(fmt(crit[c]) for c in common.CRITERIA) + f" | {fmt(overall)} | {fmt(best)} | {fmt(worst)} |")
    lines += ["", "샘플별 세트 평균 (후보 평균 점수의 평균, 괄호는 최고/최저):", "", "| 샘플 | " + " | ".join(common.CONDITIONS) + " |", "|---" * (len(common.CONDITIONS) + 1) + "|"]
    for s in samples:
        cells = []
        for cond in common.CONDITIONS:
            v = [avg(list(x.values())) for x in per.get((s, cond), {}).values()]
            cells.append(f"{fmt(avg(v))} ({fmt(max(v))}/{fmt(min(v))})" if v else "-")
        lines.append(f"| {s} | " + " | ".join(cells) + " |")
    lines.append("")
    return per, summary


USABLE_MIN = 3  # 항목 1~5가 모두 이 점수 이상이면 "쓸 만한 후보"
STRONG_MIN = 4  # 모두 이 점수 이상이면 "강한 후보". 3점 기준은 모든 조건이 채워 구분이 안 될 수 있다


def final_metrics(run, per, summary, lines):
    """사전에 정한 보조 지표: 쓸 만한 후보 수, 공백 메움 비율, 샘플당 호출 수와 시간."""
    lines += ["## 쓸 만한 후보와 비용", "",
              f"쓸 만한 후보는 항목 1~5가 모두 {USABLE_MIN}점 이상인 후보입니다. usable@3은 쓸 만한 후보가 하나라도 있는 샘플의 비율입니다. 강한 후보는 모두 {STRONG_MIN}점 이상입니다.", "",
              "| 조건 | 세트당 쓸 만한 후보 (0~3) | usable@3 | 세트당 강한 후보 (0~3) | strong@3 | 샘플당 호출 | 샘플당 시간(초) |", "|---|---|---|---|---|---|---|"]
    out = {}
    for cond in common.CONDITIONS:
        sets = [d for (s, c), d in per.items() if c == cond]
        gens = [common.read_json(p) for p in sorted((run / "gen").glob(f"t*_{cond}.json"))]
        gens = [g for g in gens if not g.get("failed")]
        if not sets and not gens:
            continue
        counts = [sum(all(x[c] >= USABLE_MIN for c in common.CRITERIA) for x in d.values()) for d in sets]
        strong = [sum(all(x[c] >= STRONG_MIN for c in common.CRITERIA) for x in d.values()) for d in sets]
        row = {"usable_per_set": avg(counts), "usable_at_3": avg([float(n > 0) for n in counts]) if counts else None,
               "strong_per_set": avg(strong), "strong_at_3": avg([float(n > 0) for n in strong]) if strong else None,
               "calls": avg([g.get("calls") for g in gens if g.get("calls") is not None]), "seconds": avg([g.get("seconds") for g in gens])}
        out[cond] = row
        pct = lambda v: "-" if v is None else f"{v * 100:.0f}%"
        lines.append(f"| {cond} | {fmt(row['usable_per_set'])} | {pct(row['usable_at_3'])} | {fmt(row['strong_per_set'])} | {pct(row['strong_at_3'])} | {fmt(row['calls'])} | {fmt(row['seconds'])} |")
    gap = {}
    for wf in common.WORKFLOW_CONDITIONS:
        if "A" not in summary or wf not in summary:
            continue
        for ref in ("C", "D"):
            if ref in summary and summary[ref]["mean"] != summary["A"]["mean"]:
                gap[f"{wf}/{ref}"] = (summary[wf]["mean"] - summary["A"]["mean"]) / (summary[ref]["mean"] - summary["A"]["mean"])
    if gap:
        lines += ["", "공백 메움 비율 (워크플로우−A)/(기준−A), 후보 평균 점수 기준: "
                  + ", ".join(f"{k.split('/')[0]}의 {k.split('/')[1]} 기준 {v * 100:.0f}%" for k, v in gap.items()),
                  "A가 실패한 샘플이 있으면 조건별 평균의 샘플 구성이 다릅니다. 샘플별 표와 아래의 같은 샘플 비교를 함께 보세요."]
    matched = {}
    for wf in common.WORKFLOW_CONDITIONS:
        both = sorted(s for (s, c) in per if c == "A" and (s, wf) in per)
        if both:
            mean_of = lambda cond: avg([avg(list(x.values())) for s in both for x in per[(s, cond)].values()])
            matched[wf] = {"samples": both, "A": mean_of("A"), wf: mean_of(wf), "diff": mean_of(wf) - mean_of("A")}
            lines += ["", f"같은 샘플 비교 ({', '.join(both)}): A {fmt(matched[wf]['A'])}, {wf} {fmt(matched[wf][wf])}, 차이 {matched[wf]['diff']:+.2f}"]
    lines.append("")
    return {"by_condition": out, "gap_closure": gap, "matched_vs_A": matched}


def novelty_table(key, results, lines):
    by = defaultdict(list)
    for jid, job in key.items():
        if job["kind"] == "novelty" and jid in results:
            by[job["condition"]].append(results[jid]["score"])
    lines += ["## 외부 새로움 (1~5, 웹 검색)", "", "| 조건 | 후보 수 | 평균 | 1점(이미 있음) 개수 |", "|---|---|---|---|"]
    for cond in common.CONDITIONS:
        if by[cond]:
            lines.append(f"| {cond} | {len(by[cond])} | {fmt(avg(by[cond]))} | {sum(x == 1 for x in by[cond])} |")
    lines.append("")
    return dict(by)


def combine(a, b):
    """두 순서의 판정(조건 이름 또는 None=무승부)을 합친다. 서로 다른 조건이 이기면 위치 편향으로 무승부."""
    if a == b:
        return a, False
    if a is None or b is None:
        return a or b, False
    return None, True


def pairwise_table(key, results, lines):
    votes = defaultdict(dict)  # (sample, pair, criterion) -> {order: winner cond or None}
    for jid, job in key.items():
        if job["kind"] != "pairwise" or jid not in results:
            continue
        for c in common.SET_CRITERIA:
            v = results[jid][c]["verdict"]
            votes[(job["sample"], job["pair"], c)][job["X"]] = {"X": job["X"], "Y": job["Y"], "tie": None}[v]
    tally, rows = {}, []
    lines += ["## 세트 쌍대 비교", "", "두 순서의 결과를 합쳤습니다. 서로 다른 조건이 이기면 위치 편향으로 보고 무승부로 셉니다. 한 순서만 있는 경우는 집계하지 않습니다.", "",
              "| 비교 | 항목 | 왼쪽 승 | 무승부 | 오른쪽 승 | 위치 편향 무승부 |", "|---|---|---|---|---|---|"]
    for p, q in common.PAIRS:
        pair = f"{p}-{q}"
        if not any(pr == pair and len(v) == 2 for (_, pr, _), v in votes.items()):
            continue  # 판정하지 않은 쌍은 표에서 뺀다
        for c in common.SET_CRITERIA:
            t = {"left": 0, "tie": 0, "right": 0, "position": 0}
            for (s, pr, cr), v in votes.items():
                if pr != pair or cr != c or len(v) < 2:
                    continue
                win, conflict = combine(v[p], v[q])
                t["position"] += conflict
                t["left" if win == p else "right" if win == q else "tie"] += 1
                rows.append({"sample": s, "pair": pair, "criterion": c, "winner": win, "position_conflict": conflict})
            tally[(pair, c)] = t
            lines.append(f"| {p} vs {q} | {c} | {p} {t['left']} | {t['tie']} | {q} {t['right']} | {t['position']} |")
    lines.append("")
    return rows


def human_table(run, key, per, lines):
    path = run / "human" / "scores.json"
    if not path.exists():
        return None
    human = [h for h in common.read_json(path) if all(isinstance(h.get(c), int) for c in common.CRITERIA)]
    if not human:
        return None
    pairs = []
    for item in human:
        job = key[item["job"]]
        judge = per.get((job["sample"], job["condition"]), {}).get(job["candidate"])
        if judge and all(isinstance(item.get(c), int) for c in common.CRITERIA):
            pairs.append((item, judge))
    lines += ["## 사람 검증", "", f"사람이 채점한 후보 {len(pairs)}개와 심판 점수를 비교했습니다.", "", "| 항목 | 완전 일치 | ±1 일치 |", "|---|---|---|"]
    out = {}
    for c in common.CRITERIA:
        if pairs:
            exact = sum(h[c] == j[c] for h, j in pairs) / len(pairs)
            near = sum(abs(h[c] - j[c]) <= 1 for h, j in pairs) / len(pairs)
            out[c] = {"exact": exact, "within1": near}
            lines.append(f"| {c} | {exact:.0%} | {near:.0%} |")
    lines.append("")
    return out


def short(text, n=400):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "..."


def details(run, key, results):
    """샘플, 조건, 후보별로 후보 내용, 판정 점수와 근거, 작업 번호를 모은다. 결과가 이상할 때 원인을 찾는 용도."""
    gen = {}
    for path in sorted((run / "gen").glob("t*_?.json")):
        rec = common.read_json(path)
        gen[(rec["sample"], rec["condition"])] = rec
    jobs = defaultdict(dict)  # (sample, cond, candidate) -> {kind: jid}
    pair_jobs = defaultdict(list)  # sample -> [jid]
    for jid, job in key.items():
        if job["kind"] == "pairwise":
            pair_jobs[job["sample"]].append(jid)
        else:
            jobs[(job["sample"], job["condition"], job["candidate"])][job["kind"]] = jid
    lines = [f"# 상세 결과: {run.name}", "",
             "후보마다 내용, 심판 점수와 근거, 작업 번호를 적었습니다. 작업 폴더(`jobs/<번호>/`)에는 심판이 받은 입력, `codex_*.log`, `result.json`이 있습니다.",
             "생성 과정은 `gen/<샘플>_<조건>.json`(시도별 원문 응답), `gen/<샘플>_B_trace.json`(B의 제안, 검토, 토너먼트 전체), `progress.log`, `calllog/`에 있습니다.", ""]
    for sample in sorted({s for s, _ in gen}):
        lines += [f"## {sample}", ""]
        for cond in common.CONDITIONS:
            rec = gen.get((sample, cond))
            if rec is None:
                continue
            head = f"### {sample} / {cond}  (`gen/{sample}_{cond}.json`, {rec.get('seconds')}초, 호출 {rec.get('calls')})"
            if rec.get("failed"):
                lines += [head, "", f"생성 실패: {rec.get('error')}", ""]
                continue
            lines += [head, ""]
            for i, cand in enumerate(rec["candidates"]):
                ids = jobs.get((sample, cond, i), {})
                lines += [f"#### 후보 {i + 1}: {cand['title']}", "",
                          f"- gap: {short(cand['gap'], 300)}", f"- approach: {short(cand['approach'], 300)}",
                          f"- evaluation: {short(cand['evaluation'], 300)}", f"- support: {', '.join(cand['support'])}", ""]
                sj, nj = ids.get("score"), ids.get("novelty")
                if sj in results:
                    r = results[sj]
                    lines.append(f"점수 (`{sj}`): " + ", ".join(f"{c} {r[c]['score']}" for c in common.CRITERIA))
                    lines += [f"- {c}: {short(r[c]['rationale'])}" for c in common.CRITERIA]
                else:
                    lines.append(f"점수: 없음 (`{sj}`)")
                if nj in results:
                    r = results[nj]
                    close = "; ".join(f"{c['title']} ({c['year']}) {c['url']}" for c in r["closest"][:3])
                    lines += ["", f"외부 새로움 (`{nj}`): {r['score']}. {short(r['rationale'])}", f"- 가장 가까운 연구: {close or '없음'}"]
                else:
                    lines.append(f"외부 새로움: 없음 (`{nj}`)")
                lines.append("")
        lines += [f"### {sample} 쌍대 비교", ""]
        for jid in sorted(pair_jobs.get(sample, []), key=lambda j: (key[j]["pair"], key[j]["X"])):
            job = key[jid]
            if jid not in results:
                lines += [f"- `{jid}` X={job['X']}, Y={job['Y']}: 결과 없음", ""]
                continue
            r = results[jid]
            verdicts = ", ".join(f"{c} {({'X': job['X'], 'Y': job['Y'], 'tie': '무'})[r[c]['verdict']]}" for c in common.SET_CRITERIA)
            best = {side: f"{job[side]} 후보 {job['ids'][r['strongest'][side]] + 1}" for side in ("X", "Y")}
            worst = {side: f"{job[side]} 후보 {job['ids'][r['weakest'][side]] + 1}" for side in ("X", "Y")}
            lines += [f"**`{jid}` X={job['X']}, Y={job['Y']}**: {verdicts}",
                      f"- 최고: {best['X']} / {best['Y']}, 최저: {worst['X']} / {worst['Y']}",
                      f"- overall 근거: {short(r['overall']['rationale'], 600)}",
                      f"- diversity 근거: {short(r['diversity']['rationale'], 400)}", ""]
    (run / "details.md").write_text("\n".join(lines), "utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    args = ap.parse_args()

    run = common.run_dir(args.run)
    key, results, missing = load(run)
    lines = [f"# 기준선 비교 결과: {args.run}", "", ", ".join(f"{c}: {common.LABELS[c]}" for c in common.CONDITIONS if any((run / "gen").glob(f"t*_{c}.json"))) + ". 주 비교는 A vs B이고 나머지는 추가·참고 결과입니다. 판정은 Codex(GPT) 하나가 했습니다.", "",
             f"판정 {len(results)}/{len(key)}개 완료." + (f" 남은 작업: {', '.join(sorted(missing))}" if missing else ""), ""]
    failed = [p.stem for p in sorted((run / "gen").glob("t*_?.json")) if common.read_json(p).get("failed")]
    if failed:
        lines += [f"생성 실패: {', '.join(failed)}", ""]
    per, summary = scores_table(key, results, lines)
    final = final_metrics(run, per, summary, lines)
    novelty = novelty_table(key, results, lines)
    pairwise = pairwise_table(key, results, lines)
    human = human_table(run, key, per, lines)
    details(run, key, results)
    lines += ["후보별 내용, 판정 근거, 작업 번호는 `details.md`에 있습니다.", ""]
    (run / "report.md").write_text("\n".join(lines), "utf-8")
    common.write_json(run / "report.json", {"scores": summary, "final": final, "novelty": novelty, "pairwise": pairwise, "human": human, "missing": missing})
    print("\n".join(lines))


if __name__ == "__main__":
    main()
