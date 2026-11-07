"""B 워크플로우의 단계별 판정: 어느 단계에서 품질이 오르고 떨어지는지 본다.

저장된 B 기록(gen/<샘플>_B_trace.json)으로 처음 제안과 수정본을 같은 루브릭으로 채점하고,
토너먼트 순위와 최종 선별을 GPT 점수와 비교한다. 추가 생성 호출은 없다.

  pack    runs/<run>/stage/ 아래에 블라인드 점수 작업을 만든다 (제안, 수정본 모두 같은 형식이라 단계가 드러나지 않음)
  report  판정 결과로 runs/<run>/stage/stage_report.md를 쓴다

예:
  python eval/baseline_comparison/stage_eval.py pack --run dev_v3
  python eval/baseline_comparison/judge.py --run dev_v3/stage --kinds score --workers 3
  python eval/baseline_comparison/stage_eval.py report --run dev_v3 --ref-run dev_v2
"""
import argparse
import random
from collections import defaultdict
from statistics import mean

import common
from src.multi_agent import corpus_text

STAGES = ("proposed", "refined")  # trace 후보의 hypothesis(처음 제안), refined(검토 후 수정본)


def traces(run):
    return {p.name.split("_")[0]: common.read_json(p) for p in sorted((run / "gen").glob("t*_B_trace.json"))}


def pack(run, force=False):
    stage = run / "stage"
    if (stage / "key.json").exists() and not force:
        raise SystemExit("이미 포장했습니다. 판정 결과를 지우고 다시 만들려면 --force")
    jobs = []
    for sample, t in traces(run).items():
        for c in t["candidates"]:
            for st, field in zip(STAGES, ("hypothesis", "refined")):
                jobs.append({"kind": "score", "sample": sample, "stage": st, "candidate": c["id"],
                             "refine_failed": bool(c.get("refine_failed")), "files": {"candidate.json": common.normalize(c[field])}})
    random.Random(f"stage-{run.name}").shuffle(jobs)
    refs_text = {s: corpus_text(common.refs(s)) for s in {j["sample"] for j in jobs}}
    key = {}
    for n, job in enumerate(jobs, 1):
        jid = f"s{n:03d}"
        folder = stage / "jobs" / jid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "instructions.md").write_text((common.PROMPTS / "judge_score.md").read_text("utf-8"), "utf-8")
        (folder / "references.txt").write_text(refs_text[job["sample"]], "utf-8")
        common.write_json(folder / "schema.json", common.judge_schema("score"))
        for name, data in job.pop("files").items():
            common.write_json(folder / name, data)
        key[jid] = job
    common.write_json(stage / "key.json", key)
    print(f"단계별 점수 작업 {len(jobs)}개: {stage}")


def fmt(x, signed=False):
    if x is None:
        return "-"
    return f"{x:+.2f}" if signed else f"{x:.2f}"


def avg(xs):
    xs = [x for x in xs if x is not None]
    return mean(xs) if xs else None


