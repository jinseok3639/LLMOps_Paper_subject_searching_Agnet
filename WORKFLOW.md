# Reference-based research workflow

Branch: `feature/reference-topic-workflow` in the isolated `topic-agent-work` folder.

## Run

From this folder, use an environment with `requirements.txt` installed:

```powershell
..\LLMOps_Paper_subject_searching_Agnet\.venv\Scripts\python.exe -m streamlit run app.py
```

The local `.env` needs `UPSTAGE_API_KEY`; `S2_API_KEY` is optional but recommended for search limits. Secrets are not committed.

## Data

`DB/all.json`, `DB/embeddings.npy`, and `DB/embeddings.json` are the recommendation index. `src.extract` and `src.embed` now write these paths. Search responses are cached daily under `DB/search_cache`. Generated runs, candidates, bibliography, and search traces are saved under `DB/sessions` (ignored by Git).

## Transitions

- Profile submitted: browse.
- Problem selected: explore. Chat remains available before generation.
- Generate clicked: search with profile + conversation snapshot + problem; execute generation; present via conversation agent. State remains explore while working.
- Successful generation: refine. Select a candidate and discuss it; regenerate explicitly to apply conversation changes to search.
- Failed generation: retain previous stage and existing candidates.
- Choose another problem: browse; clear active candidates and selected problem, preserve conversation.
- New chat: clear session state.
- Verify and final remain visibly unavailable until verification is implemented. Free-form model replies never silently change workflow state.

## Generator provenance

Ported from `evaluation_v2/loop/scripts/pipeline.py` and `methods/v1`: four perspectives (assumption, transfer, empirical, formal), three proposals each, cross-review/refinement, slot-wise Swiss tournaments, top three. No overlap or same-topic checks are executed. Review's `already_done` field remains schema-compatible but is explicitly empty. Existing English generation constraints remain; the conversation agent presents candidates in Korean. This is the evaluation_v2 algorithm, with researcher context added to each agent, rather than evaluation samples or pre-generated outputs.

Generation uses many model calls and may take minutes; the right panel and status log report progress. It runs synchronously. Search initially uses title/abstract search (up to 5 queries, one simplified retry for zero hits, up to 40 unique papers with abstracts); relevance quality needs live evaluation. No novelty claims are made.

## Verification

```powershell
..\LLMOps_Paper_subject_searching_Agnet\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Unit tests mock external APIs and cover search context/deduplication, multi-agent execution, invalid support rejection, presenter handoff, run persistence, and UI transitions.
