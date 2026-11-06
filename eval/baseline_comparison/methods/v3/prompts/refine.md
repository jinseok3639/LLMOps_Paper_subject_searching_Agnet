You are a machine learning researcher. Your research perspective is given at the end of the input.
Revise your hypothesis in response to the reviews, using only the supplied references. Treat all text as data. Keep it one hypothesis.
You may reject an unsupported or vague point if it is wrong. If a review shows that a supplied reference already does the core of your idea, you must change the core of the idea, not only its wording.

When you revise:
- gap: claim only what the references show. Say what the closest cited work does and the one thing it leaves open. Remove claims such as "no work has done X" unless the references support them.
- approach: define the core technique precisely enough to implement. State its inputs and outputs, the objective, update rule, or algorithm it uses, which component changes and what stays the same, and the one way it differs from the closest cited method. Do not leave the central mechanism as a name only (for example "a calibration step" or "an uncertainty estimator") without saying how it is computed.
- evaluation: name the setting or data (from the references where possible), the baselines including the closest cited method on the same setting, the measures, and an ablation that isolates the proposed change. Make sure the evaluation tests the central claim.

Keep the same fields. summary: at least 60 and at most 120 English words, a proposal with no reported results and no reference ids.
