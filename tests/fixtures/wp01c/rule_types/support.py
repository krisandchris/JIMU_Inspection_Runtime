"""Synthetic type inputs assembled from the actual A/B/C contracts."""

from copy import deepcopy

from fixtures.wp01b.v1.support import entry, fixture, with_step
from pydantic import TypeAdapter

from jimu_runtime.domain.conditions import condition_references
from jimu_runtime.domain.definition_enums import ConditionInputDomain
from jimu_runtime.domain.fact_schemas.models import (
    FactSchemaDefinition,
    ValueSchemaGenerationSpec,
)
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.rules import RuleDefinition
from jimu_runtime.domain.validation.rule_type_inputs import (
    RuleTypeBindings,
    RuleTypeUseContext,
)
from jimu_runtime.domain.validation.rule_types import check_condition_types


def ref(kind, name, digest="a"):
    return {
        "artifact_kind": kind,
        "artifact_id": name,
        "version": "1.0.0",
        "digest": digest * 64,
    }


def unit(member="m"):
    return {"vocabulary_ref": ref("vocabulary", "test-unit"), "member": member}


def literal(kind="bool", value=True, **constraints):
    return {
        "kind": "literal",
        "constant": {
            "type_definition": {"kind": kind, **constraints},
            "value": {"kind": kind, "value": value},
        },
    }


def compare(left, right, operator="eq"):
    return {"kind": "comparison", "operator": operator, "left": left, "right": right}


def membership(item, collection):
    return {"kind": "in", "item": item, "collection": collection}


def selector(schema, *, source="context_snapshot", path=("scene",)):
    return {"source": source, "schema_ref": schema, "field_path": path}


