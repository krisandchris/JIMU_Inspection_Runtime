# Offline evaluation

`jimu_runtime.evaluation.score` scores a frozen holdout using an independently
approved TruthSnapshot, ScoringProtocol, and TrialSource. The source comes from
an authenticated runtime exporter with access to accepted inputs and complete
attempt journals. The exporter supplies a separately pinned inventory for
each sample, including the exact accepted Task, Execution input digest,
ModelInvocation, ToolExecution, Decision, and Evidence records. The scorer
reconciles the supplied PredictionSnapshot and its journal records with that
inventory. TrialSource is a required trusted input. The scorer performs
no file or network I/O and relies on the host for export completeness,
authentication, storage, and retention.

```python
report = score(
    dataset,
    truth,
    predictions,
    protocol,
    source=runtime_export,
    expected_source=approved_source_pin,
    expected_truth=approved_truth_pin,
    expected_protocol=approved_protocol_pin,
)
report_pin = report.pin()
```

Use `model_validate_json`, `model_json_schema`, and `model_dump_json` to read,
generate schemas for, and write the versioned contracts. Pins use SHA-256
over RFC 8785 canonical JSON. Identical pinned inputs yield identical
reports. Reading a report rechecks its input-based identity, sample
uniqueness, and aggregate counts against the details.

Run the synthetic example from the repository root with the locked CPU
environment selected.

```bash
PYTHONPATH=src:tests python -m fixtures.wp06.evaluation_sample
```

## Scoring rules

- The manifest fixes one client and project, original-image dimensions,
  image hashes, split assignments, and known deduplication groups. Scoring
  accounts for every holdout sample exactly once in truth, predictions, and
  the trusted source. The preparer supplies all training and calibration
  samples to screen for overlap and derives the deduplication groups.
- Truth fixes the annotation catalogue independently of the IoU threshold.
  The host exports exact Profile-to-Question, geometry, and Rule bindings
  from the admitted Bundle. Supply the selected canonical Fact Schema
  artifact bytes as UTF-8 when those bytes differ from the canonical
  validated definition. The scorer verifies the Fact Schema artifact
  identity and derived child reference. It validates independently labeled
  and known predicted values against the exact object Fact Schema. Rule
  results must match the persisted Decision for that Execution, context,
  Profile, and Object.
- Profile recall uses independently labeled presence claims. A labeled
  object establishes Profile presence. An empty complete Profile
  annotation confirms absence for the frozen catalogue. Grounding recall
  uses all labeled boxes. The scorer matches boxes one to one within each
  exact Profile. It visits pairs at or above the protocol threshold in
  descending exact IoU, then ascending label and prediction IDs. Mean IoU
  covers matched pairs. Every unmatched prediction is retained. Only a
  frozen presence annotation confirming absence or an explicit complete
  grounding annotation establishes an unmatched prediction as a false
  positive.
- Question, geometry, and Rule coverage count comparable results over all
  corresponding labels. Accuracy counts correct results over comparable
  results. Non-known results and unmatched objects remain visible. Rule
  comparisons use the evidence-checked predicate recorded by N06.
- Task violation precision uses labeled violation predictions. Recall
  and erroneous release rate use all task-level labeled violations.
  Automatic release risk uses observed automatic clears with task-level
  truth. Automatic release coverage uses inputs with a recorded release
  disposition. When some dispositions are unobserved, the report adds
  observation coverage over all inputs and separate counts of unobserved
  dispositions, confirmed erroneous releases, and violating cases with
  unobserved release. Erroneous release rate is undefined if any labeled
  violation lacks release status. An uncertain workflow decision alone
  establishes neither a release nor a manual review.
- Task category precision and recall use cases with explicitly complete
  category truth and categories derived from matched, pinned Rule paths.
  Category mismatches retain their false positive and missed positive
  counts through the precision and recall denominators, even when binary
  detection is correct. A category outside the approved catalogue is
  rejected. Cases without category truth do not contribute to category
  metrics, and their predicted category assignments are counted separately.
- For `no_hits`, the confirmed target-miss lower bound divides known misses
  by all `no_hits` samples. The labeled miss rate divides by `no_hits`
  samples with independent target truth. Missing target truth is counted
  separately. Each zero denominator yields a null value.
- Tool success uses the complete, reconciled inventory of physically
  attempted Tool executions. The report retains partial, failed,
  not-dispatched, and unknown implementation resolutions. Provider
  identity and observed resolution are preserved with each invocation digest.

## Human review

Use `ReviewHistory.append_vote` and `ReviewHistory.adjudicate` to append new
immutable revisions. Votes identify the sample, dataset, annotation
catalogue, and review. Adjudication identifies the exact preceding review
and voted content. A vote or open review cannot withdraw an existing
adjudicated label. An independent adjudicator may explicitly abstain to
withdraw it. The host retains earlier truth and review revisions and
coordinates concurrent publication.

`BadCaseAttribution` binds to a specific report, sample, and evidence from
that sample's inventory. Independent human attachments are outside the
current attribution contract.

The synthetic example verifies scoring behavior. Real-data quality,
statistical confidence intervals, acceptance thresholds, and automatic
clearance policy require an authorized evaluation.


## Historical export

`HistoricalEvaluationExporter` opens a scope-authorized list of immutable
Execution pins. It uses a read-only repeatable PostgreSQL snapshot, resolves
the approved historical Release against its admission receipt and recorded
capabilities, then reconciles published records with all admitted attempt
receipts. It validates retained response and selected Evidence bytes through
the scoped media store. The export includes its archive witnesses and binds
their digests into `TrialSource`. Reading a portable historical export
recomputes and checks every witness digest. Authorization and retention
remain the responsibility of the headless host.

Current runtime history contains no external automatic clearance observation,
so historical projections use null status. The additive fields are omitted
from existing V1 reports where all release statuses were observed. Such
reports keep their previous serialized bytes and content pins. Legacy source
snapshots may omit history witnesses. Historical exports require them.