def spearman(xs, ys):
    """동점은 평균 순위로 처리한 스피어만 상관."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def collect(run):
    """반환: {sample: {"trace": t, "scores": {(stage, cid): {criterion: score}}, "rationale": {...}}}"""
    stage = run / "stage"
    key = common.read_json(stage / "key.json")
    data, missing = defaultdict(lambda: {"scores": {}, "jobs": {}}), []
    for jid, job in key.items():
        path = stage / "jobs" / jid / "result.json"
        if not path.exists():
            missing.append(jid)
            continue
        res = common.read_json(path)
        data[job["sample"]]["scores"][(job["stage"], job["candidate"])] = {c: res[c]["score"] for c in common.CRITERIA}
        data[job["sample"]]["jobs"][(job["stage"], job["candidate"])] = jid
    for sample, t in traces(run).items():
        data[sample]["trace"] = t
    return data, missing, len(key)


def analyse(sample, d):
    t, sc = d["trace"], d["scores"]
    total = lambda st, cid: avg(list(sc[(st, cid)].values())) if (st, cid) in sc else None
    ids = [c["id"] for c in t["candidates"]]
    prop = {i: total("proposed", i) for i in ids}
    ref = {i: total("refined", i) for i in ids}
    # ② 수정 효과: 같은 후보의 수정 전후. 수정이 실패해 원본을 쓴 후보는 뺀다.
    failed = {c["id"] for c in t["candidates"] if c.get("refine_failed")}
    paired = [i for i in ids if prop[i] is not None and ref[i] is not None and i not in failed]
    crit_diff = {c: avg([sc[("refined", i)][c] - sc[("proposed", i)][c] for i in paired]) for c in common.CRITERIA}
    diffs = [ref[i] - prop[i] for i in paired]
    # ③ 토너먼트: 경기 승패가 GPT 점수(수정본)의 높낮이와 맞는지, 전체 순위 상관
    agree = disagree = undecided = 0
    for rounds in t["rounds"].values():
        for matches in rounds:
            for m in matches:
                ga, gb = ref.get(m["a"]), ref.get(m["b"])
                if m["points_a"] == m["points_b"] or ga is None or gb is None or ga == gb:
                    undecided += 1
                elif (m["points_a"] > m["points_b"]) == (ga > gb):
                    agree += 1
                else:
                    disagree += 1
    scored = [i for i in ids if ref[i] is not None]
    rho = spearman([t["scores"][i] for i in scored], [ref[i] for i in scored])
    # ④⑤ 선별: 실제 선택, 토너먼트 상위 3개(거르기 전), GPT 기준 상위 3개(이상적 선택)
    n = common.N_CANDIDATES
    ranked = sorted(t["scores"], key=lambda i: (-t["scores"][i], i))
    chosen = avg([ref[i] for i in t["chosen"]])
    top_tournament = avg([ref[i] for i in ranked[:n]])
    oracle = avg(sorted([ref[i] for i in scored], reverse=True)[:n])
    return {"n": len(ids), "proposed_mean": avg(prop.values()), "proposed_best": max([v for v in prop.values() if v is not None], default=None),
            "refined_mean": avg(ref.values()), "refined_best": max([v for v in ref.values() if v is not None], default=None),
            "refine_diff": avg(diffs), "refine_wins": sum(x > 0 for x in diffs), "refine_ties": sum(x == 0 for x in diffs),
            "refine_losses": sum(x < 0 for x in diffs), "refine_failed": len(failed), "crit_diff": crit_diff,
            "match_agree": agree, "match_disagree": disagree, "match_undecided": undecided, "rank_corr": rho,
            "chosen": chosen, "top_tournament": top_tournament, "oracle": oracle,
            "selection_loss": None if oracle is None or chosen is None else oracle - chosen,
            "dedup_cost": None if top_tournament is None or chosen is None else top_tournament - chosen,
            "chosen_ids": t["chosen"], "tournament_top": ranked[:n], "oracle_ids": sorted(scored, key=lambda i: -ref[i])[:n],
            "per_candidate": {i: {"proposed": prop[i], "refined": ref[i], "tournament": t["scores"][i]} for i in ids}}


def report(run, ref_run=None):
    data, missing, n_jobs = collect(run)
    res = {s: analyse(s, d) for s, d in sorted(data.items()) if d["scores"]}
    samples = list(res)
    col = lambda k: avg([res[s][k] for s in samples])
    L = [f"# 단계별 판정: {run.name}", "",
         "B 워크플로우의 처음 제안과 수정본을 같은 루브릭(항목 1~5 평균)으로 채점하고, 토너먼트와 최종 선별을 그 점수와 비교했습니다. "
         "점수는 이 단계별 판정 안에서만 비교합니다(본 판정과는 심판 실행이 다름).", "",
         f"판정 {n_jobs - len(missing)}/{n_jobs}개 완료." + (f" 남은 작업: {', '.join(sorted(missing))}" if missing else ""), ""]
    if ref_run:
        rep = common.run_dir(ref_run) / "report.json"
        if rep.exists():
            ref = common.read_json(rep)["scores"]
            L += [f"참고: `{ref_run}` 본 판정의 조건별 후보 평균 / best-of-3: " +
                  ", ".join(f"{c} {fmt(v['mean'])} / {fmt(v['best'])}" for c, v in ref.items()), ""]
    L += ["## 단계별 흐름", "",
          "| 샘플 | ① 제안 평균 | ① 제안 최고 | ② 수정 평균 | ② 수정 최고 | ④ 이상적 3개 | ⑤ 거르기 전 토너먼트 상위 3개 | 최종 3개 |",
          "|---|---|---|---|---|---|---|---|"]
    for s in samples:
        r = res[s]
        L.append(f"| {s} | {fmt(r['proposed_mean'])} | {fmt(r['proposed_best'])} | {fmt(r['refined_mean'])} | {fmt(r['refined_best'])} | "
                 f"{fmt(r['oracle'])} | {fmt(r['top_tournament'])} | {fmt(r['chosen'])} |")
    L.append(f"| 평균 | {fmt(col('proposed_mean'))} | {fmt(col('proposed_best'))} | {fmt(col('refined_mean'))} | {fmt(col('refined_best'))} | "
             f"{fmt(col('oracle'))} | {fmt(col('top_tournament'))} | {fmt(col('chosen'))} |")
    L += ["", "- ④ 이상적 3개: 수정본 중 GPT 점수 상위 3개(다양성 무시). 최종 3개와의 차이가 선별 손실입니다.",
          "- ⑤ 토너먼트 상위 3개: 같은 주제 거르기 전 순위. 최종 3개와의 차이가 거르기 비용입니다(음수면 거르기가 오히려 점수를 올림).", "",
          "## 단계별 손익", "", "| 샘플 | ② 수정 효과 (승/무/패) | ③ 경기 일치 (일치/불일치/판단 불가) | ③ 순위 상관 | ④ 선별 손실 | ⑤ 거르기 비용 |",
          "|---|---|---|---|---|---|"]
    for s in samples:
        r = res[s]
        L.append(f"| {s} | {fmt(r['refine_diff'], True)} ({r['refine_wins']}/{r['refine_ties']}/{r['refine_losses']}"
                 + (f", 수정 실패 {r['refine_failed']}개 제외" if r["refine_failed"] else "") + ") | "
                 f"{r['match_agree']}/{r['match_disagree']}/{r['match_undecided']} | {fmt(r['rank_corr'], True)} | "
                 f"{fmt(r['selection_loss'], True)} | {fmt(r['dedup_cost'], True)} |")
    L.append(f"| 평균 | {fmt(col('refine_diff'), True)} | {sum(res[s]['match_agree'] for s in samples)}/{sum(res[s]['match_disagree'] for s in samples)}/"
             f"{sum(res[s]['match_undecided'] for s in samples)} | {fmt(col('rank_corr'), True)} | {fmt(col('selection_loss'), True)} | {fmt(col('dedup_cost'), True)} |")
    L += ["", "- ③ 경기 일치: 토너먼트 경기의 승자가 GPT 점수도 더 높은 비율. 동점 경기나 GPT 동점은 판단 불가로 셉니다.",
          "- ③ 순위 상관: 토너먼트 점수와 GPT 점수의 스피어만 상관(샘플 안 후보 전체). 0 근처면 Solar 토너먼트가 GPT 기준 품질을 못 가립니다.", "",
          "## 수정 단계의 항목별 변화 (수정본 − 제안)", "", "| 샘플 | " + " | ".join(common.CRITERIA) + " |", "|---" * (len(common.CRITERIA) + 1) + "|"]
    for s in samples:
        L.append(f"| {s} | " + " | ".join(fmt(res[s]["crit_diff"][c], True) for c in common.CRITERIA) + " |")
    L.append("| 평균 | " + " | ".join(fmt(avg([res[s]["crit_diff"][c] for s in samples]), True) for c in common.CRITERIA) + " |")
    L += ["", "## 후보별 점수", ""]
    for s in samples:
        r, jobs = res[s], data[s]["jobs"]
        L += [f"### {s}", "", f"최종 선택 {r['chosen_ids']}, 토너먼트 상위 {r['tournament_top']}, GPT 상위 {r['oracle_ids']}", "",
              "| 후보 | 제안 | 수정본 | 변화 | 토너먼트 점수 | 작업(제안/수정본) |", "|---|---|---|---|---|---|"]
        order = sorted(r["per_candidate"], key=lambda i: -(r["per_candidate"][i]["refined"] or 0))
        for i in order:
            p = r["per_candidate"][i]
            d = None if p["proposed"] is None or p["refined"] is None else p["refined"] - p["proposed"]
            mark = " ✓" if i in r["chosen_ids"] else ""
            L.append(f"| {i}{mark} | {fmt(p['proposed'])} | {fmt(p['refined'])} | {fmt(d, True)} | {p['tournament']:g} | "
                     f"{jobs.get(('proposed', i), '-')}/{jobs.get(('refined', i), '-')} |")
        L.append("")
    L += ["판정 근거는 `stage/jobs/<작업>/result.json`, 후보 원문과 검토 의견은 `gen/<샘플>_B_trace.json`에 있습니다.", ""]
    out = run / "stage"
    (out / "stage_report.md").write_text("\n".join(L), "utf-8")
    common.write_json(out / "stage_report.json", {s: {k: v for k, v in r.items()} for s, r in res.items()})
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=("pack", "report"))
    ap.add_argument("--run", required=True)
    ap.add_argument("--ref-run", default="", help="report에 참고로 붙일 본 판정 회차 (예: dev_v2)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    run = common.run_dir(args.run)
    if args.action == "pack":
        pack(run, args.force)
    else:
        report(run, args.ref_run or None)


if __name__ == "__main__":
    main()
