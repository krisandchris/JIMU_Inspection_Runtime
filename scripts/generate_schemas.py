"""Engineering Schema inventory for the WP-01 domain contracts."""

import sys
from collections.abc import Callable

# This must precede every source import: --check is read-only, including pyc.
sys.dont_write_bytecode = True


def _bootstrap() -> tuple[tuple[object, ...], Callable[[], int]]:
    """Import/generate only after the CLI no-bytecode precondition is installed."""
    import argparse
    import hashlib
    import json
    import os
    import re
    from dataclasses import dataclass, replace
    from pathlib import Path
    from types import ModuleType
    from typing import Literal, cast
    from urllib.parse import unquote

    from jsonschema import Draft202012Validator
    from pydantic import TypeAdapter
    from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
    from pydantic_core import core_schema

    from jimu_runtime.domain import (
        conditions,
        definition_enums,
        definition_support,
        enums,
        fact_selectors,
        policies,
        primitives,
        producers,
        references,
        schema_types,
        tool_mappings,
        values,
    )
    from jimu_runtime.domain.definitions import (
        capabilities,
        geometry,
        models,
        ontology,
        prompts,
        resources,
        tools,
    )
    from jimu_runtime.domain.fact_schemas import models as fact_schemas
    from jimu_runtime.domain.nodes import (
        context,
        decision,
        discovery,
        envelopes,
        grounding,
        observation,
    )
    from jimu_runtime.domain.nodes import tool_execution as tool_node
    from jimu_runtime.domain.rules import definitions as rule_definitions
    from jimu_runtime.domain.rules import operators, type_results
    from jimu_runtime.domain.runtime import (
        applicability,
        authority,
        bundle_manifest,
        candidates,
        coverage,
        decisions,
        evidence,
        executions,
        facts,
        intake,
        invocations,
        media,
        observations,
        perception,
        releases,
        replay,
        selection,
        tool_executions,
    )
    from jimu_runtime.domain.validation import applicability as applicability_inputs
    from jimu_runtime.domain.validation import authority as authority_checks
    from jimu_runtime.domain.validation import (
        authority_inputs,
        condition_inputs,
        coverage_sources,
        decision_coverage,
        definition_inputs,
        issues,
        producer_authority_inputs,
        producer_inputs,
        review_inputs,
        rule_type_inputs,
        runtime_authority,
        tool_plans,
    )
    from jimu_runtime.domain.validation import policies as policy_inputs

    DIALECT = "https://json-schema.org/draft/2020-12/schema"
    STATUS = "COMPLETE_WP01C_CONTRACTS"
    REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
    type Role = Literal[
        "foundation",
        "definition_root",
        "runtime_root",
        "definition_component",
        "runtime_component",
        "named_union",
        "nonpersistent_input",
        "nonpersistent_output",
    ]

    @dataclass(frozen=True)
    class Export:
        module: ModuleType
        symbol: str
        filename: str
        role: Role
        revision: Literal["1.0.0", "1.1.0", "1.2.0", "1.3.0"] | None = None
        supersedes: str | None = None

        @property
        def source_symbol(self) -> str:
            return f"{self.module.__name__}.{self.symbol}"

    def _group(module: ModuleType, names: str, role: Role) -> tuple[Export, ...]:
        # Names are explicitly listed below, never supplied by files or CLI data.
        stem = module.__name__.removeprefix("jimu_runtime.domain.")
        return tuple(
            Export(module, name, f"{stem}.{name}.schema.json", role)
            for name in names.split()
        )

    WP01B_INVENTORY: tuple[Export, ...] = (
        *_group(primitives, "ArtifactId RecordId Version Digest", "foundation"),
        *_group(
            references,
            "ContentRef IdentitySelector RuntimeRecordRef SchemaRef",
            "foundation",
        ),
        *_group(
            enums,
            "ArtifactKind RuntimeRecordKind TaskStatus ExecutionStatus NodeRunStatus AttemptStatus FactStatus DecisionResult",
            "foundation",
        ),
        *_group(
            values,
            "BooleanValue IntegerValue NumberValue StringValue EnumValue ListValue RecordValue BooleanTypeDefinition IntegerTypeDefinition NumberTypeDefinition StringTypeDefinition EnumTypeDefinition ListTypeDefinition RecordTypeDefinition RecordFieldDefinition ValueContract",
            "foundation",
        ),
        *_group(values, "Value TypeDefinition", "named_union"),
        *_group(
            definition_enums,
            "ConditionInputDomain ComparisonOperator ProducerCardinality",
            "foundation",
        ),
        *_group(conditions, "ConditionExpr", "named_union"),
        *_group(conditions, "LocalToolFactSelector", "nonpersistent_input"),
        *_group(conditions, "ConditionReferences", "nonpersistent_output"),
        *_group(
            definition_support,
            "UTCDateTime FieldPath ReviewReportRef ExternalCapabilityRequirement VocabularyMemberRef VersionConstraint ContextFieldSelector AssetDimensions",
            "nonpersistent_input",
        ),
        *_group(ontology, "EntityDefinition RegionDefinition", "definition_root"),
        *_group(
            resources, "VocabularyDefinition StaticAssetDescriptor", "definition_root"
        ),
        *_group(
            capabilities,
            "FacetDefinition QuestionDefinition ProfileDefinition",
            "definition_root",
        ),
        *_group(capabilities, "GroundingInputSource GroundingBinding", "named_union"),
        *_group(prompts, "PromptTemplateDefinition", "definition_root"),
        *_group(models, "ModelBinding", "definition_root"),
        *_group(tools, "ToolDefinition ToolPlanDefinition", "definition_root"),
        *_group(fact_schemas, "FactSchemaDefinition", "definition_root"),
        *_group(
            fact_schemas,
            "GlobalTypeLimits ValueSchemaGenerationSpec ValueSchemaGenerationInput",
            "nonpersistent_input",
        ),
        *_group(fact_schemas, "GeneratedValueSchema", "nonpersistent_output"),
        *_group(
            fact_schemas,
            "UnitMetadata CoordinateMetadata ConfidenceMetadataPolicy",
            "named_union",
        ),
        *_group(fact_selectors, "FactSelector", "nonpersistent_input"),
        *_group(fact_selectors, "ProducerCatalogueContentRef", "foundation"),
        *_group(producers, "ProducerCatalogue", "definition_root"),
        *_group(producers, "FactProducerDeclaration", "definition_component"),
        *_group(producers, "Producer ActivationCondition", "named_union"),
        *_group(fact_selectors, "ProducerSelector", "named_union"),
        *_group(schema_types, "FieldSelector ResolvedFieldType", "nonpersistent_input"),
        *_group(schema_types, "SchemaDescriptor", "named_union"),
        *_group(
            tool_mappings, "InputBinding MappingTypeContext", "nonpersistent_input"
        ),
        *_group(tool_mappings, "InputSource ContextTypeRegistration", "named_union"),
        *_group(review_inputs, "ReviewMetadata", "nonpersistent_input"),
        *_group(review_inputs, "ReviewScope ReviewAssessment", "named_union"),
        *_group(
            policies,
            "QuestionSelectionPolicyArtifact UnknownPolicyArtifact QuestionValidationPolicyArtifact EvidencePolicyArtifact ToolExecutionPolicyArtifact ToolFailurePolicyArtifact ResourceRequirementsArtifact ProviderPolicyArtifact FallbackPolicyArtifact RulePolicyArtifact ConflictPolicyArtifact CoveragePolicyArtifact ContextProvenancePolicyArtifact ExecutionPolicyArtifact ApplicabilityPolicyArtifact ToolSkipPolicyArtifact",
            "definition_root",
        ),
        *_group(
            policies,
            "ResourceRequirements ProviderPolicy FallbackPolicy SelectionCallPolicy NonExpressionPolicyArtifact PolicyArtifact",
            "named_union",
        ),
        *_group(
            policy_inputs,
            "PolicyRegistration ModelBindingRegistration PromptRegistration RetentionVocabularyRegistration",
            "nonpersistent_input",
        ),
        *_group(tool_plans, "ToolRegistration", "nonpersistent_input"),
        *_group(
            producer_inputs,
            "ProducerCatalogueRegistration SystemCapabilityRegistration ProducerValidationContext ProducerCatalogueContext",
            "nonpersistent_input",
        ),
        *_group(
            producer_authority_inputs,
            "ExecutionDefinitionPin ProducerAuthorityValidationInput",
            "nonpersistent_input",
        ),
        *_group(
            condition_inputs,
            "ApplicabilityConditionContext ToolStepGuardConditionContext ProducerActivationConditionContext",
            "nonpersistent_input",
        ),
        *_group(condition_inputs, "ConditionReferenceContext", "named_union"),
        *_group(condition_inputs, "ConditionReferenceResult", "nonpersistent_output"),
        *_group(
            facts,
            "KnownFact UnknownFact ErrorFact NotApplicableFact NotEvaluatedFact",
            "runtime_root",
        ),
        *_group(facts, "FactRecord FactSource", "named_union"),
        *_group(
            authority,
            "ObservationBatchSelection ToolStepSelection SystemOutputSelection",
            "runtime_root",
        ),
        *_group(authority, "AuthoritativeResultSelection", "named_union"),
        *_group(
            authority_inputs,
            "ObjectDescriptor NodeRunDescriptor AttemptDescriptor QuestionSelectionDescriptor ObservationResultDescriptor ToolStepResultDescriptor SystemOutputResultDescriptor FactCandidateSubmission AuthorityQuestionInput AuthorityToolPlanInput",
            "nonpersistent_input",
        ),
        *_group(authority_inputs, "AttemptOwner MappedOutputResult", "named_union"),
        *_group(
            candidates,
            "CandidateParseLimits CandidateSnapshotLimits CandidateSnapshot",
            "nonpersistent_input",
        ),
        *_group(
            candidates,
            "CandidateParseOutcome RejectedPrior CandidateAppendOutcome",
            "named_union",
        ),
        *_group(authority_checks, "LocalAuthorityHistoryInput", "nonpersistent_input"),
        *_group(authority_checks, "ResultDescriptor", "named_union"),
        *_group(
            issues,
            "ValidationIssue CheckResult ValidationReport",
            "nonpersistent_output",
        ),
        *_group(definition_inputs, "DefinitionRegistration", "named_union"),
        *_group(
            definition_inputs,
            "DefinitionValidationInput ProfileUseContext ModelPromptUse",
            "nonpersistent_input",
        ),
    )

    WP01C_INVENTORY: tuple[Export, ...] = (
        *_group(
            rule_definitions, "RuleDefinition RuleSetDefinition", "definition_root"
        ),
        *_group(rule_definitions, "RuleContentRef", "foundation"),
        *_group(operators, "OperatorDescriptor", "definition_component"),
        *_group(operators, "OperatorId", "named_union"),
        *_group(type_results, "TypeCheckResult", "named_union"),
        *_group(
            rule_type_inputs,
            "RuleConditionContext RuleTypeBindings",
            "nonpersistent_input",
        ),
        *_group(rule_type_inputs, "RuleTypeUseContext", "named_union"),
        *_group(geometry, "GeometryResourceDefinition", "definition_root"),
        *_group(geometry, "GeometryCoordinate", "named_union"),
        *_group(
            geometry,
            "GeometryPoint PolygonGeometry GeometryProvenance",
            "definition_component",
        ),
        *_group(bundle_manifest, "BundleManifest ChecksumManifest", "runtime_root"),
        *_group(bundle_manifest, "BundleRef", "foundation"),
        *_group(
            bundle_manifest,
            "ArtifactInventoryEntry SourceProvenance SourceArtifactRef CompatibilityRequirement IntegrityEnvelope DetachedSignatureRef ChecksumEntry",
            "runtime_component",
        ),
        *_group(
            releases,
            "AdmissionReceipt ApprovalReceipt ReleaseRecord ActivationRecord",
            "runtime_root",
        ),
        *_group(releases, "ReleaseRef ReleaseStatus ActivationRef", "foundation"),
        *_group(releases, "CapabilityReport", "runtime_component"),
        *_group(releases, "CapabilityAssessment ActivationScope", "named_union"),
        *_group(intake, "TaskSubmission AcceptedTask ContextSnapshot", "runtime_root"),
        *_group(
            intake, "TaskContext ClockProvenance ContextValue", "runtime_component"
        ),
        *_group(intake, "CaptureClockSource", "foundation"),
        *_group(intake, "ContextValueProvenance", "named_union"),
        *_group(media, "MediaAsset", "runtime_root"),
        *_group(
            media,
            "MediaRef BBoxXYXY CoordinateSystem TransformRecord StorageReference",
            "runtime_component",
        ),
        *_group(media, "AssetStage", "named_union"),
        *_group(evidence, "EvidenceAsset", "runtime_root"),
        *_group(evidence, "RetentionPolicyRef", "runtime_component"),
        *_group(
            evidence,
            "EvidenceScope EvidenceAccessScope EvidenceSpatialProvenance",
            "named_union",
        ),
        *_group(
            executions, "ExecutionRecord NodeRun Attempt ExecutionEvent", "runtime_root"
        ),
        *_group(
            executions,
            "ExecutionPins ConfiguredModelPin ConfiguredToolPin ContextSnapshotPin TraceContext SystemOutputAuthorityWitness",
            "runtime_component",
        ),
        *_group(executions, "NodeId", "foundation"),
        *_group(executions, "ExecutionEventPayload", "named_union"),
        *_group(invocations, "ModelInvocation", "runtime_root"),
        *_group(
            invocations,
            "InvocationIdentity InvocationContractSnapshot ConfiguredProviderIdentity InvocationError UsageRecord",
            "runtime_component",
        ),
        *_group(
            invocations,
            "ActualProviderIdentity ProviderRouteTrace InvocationOutcome InvocationPurpose",
            "named_union",
        ),
        *_group(
            invocations,
            "ProviderRoutingMode RouteKind ProviderVersionResolution",
            "foundation",
        ),
        *_group(
            selection,
            "AllDeclaredSelectionRecord ContextRuleSelectionRecord ModelAdvisorySelectionRecord FallbackSelectionRecord",
            "runtime_root",
        ),
        *_group(
            selection,
            "QuestionSelectionRecord FallbackSelectionEvidence",
            "named_union",
        ),
        *_group(selection, "ExcludedQuestionReason", "runtime_component"),
        *_group(selection, "SelectionInvocationMode", "foundation"),
        *_group(tool_executions, "ToolExecution", "runtime_root"),
        *_group(
            tool_executions,
            "GroundingBindingOrigin ToolPlanStepOrigin ToolError ToolResourceUsage ToolAuthorityObservation",
            "runtime_component",
        ),
        *_group(
            tool_executions,
            "ToolInvocationOrigin ToolResult ToolOutputState ToolAttemptResult",
            "named_union",
        ),
        *_group(
            tool_executions,
            "ToolInvocationOriginKind ImplementationVersionResolution",
            "foundation",
        ),
        *_group(
            applicability,
            "ProfileEligibilityEvaluation RuleSetApplicabilityEvaluation RuleApplicabilityEvaluation ApplicabilityTrace UnresolvedObligation",
            "runtime_root",
        ),
        *_group(
            applicability,
            "RuleTruth ApplicabilityLevel ApplicabilityEvaluationStatus ApplicabilityDisposition ApplicabilityEvaluationOwner ApplicabilityReason",
            "foundation",
        ),
        *_group(
            applicability,
            "ApplicabilityLevelTrace ApplicabilityAncestorRef ApplicabilityEvaluationRef ApplicabilityTargetRef ApplicabilityReasonEvidenceRef",
            "named_union",
        ),
        *_group(
            applicability_inputs,
            "ApplicabilityScope ApplicabilityRecordSet",
            "nonpersistent_input",
        ),
        *_group(coverage, "ExecutionCoverage ProfileCoverage", "runtime_root"),
        *_group(
            coverage,
            "MandatoryRuleCoverage MandatoryRuleObligation",
            "runtime_component",
        ),
        *_group(coverage, "CoverageStatus CoverageReason", "foundation"),
        *_group(
            coverage,
            "ObligationTarget ObligationDisposition CoverageScope",
            "named_union",
        ),
        *_group(decisions, "CandidatePrediction DecisionRecord", "runtime_root"),
        *_group(
            decisions,
            "RuleEvaluation CandidateCategory ConflictResolutionTrace ExplanationTrace",
            "runtime_component",
        ),
        *_group(
            decisions, "RuleEvaluationOutcome RequiredEvidenceGuard", "named_union"
        ),
        *_group(decision_coverage, "DecisionCoverageRecords", "nonpersistent_input"),
        *_group(coverage_sources, "CoverageSources", "nonpersistent_input"),
        *_group(runtime_authority, "RuntimeAuthorityRecords", "nonpersistent_input"),
        *_group(perception, "ObjectRecord GroundingRecord", "runtime_root"),
        *_group(perception, "DiscoveryOutcome GroundingOutcome", "foundation"),
        *_group(
            perception,
            "EffectiveQuestionSelection ProfileGroundingResult ModelGroundingProvenance ToolGroundingProvenance",
            "runtime_component",
        ),
        *_group(perception, "GroundingProvenance", "named_union"),
        *_group(observations, "ObservationBatch", "runtime_root"),
        *_group(observations, "QuestionResult", "runtime_component"),
        *_group(observations, "QuestionValidationResult", "named_union"),
        *_group(
            context,
            "ContextBuildInput SuccessfulContextBuild FailedContextBuild",
            "runtime_root",
        ),
        *_group(context, "ContextBuildOutput ContextValidationReport", "named_union"),
        *_group(
            discovery,
            "DiscoveryInput DiscoveryProfileDiagnostic DiscoveryAttemptOutput SkippedDiscoveryOutput",
            "runtime_root",
        ),
        *_group(discovery, "ProfileHit", "runtime_component"),
        *_group(discovery, "DiscoveryOutput", "named_union"),
        *_group(grounding, "GroundingInput GroundingOutput", "runtime_root"),
        *_group(observation, "ObservationInput", "runtime_root"),
        *_group(tool_node, "ToolNodeInput ToolNodeOutput", "runtime_root"),
        *_group(tool_node, "ToolPlanRunSummary", "runtime_component"),
        *_group(tool_node, "ToolPlanStepResult", "named_union"),
        *_group(decision, "DecisionInput DecisionOutput", "runtime_root"),
        *_group(
            envelopes,
            "NodeInputEnvelope SucceededNodeOutput FailedNodeOutput CancelledNodeOutput",
            "runtime_root",
        ),
        *_group(
            envelopes,
            "NodeError ArtifactOutputRef NodeInputs NodeOutputs",
            "runtime_component",
        ),
        *_group(envelopes, "NodeOutputEnvelope", "named_union"),
        *_group(replay, "ReplayKind", "foundation"),
        *_group(
            replay,
            "ArtifactReplayRequest InferenceReplayRequest",
            "nonpersistent_input",
        ),
        *_group(replay, "ReplayRequest ReplayRun", "named_union"),
        *_group(replay, "ArtifactReplayRun InferenceReplayRun", "runtime_root"),
    )
    # Revisions describe the engineering JSON Schema files. The optional grounding
    # fields and expanded N05 history keep the persisted 1.0.0 wire version.
    REVISED_EXPORTS = {
        "definitions.capabilities.ProfileDefinition",
        "nodes.decision.DecisionInput",
        "nodes.observation.ObservationInput",
        "nodes.tool_execution.ToolNodeInput",
        "runtime.executions.NodeRun",
        "runtime.perception.ObjectRecord",
        "validation.coverage_sources.CoverageSources",
        "validation.definition_inputs.DefinitionRegistration",
        "validation.definition_inputs.DefinitionValidationInput",
        "validation.producer_authority_inputs.ProducerAuthorityValidationInput",
        "validation.producer_inputs.ProducerCatalogueContext",
        "validation.producer_inputs.ProducerValidationContext",
        "validation.rule_type_inputs.RuleTypeBindings",
        "validation.runtime_authority.RuntimeAuthorityRecords",
    }
    TOOL_PLAN_COMPLETION_EXPORTS = {
        "enums.RuntimeRecordKind",
        "nodes.context.ContextBuildInput",
        "nodes.context.ContextBuildOutput",
        "nodes.context.FailedContextBuild",
        "nodes.context.SuccessfulContextBuild",
        "nodes.decision.DecisionInput",
        "nodes.decision.DecisionOutput",
        "nodes.discovery.DiscoveryAttemptOutput",
        "nodes.discovery.DiscoveryInput",
        "nodes.discovery.DiscoveryOutput",
        "nodes.envelopes.ArtifactOutputRef",
        "nodes.envelopes.CancelledNodeOutput",
        "nodes.envelopes.FailedNodeOutput",
        "nodes.envelopes.NodeInputEnvelope",
        "nodes.envelopes.NodeInputs",
        "nodes.envelopes.NodeOutputEnvelope",
        "nodes.envelopes.NodeOutputs",
        "nodes.envelopes.SucceededNodeOutput",
        "nodes.grounding.GroundingInput",
        "nodes.grounding.GroundingOutput",
        "nodes.observation.ObservationInput",
        "nodes.tool_execution.ToolNodeInput",
        "nodes.tool_execution.ToolNodeOutput",
        "references.RuntimeRecordRef",
        "runtime.executions.ExecutionEvent",
        "runtime.executions.ExecutionEventPayload",
        "runtime.executions.NodeRun",
        "runtime.replay.ArtifactReplayRequest",
        "runtime.replay.ArtifactReplayRun",
        "runtime.replay.ReplayRequest",
        "runtime.replay.ReplayRun",
        "validation.coverage_sources.CoverageSources",
        "validation.runtime_authority.RuntimeAuthorityRecords",
    }

    def _published(entries: tuple[Export, ...]) -> tuple[Export, ...]:
        return tuple(
            replace(entry, revision="1.0.0")
            if entry.filename.removesuffix(".schema.json")
            in REVISED_EXPORTS | TOOL_PLAN_COMPLETION_EXPORTS
            else entry
            for entry in entries
        )

    WP01B_INVENTORY = _published(WP01B_INVENTORY)
    WP01C_INVENTORY = _published(WP01C_INVENTORY)
    REVISED_INVENTORY = tuple(
        replace(
            entry,
            filename=entry.filename.removesuffix(".schema.json")
            + ".v1.1.0.schema.json",
            revision="1.1.0",
            supersedes=entry.filename,
        )
        for entry in (*WP01B_INVENTORY, *WP01C_INVENTORY)
        if entry.filename.removesuffix(".schema.json") in REVISED_EXPORTS
    )
    TOOL_PLAN_COMPLETION_INVENTORY = tuple(
        replace(
            entry,
            filename=stem + ".v1.2.0.schema.json",
            revision="1.2.0",
            supersedes=(
                stem + ".v1.1.0.schema.json"
                if stem in REVISED_EXPORTS
                else entry.filename
            ),
        )
        for entry in (*WP01B_INVENTORY, *WP01C_INVENTORY)
        if (stem := entry.filename.removesuffix(".schema.json"))
        in TOOL_PLAN_COMPLETION_EXPORTS
    )
    INVENTORY = (
        *WP01B_INVENTORY,
        *WP01C_INVENTORY,
        *REVISED_INVENTORY,
        *TOOL_PLAN_COMPLETION_INVENTORY,
        *(
            replace(
                entry,
                filename="nodes.observation.ObservationInput.v1.3.0.schema.json",
                revision="1.3.0",
                supersedes=entry.filename,
            )
            for entry in TOOL_PLAN_COMPLETION_INVENTORY
            if entry.symbol == "ObservationInput"
        ),
        *_group(tool_node, "ToolPlanCompletionRecord", "runtime_root"),
        *_group(capabilities, "GroundingGrouping", "named_union"),
    )

    class PreRegionObservationSchemaGenerator(GenerateJsonSchema):
        """Keep the published single-Object input projection byte stable."""

        def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
            document = super().model_schema(schema)
            if schema["cls"] is observation.ObservationInput:
                document["properties"].pop("image_mode", None)
            return document

    class PreToolPlanCompletionSchemaGenerator(PreRegionObservationSchemaGenerator):
        """Preserve the enum values present in published schema revisions."""

        def enum_schema(self, schema: core_schema.EnumSchema) -> JsonSchemaValue:
            if schema["cls"] is enums.RuntimeRecordKind:
                schema = {
                    **schema,
                    "members": [
                        member
                        for member in schema["members"]
                        if member is not enums.RuntimeRecordKind.TOOL_PLAN_RUN_SUMMARY
                    ],
                }
            return super().enum_schema(schema)

    class PublishedSchemaGenerator(PreToolPlanCompletionSchemaGenerator):
        """Generate the frozen validation shape from the shared Pydantic contracts."""

        _grouping_fields: dict[type[object], set[str]] = {
            capabilities.ProfileDefinition: {"grounding_grouping"},
            perception.ObjectRecord: {"grounding_member_boxes", "marked_frame"},
            observation.ObjectImageView: {"marked_media_ref"},
        }

        def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
            if schema["cls"] is executions.NodeRun:
                document = super().model_schema(schema)
                document["properties"]["attempt_history"]["maxItems"] = 1024
                del document["if"], document["then"]
                return document
            fields_added_by_grouping = self._grouping_fields.get(schema["cls"])
            if fields_added_by_grouping is None:
                return super().model_schema(schema)
            fields_schema = schema["schema"]
            if fields_schema["type"] != "model-fields" or (
                not fields_added_by_grouping <= fields_schema["fields"].keys()
            ):
                raise ValueError("Current model no longer supports its published shape")
            return super().model_schema(
                {
                    **schema,
                    "schema": {
                        **fields_schema,
                        "fields": {
                            name: field
                            for name, field in fields_schema["fields"].items()
                            if name not in fields_added_by_grouping
                        },
                    },
                }
            )

    LIMITATIONS = {
        "numeric": "JSON Schema mathematical integer/number do not enforce A strict Python int/float representation or all dynamic bounds.",
        "utc": "Schema states normalized UTC lexical support; Pydantic checks calendar/aware semantics. No leap-second support; -00:00 provenance is not normalized away.",
        "bytes": "Nonpersistent candidate/snapshot bytes are Python inputs, not a specified JSON byte-transport or semantic roundtrip protocol.",
        "taint": "Constructed/copied-instance revalidation and deep immutability require Pydantic checks, not JSON Schema alone.",
        "relations": "Finite reference/policy/Producer/history checks are separate from shape validation. Schemas do not prove expression typing, condition truth, model calls, runtime authority, external authenticity/history completeness or Release/Bundle admission.",
        "digest": "Engineering file SHA256 is not RFC8785 child projection or parent/Bundle digest; F1 owns value_schema derivation.",
    }
    DEFERRED_GROUPS = (
        "deterministic Rule evaluation",
        "dynamic condition activation",
        "Release and Bundle admission",
        "external authenticity and history completeness",
        "consumer-ready runtime authority",
    )

    def _json_bytes(value: object) -> bytes:
        return (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            )
            + "\n"
        ).encode("utf-8")

    def _resolve_pointer(document: dict[str, object], reference: str) -> None:
        if reference == "#":
            return
        if not reference.startswith("#/"):
            raise ValueError("Only document-local JSON Pointer refs are allowed")
        current: object = document
        for encoded in unquote(reference[2:]).split("/"):
            if re.search(r"~(?![01])", encoded):
                raise ValueError("Invalid JSON Pointer escape")
            part = encoded.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif (
                isinstance(current, list)
                and part.isascii()
                and part.isdecimal()
                and (part == "0" or not part.startswith("0"))
                and int(part) < len(current)
            ):
                current = current[int(part)]
            else:
                raise ValueError("Unresolved document-local JSON Pointer")

    def validate_document(document: dict[str, object]) -> None:
        Draft202012Validator.check_schema(document)
        pending: list[object] = [document]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in ("$ref", "$dynamicRef", "$recursiveRef"):
                        if not isinstance(value, str):
                            raise ValueError("Schema ref must be a string")
                        _resolve_pointer(document, value)
                    pending.append(value)
            elif isinstance(item, list):
                pending.extend(item)

    def render_exports(inventory: tuple[Export, ...] = INVENTORY) -> dict[str, bytes]:
        names: set[str] = set()
        symbols: set[tuple[str, str | None]] = set()
        # Check source and filename collisions before generating any Schema.
        for entry in inventory:
            if not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*\.schema\.json", entry.filename
            ):
                raise ValueError("Schema filename must be a unique safe basename")
            identity = (entry.source_symbol, entry.revision)
            if entry.filename in names or identity in symbols:
                raise ValueError("Duplicate Schema filename or source symbol")
            names.add(entry.filename)
            symbols.add(identity)
            source = Path(cast(str, entry.module.__file__)).resolve()
            expected = (
                REPOSITORY_ROOT
                / "src"
                / Path(*entry.module.__name__.split(".")).with_suffix(".py")
            )
            if source != expected or not entry.module.__name__.startswith(
                "jimu_runtime.domain."
            ):
                raise ValueError(
                    "Export source module does not belong to this repository"
                )
            if not hasattr(entry.module, entry.symbol):
                raise ValueError(
                    "Explicit inventory names an unimplemented source symbol"
                )
        rendered: dict[str, bytes] = {}
        index: list[dict[str, object]] = []
        for entry in sorted(inventory, key=lambda value: value.filename):
            target = getattr(entry.module, entry.symbol)
            document = {
                "$schema": DIALECT,
                **TypeAdapter(target).json_schema(
                    mode="validation",
                    by_alias=True,
                    union_format="any_of",
                    schema_generator=(
                        PublishedSchemaGenerator
                        if entry.revision == "1.0.0"
                        else PreToolPlanCompletionSchemaGenerator
                        if entry.revision == "1.1.0"
                        else PreRegionObservationSchemaGenerator
                        if entry.revision == "1.2.0"
                        else GenerateJsonSchema
                    ),
                ),
            }
            validate_document(document)
            encoded = _json_bytes(document)
            rendered[entry.filename] = encoded
            item: dict[str, object] = {
                "source_symbol": entry.source_symbol,
                "role": entry.role,
                "filename": entry.filename,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "limitations": list(LIMITATIONS),
            }
            if entry.revision is not None:
                item["export_revision"] = entry.revision
            if entry.supersedes is not None:
                item["supersedes"] = entry.supersedes
            index.append(item)
        rendered["index.json"] = _json_bytes(
            {
                "schema_version": "1.0.0",
                "status": STATUS,
                "export_kind": "engineering_pydantic_schemas",
                "generation": {
                    "mode": "validation",
                    "by_alias": True,
                    "union_format": "any_of",
                    "dialect": DIALECT,
                },
                "deferred_groups": list(DEFERRED_GROUPS),
                "limitations": LIMITATIONS,
                "schemas": index,
            }
        )
        return rendered

    def _safe_output(output: Path) -> Path:
        if ".." in output.parts:
            raise ValueError("Output traversal is not allowed")
        absolute = output.absolute()
        for part in (*reversed(absolute.parents), absolute):
            if part.is_symlink():
                raise ValueError("Output directory/file symlinks are not allowed")
            if part.exists() and not part.is_dir():
                raise ValueError("Output ancestors must be directories")
        return absolute

    def export_schemas(
        output: Path | None = None, *, check: bool = False
    ) -> tuple[str, ...]:
        default = REPOSITORY_ROOT / "schemas" / "generated"
        directory = _safe_output(default if output is None else output)
        if output is None and not directory.resolve().is_relative_to(REPOSITORY_ROOT):
            raise ValueError("Default output escaped the repository")
        rendered = (
            render_exports()
        )  # All documents validated before any filesystem write.
        expected = set(rendered)
        errors: list[str] = []
        if directory.exists():
            for entry in directory.iterdir():
                if entry.is_symlink():
                    raise ValueError("Generated entries cannot be symlinks")
                if not entry.is_file() or entry.name not in expected:
                    errors.append(f"unknown generated entry: {entry.name}")
            if errors:
                return tuple(sorted(errors))
        elif check:
            return ("missing generated directory",)
        for name, encoded in rendered.items():
            child = directory / name
            if child.is_symlink() or child.resolve().parent != directory.resolve():
                raise ValueError("Generated child escaped output or is a symlink")
            if child.exists() and not child.is_file():
                raise ValueError("Generated child must be a regular file")
            if check and (not child.exists() or child.read_bytes() != encoded):
                errors.append(f"missing or stale generated file: {name}")
        if check:
            return tuple(sorted(errors))
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name, encoded in rendered.items():
                child_fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=fd,
                )
                with os.fdopen(child_fd, "wb") as stream:
                    stream.write(encoded)
        finally:
            os.close(fd)
        return ()

    def main() -> int:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--check", action="store_true", help="Compare without writing files/caches"
        )
        parser.add_argument(
            "--output",
            type=Path,
            help="Explicit test output directory; no symlinks/traversal",
        )
        args = parser.parse_args()
        try:
            mismatches = export_schemas(args.output, check=args.check)
        except (ValueError, OSError) as exc:
            print(f"Schema export rejected: {exc}", file=sys.stderr)
            return 1
        if mismatches:
            print("\n".join(mismatches), file=sys.stderr)
            return 1
        print(
            "Schema export matches"
            if args.check
            else f"Schema export written; {STATUS}"
        )
        return 0

    return (
        (
            Export,
            INVENTORY,
            WP01B_INVENTORY,
            WP01C_INVENTORY,
            DIALECT,
            LIMITATIONS,
            DEFERRED_GROUPS,
            REPOSITORY_ROOT,
            render_exports,
            validate_document,
            export_schemas,
        ),
        main,
    )


(
    (
        Export,
        INVENTORY,
        WP01B_INVENTORY,
        WP01C_INVENTORY,
        DIALECT,
        LIMITATIONS,
        DEFERRED_GROUPS,
        REPOSITORY_ROOT,
        render_exports,
        validate_document,
        export_schemas,
    ),
    main,
) = _bootstrap()


if __name__ == "__main__":
    raise SystemExit(main())
