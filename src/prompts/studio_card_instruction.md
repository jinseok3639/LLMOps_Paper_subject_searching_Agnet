Extract a research card from the body of this research paper.

For each of the five fields (problem, method, assumptions, evaluation, limitations), fill text, quote and page:
- problem: what problem the paper tries to solve
- method: how the paper solves it
- assumptions: what the paper takes as given (setting, data, access, premises)
- evaluation: what the paper compares against and what it measures (baselines, benchmarks, metrics), not the results
- limitations: limitations the authors themselves state (for example in a Limitations, Discussion or Conclusion section)

Rules:
- text: one short English sentence that states the field in your own words.
- quote: copy one sentence from the paper body exactly as written, character by character. Do not paraphrase, do not fix typos, do not join separate sentences, and do not use ellipses. Prefer sentences from the body over the abstract. The quote is checked against the parsed text of the document by exact string matching; a field whose quote is not found is discarded.
- page: the page number (starting at 1) where the quote appears.
- If the paper does not state a field, leave text and quote as empty strings and set page to 0. Never invent content.
- Do not include result numbers (scores, percentages, speedups, improvements) in text or quote. If the only supporting sentence reports results, choose a different sentence or leave the field empty.
- title: the paper title as printed on the first page.