class Example:
    def __init__(self):
        self.data = with_step(fixture())
        self.base_fact = self.data["fact_schemas"][0]
        self.profile = entry(self.data, "profile")
        self.question = entry(self.data, "question")
        self.tool = entry(self.data, "tool")
        self.plan = entry(self.data, "tool_plan")
        self.rule_ref = ref("rule", "rule-test")
        self.rule_set_ref = ref("rule_set", "ruleset-test")
        self.profile["definition"]["rule_set_refs"] = [self.rule_set_ref]
        self.catalogue_ref = ref("fact_producer_catalog", "catalogue-test")
        self.declarations = []
        self.available = []
        self.units = []
        self.context_units = []
        self.policies = []
        self.use = self.data["input"]["use_contexts"][0]
        self.descriptors = self.data["input"]["descriptors"]

    def fact_schema(
        self, name, shape=None, *, scope="object", kinds=("vlm_question",), units=()
    ):
        shape = {"kind": "bool"} if shape is None else shape
        payload = deepcopy(self.base_fact["definition"])
        spec = ValueSchemaGenerationSpec.model_validate(
            payload["value_schema_generation"]
        )
        generated = generate_value_schema(shape, spec)
        payload.update(
            schema_id=name,
            type_definition=shape,
            scope=scope,
            allowed_producer_kinds=kinds,
            payload_schema_ref=generated.child_ref.model_dump(mode="json"),
        )
        if units:
            payload["units"] = {
                "mode": "declared",
                "bindings": [
                    {"field_path": path, "unit": declared} for path, declared in units
                ],
            }
        schema = ref("fact_schema", name)
        spec_data = {"ref": schema, "definition": payload}
        self.data["fact_schemas"].append(spec_data)
        return schema

    def _fact(
        self,
        name,
        schema,
        producer,
        *,
        scope="object",
        output="primary",
        path=(),
        activation=None,
    ):
        kind = producer["producer_kind"]
        if kind == "system":
            identity = producer["requirement"]["capability_id"]
            version = producer["requirement"]["contract_version"]
        else:
            owner = (
                producer["question_ref"]
                if kind == "vlm_question"
                else producer["tool_ref"]
            )
            identity, version = owner["artifact_id"], owner["version"]
        declaration = {
            "declaration_id": name,
            "scope": scope,
            "producer_kind": kind,
            "producer_id": identity,
            "producer_version": version,
            "producer": producer,
            "output_name": output,
            "fact_key": name,
            "fact_schema_ref": schema,
            "cardinality": "exactly_one",
            "activation_condition": {"mode": "always"}
            if activation is None
            else {
                "mode": "tool_condition",
                "condition_ast": activation,
            },
        }
        if scope == "object":
            declaration["profile_ref"] = self.profile["ref"]
        self.declarations.append(declaration)
        return {
            "kind": "record_field_ref" if path else "fact_ref",
            "selector": {
                "scope": scope,
                "fact_key": name,
                "fact_schema_ref": schema,
                "field_path": path,
                "producer_selector": {**producer, "output_name": output},
            },
        }

    def question_fact(self, name="q.test", *, shape=None, path=(), units=()):
        schema = self.fact_schema(name, shape, units=units)
        question = deepcopy(self.question)
        question["ref"] = ref("question", name)
        question["definition"].update(
            question_id=name,
            primary_fact_key=name,
            primary_fact_schema_ref=schema,
        )
        self.data["input"]["definitions"].append(question)
        self.profile["definition"]["question_refs"].append(question["ref"])
        return self._fact(
            name,
            schema,
            {"producer_kind": "vlm_question", "question_ref": question["ref"]},
            path=path,
        )

    def system_fact(self, name="system.test", *, scope="execution", activation=None):
        schema = self.fact_schema(name, scope=scope, kinds=("system",))
        return self._fact(
            name,
            schema,
            {
                "producer_kind": "system",
                "requirement": {
                    "family": "system_fact_producer",
                    "capability_id": name,
                    "contract_version": "1.0.0",
                    "allowed_implementation_versions": ["1.0.0"],
                    "required_features": [],
                },
            },
            scope=scope,
            activation=activation,
        )

    def plan_chain(self):
        base = self.plan["definition"]["steps"][0]
        self.plan["definition"]["steps"] = [
            dict(deepcopy(base), step_id="first", depends_on=[]),
            dict(deepcopy(base), step_id="second", depends_on=["first"]),
            dict(deepcopy(base), step_id="third", depends_on=["second"]),
            dict(deepcopy(base), step_id="sibling", depends_on=[]),
        ]

    def tool_fact(self, name="output.test", *, step="first", activation=None):
        schema = self.fact_schema(name, kinds=("tool",))
        self.tool["definition"]["fact_outputs"].append(
            dict(
                output_name=name,
                fact_key=name,
                fact_schema_ref=schema,
                scope="object",
                required=True,
                value_path=[],
            )
        )
        step_def = next(
            value
            for value in self.plan["definition"]["steps"]
            if value["step_id"] == step
        )
        step_def["fact_output_mappings"].append(
            {"output_name": name, "target_fact_key": name}
        )
        return self._fact(
            name,
            schema,
            {
                "producer_kind": "tool",
                "tool_plan_ref": self.plan["ref"],
                "step_id": step,
                "tool_ref": self.tool["ref"],
            },
            output=name,
            activation=activation,
        )

    def local_fact(self, **kwargs):
        fact = self.tool_fact(**kwargs)
        data = fact["selector"]
        return {
            "kind": "local_tool_fact_ref",
            "selector": {
                **{
                    key: value
                    for key, value in data.items()
                    if key != "producer_selector"
                },
                **{
                    key: value
                    for key, value in data["producer_selector"].items()
                    if key in ("step_id", "tool_ref", "output_name")
                },
            },
        }

    def context_field(self, *, role="context_snapshot", shape=None, path=("scene",)):
        source = next(
            item for item in self.use["context_registrations"] if item["source"] == role
        )
        if shape is not None:
            descriptor = next(
                item
                for item in self.descriptors
                if item["schema_ref"] == source["schema_ref"]
            )
            descriptor["type_definition"]["fields"] = [
                {"name": "scene", "required": True, "type_definition": shape},
            ]
        return {
            "kind": "context_ref",
            "selector": selector(source["schema_ref"], source=role, path=path),
        }

    def registry(self):
        builder = FactSchemaRegistryBuilder()
        for item in self.data["fact_schemas"]:
            builder.register(
                ContentRef.model_validate(item["ref"]),
                FactSchemaDefinition.model_validate(item["definition"]),
            )
        return builder.freeze()

    def bindings(self, *, catalogue=True):
        definitions = self.data["input"]["definitions"]
        raw = {
            directory: [
                item
                if kind == "profile"
                else {"ref": item["ref"], "definition": item["definition"]}
                for item in definitions
                if item["registration_kind"] == kind
            ]
            for directory, kind in (
                ("profiles", "profile"),
                ("questions", "question"),
                ("plans", "tool_plan"),
                ("tools", "tool"),
            )
        }
        raw.update(
            policies=self.policies,
            literal_units=self.units,
            context_units=self.context_units,
            prior_producer_declaration_ids=self.available,
        )
        if catalogue:
            raw["producer_catalogue"] = {
                "ref": self.catalogue_ref,
                "definition": {
                    "schema_version": "1.0.0",
                    "catalogue_id": self.catalogue_ref["artifact_id"],
                    "version": "1.0.0",
                    "declarations": self.declarations,
                },
            }
        return RuleTypeBindings.model_validate(raw)

    def context(self, role, *, step="third", declaration=None, **overrides):
        raw = {
            "use_role": role,
            "profile_ref": self.profile["ref"],
            "region_ref": self.use["region_ref"],
            "descriptors": self.descriptors,
            "context_registrations": self.use["context_registrations"],
        }
        if role == "rule_condition":
            raw.update(
                rule_ref=self.rule_ref,
                rule_set={
                    "ref": self.rule_set_ref,
                    "definition": {
                        "schema_version": "1.0.0",
                        "rule_set_id": self.rule_set_ref["artifact_id"],
                        "version": "1.0.0",
                        "rule_refs": [self.rule_ref],
                        "applicability_policy_ref": ref("policy", "ruleset-applies"),
                        "conflict_policy_ref": ref("policy", "conflict"),
                    },
                },
            )
        if role == "applicability":
            raw["policy_ref"] = self.profile["definition"]["applicability_policy_ref"]
        if role == "tool_step_guard":
            step_def = next(
                value
                for value in self.plan["definition"]["steps"]
                if value["step_id"] == step
            )
            raw.update(
                tool_plan_ref=self.plan["ref"],
                step_id=step,
                policy_ref=step_def["skip_policy_ref"],
            )
        if role == "producer_activation":
            item = next(
                current
                for current in self.declarations
                if current["declaration_id"] == declaration
            )
            raw.update(
                producer_catalogue_ref=self.catalogue_ref,
                declaration_id=declaration,
                producer_kind=item["producer_kind"],
                producer_scope=item["scope"],
            )
            if item["scope"] == "execution":
                raw.pop("profile_ref")
            if item["producer_kind"] == "tool":
                raw.update(
                    tool_plan_ref=self.plan["ref"], step_id=item["producer"]["step_id"]
                )
        return TypeAdapter(RuleTypeUseContext).validate_python({**raw, **overrides})

    def rule(self, expr, *, optional=()):
        # The genuine RuleDefinition verifies the declared selector partition.
        from jimu_runtime.domain.conditions import validate_condition_data

        tree = validate_condition_data(
            expr, input_domain=ConditionInputDomain.RULE_CONDITION
        )
        required = set(condition_references(tree).fact_selectors)
        return RuleDefinition.model_validate(
            {
                "schema_version": "1.0.0",
                "rule_id": self.rule_ref["artifact_id"],
                "version": "1.0.0",
                "violation_category": "synthetic-test",
                "condition_ast": tree,
                "required_fact_selectors": tuple(required - set(optional)),
                "optional_fact_selectors": optional,
                "applicability_policy_ref": ref("policy", "rule-applies"),
                "rule_policy_ref": ref("policy", "rule-policy"),
                "evidence_policy_ref": ref("policy", "rule-evidence"),
            },
        )

    def check(
        self, expr, role="rule_condition", *, context=None, bindings=None, **kwargs
    ):
        domain = {
            "applicability": ConditionInputDomain.RULE_APPLICABILITY,
            "rule_condition": ConditionInputDomain.RULE_CONDITION,
            "tool_step_guard": ConditionInputDomain.TOOL_CONDITION,
            "producer_activation": ConditionInputDomain.TOOL_CONDITION,
        }[role]
        return check_condition_types(
            expr,
            input_domain=domain,
            context=self.context(role) if context is None else context,
            bindings=self.bindings() if bindings is None else bindings,
            fact_schemas=self.registry(),
            **kwargs,
        )
