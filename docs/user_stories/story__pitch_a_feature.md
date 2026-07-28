<!-- pdd-story-prompts: config_python.prompt, constants_python.prompt, features_python.prompt, ingestion_service_python.prompt -->
<!-- pdd-story-dev-units: config_python.prompt, constants_python.prompt, features_python.prompt, ingestion_service_python.prompt -->

# User Story: pitch_a_feature

**ID:** US-01

## Story

As a community member of the target app,
I want to submit a feature idea in a short form (title + description),
so that my idea enters the public queue instead of dying in a chat thread.

## Acceptance criteria

- A submit form accepts a title and a description and returns immediately (`202`) with a `feature_id`.
- The pitch is queued for AI screening; it does not appear on the public board until it survives screening.
- Submission requires no account setup ceremony — an identified user is enough.
- A per-author daily pitch limit (Pitch Coins) prevents flooding; exceeding it returns a clear "out of coins" response, not a silent drop.
- The wallet holds five coins and its remaining balance is visible before the author starts typing, so the cost of a pitch is never a surprise.
- When the wallet empties, the pitch control names the wait instead of just going dead: it shows a live countdown to the refill and the balance restores on its own. For the hackathon demo the refill is compressed to two minutes so the cycle is visible inside a short session; the counter is client-side and the backend's rate limit remains the only real one.
- The author sees a pending state for their pitch while screening runs.
- Obviously malformed input is refused at submission, before any AI is involved: text outside the length bounds, invisible control characters, and embedded HTML or script markup. The author gets a clear message and keeps their Pitch Coin — a rejected submission never used a screening attempt.

## Notes

Backs `POST /api/features`, `submit_modal`, and the `feature_intake` Redis queue.

The criteria added for the forum UI (coin balance and countdown, stage naming, the four
sections, stage filtering, Vault search, the private pitches dialog, the preview's refresh and
loading states) are **presentation-layer only**. They are satisfied by `frontend/src/**` against
the API as it already stands — `view`, `sort`, `status`, `q`, `cursor` are existing query
parameters and `429` is the existing rate-limit response. No backend or orchestration prompt
acquires an obligation from them, and none needs regenerating.
