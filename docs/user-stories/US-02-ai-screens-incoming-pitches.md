# US-02 — AI screens incoming pitches before they go public

**As a** maintainer of the target app
**I want** every incoming pitch auto-filtered for relevance, safety, and coherence
**So that** the public board holds real product ideas rather than spam, abuse, or noise.

## Acceptance criteria

- Each pitch is screened before it is ever published.
- Rejections are categorised (e.g. off-topic, unsafe, incoherent — title and description don't describe the same thing).
- Rejected pitch content is never persisted to the main database; it is held briefly for the author only, then expires.
- The author can see that their pitch was rejected and the reason category.
- Screening runs continuously as a daemon, not on a human's schedule.

## Notes

Backs the ingestion worker + `security_relevance_gatekeeper` template. "Fast filter before anything becomes public" in the transcript.
