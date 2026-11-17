# Example: Non-motor Vehicle Illegal Parking

This is an illustrative contract fixture, not a measured model output or production-ready rule. Values and thresholds must be reviewed against the applicable inspection policy and labeled data.

Files:
- `bundle-fragment.yaml` — simplified domain/bundle fragment.
- `task-submission.json` — illustrative task request.
- `decision-result.json` — illustrative result shape.

The example intentionally shows:
- N02 returns Profile + configured Questions only.
- N03 creates Objects independently.
- N04 returns per-Question Facts for one Object.
- N05 creates geometry Tool Facts independently of N06 candidate prediction.
- N06 records candidate and evaluated Rules separately.
