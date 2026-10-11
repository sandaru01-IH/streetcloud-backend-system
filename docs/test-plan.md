# Test plan

Automated:

- `services/api` pytest: concepts, brief, rubric bounds, image rejection, guest demo through word cloud and fixture scores, cross-user denial, coalesced generations, honest missing vision provider, hosted GPU refusal, pairing expiry and reuse, private lease with a labeled agent result, adapter guards.
- `apps/mobile` `npm test`: word-cloud layout is stable and non-overlapping.
- `npm run typecheck` for the Expo app.
- `ruff check` on the API.

Manual, when a device or key exists:

- Camera capture and library pick on Android and iOS, including consent and EXIF strip.
- OpenAI or Gemini image edit with the model id set only in the API env.
- Vision evaluation on a real session, including a failed run when the key is absent.
- Agent `doctor` and `connect` against a ComfyUI checkpoint after `workflows/manifest.json` `ready` is set true.
- Hosted GPU only after `HOSTED_GPU_ENDPOINT` is set.
- Collaborative invite, second account redeem, batch versus automatic policy.
