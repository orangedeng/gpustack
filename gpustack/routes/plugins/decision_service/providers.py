"""The decision-service plugin's providers catalogue: how ``type=gpustack-lb-typesafe``
ModelProviders become the wasm plugin's ``defaultConfig.providers`` entries.

One entry per decision-service ModelProvider, id ``provider-{id}`` (the same
registry-name rule ai-proxy uses, so the McpBridge registry
``provider_registry()`` already maintains is the cluster behind it).

Every entry carries an explicit ``endpoint`` — a custom base url when the
provider config sets one, otherwise the TypeSafe hosted default
(``https://api.typesafe.ai``) — and the plugin-side ``type`` is uniformly
``systemone`` (the generic implementation; per review the plugin does not
distinguish flavors yet).

Every entry also carries an explicit ``cluster``: the plugin derives
``outbound|<port>||<host>`` from the endpoint only when ``cluster`` is
absent, which matches a registry-generated name only when the registry name
equals the endpoint host — never the case for ``provider-{id}`` names. The
registry-generated form is ``outbound|<port>||<name>.<static|dns>``.
"""

import logging
from functools import partial
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gpustack.config.config import Config
from gpustack.gateway.client.extensions_higress_io_v1_api import WasmPluginSpec
from gpustack.schemas.model_provider import (
    ModelProvider,
    ModelProviderTypeEnum,
    TypesafeConfig,
)
from gpustack.schemas.models import (
    Model,
    ModelInstance,
    ModelInstanceStateEnum,
    is_decision_model,
    servable_instances,
)

logger = logging.getLogger(__name__)

# The wasm plugin ships as gpustack-lb-decision-service: CR name, module
# path and gateway_plugin override key all follow it.
DECISION_SERVICE_CR_NAME = "gpustack-lb-decision-service"

# The single provider type that feeds the decision-service plugin today.
# Kept as an explicit set (not a naming-prefix convention) plus one
# predicate so every consumer — discovery categorisation, the test
# endpoints, route-target validation, catalogue generation and event
# handling — answers "is this a decision provider" through the same place;
# a future decision-engine type joins this set instead of duplicating the
# checks.
DECISION_PROVIDER_TYPES = frozenset({ModelProviderTypeEnum.GPUSTACK_LB_TYPESAFE})


def is_decision_config(config: Any) -> bool:
    """Whether a provider config feeds the decision-service plugin (and is
    therefore NOT an inference upstream)."""
    return isinstance(config, TypesafeConfig)


class DecisionServiceOverride(BaseModel):
    """Deployment-level knobs of the plugin's defaultConfig — the only place
    they are configurable. Route sections never override them; operators
    inject them through ``gateway_plugin["gpustack-lb-decision-service"].config``."""

    model_config = ConfigDict(extra="forbid")

    decisionTimeoutMs: Optional[int] = Field(default=None, ge=1)
    maxStateBytes: Optional[int] = Field(default=None, ge=1)
    maxBodyBytes: Optional[int] = Field(default=None, ge=1)


def decision_default_config(cfg: Optional[Config]) -> Dict[str, Any]:
    """The CR defaultConfig's static half: the inert switch (the plugin is
    instantiated on every route but does nothing without a matchRule) plus
    the decision knobs, with operator overrides layered on top."""
    from gpustack.gateway.plugins import plugin_entry

    config: Dict[str, Any] = {
        "enabled": False,
        "decisionTimeoutMs": 3000,
        "maxStateBytes": 65536,
        "maxBodyBytes": 104857600,
    }
    entry = plugin_entry(DECISION_SERVICE_CR_NAME, cfg)
    if entry is not None and entry.config:
        try:
            override = DecisionServiceOverride.model_validate(entry.config)
        except Exception as e:
            logger.error(
                "Invalid gateway_plugin.%s.config; using defaults: %s",
                DECISION_SERVICE_CR_NAME,
                e,
            )
        else:
            config.update(override.model_dump(exclude_none=True))
    return config


def decision_provider_entry(provider: ModelProvider) -> Optional[Dict[str, Any]]:
    """One ModelProvider's catalogue entry.

    The McpBridge registry the provider machinery maintains
    (``provider-{id}``) supplies the cluster. The endpoint is the provider's
    custom base url, or the TypeSafe hosted default when none is set; the
    plugin-side ``type`` is uniformly ``systemone``.
    """
    from gpustack.gateway import utils as gateway_utils

    config = provider.config
    if not isinstance(config, TypesafeConfig):
        return None
    entry: Dict[str, Any] = {
        "id": gateway_utils.provider_registry_name(provider.id),
        "type": "systemone",
        "endpoint": config.get_base_url(),
    }
    if config.model:
        entry["model"] = config.model
    # Canonical credential form: the plugin tries the keys in order and
    # retries the callout with the next one on 401/403/429 (invalid or
    # quota-exhausted key); the legacy singular apiToken is deprecated.
    if provider.api_tokens:
        entry["apiTokens"] = list(provider.api_tokens)
    # The cluster always comes from the provider's own ``provider-{id}``
    # registry — the registry name never equals the endpoint host, so the
    # endpoint-derived name the plugin would fall back to never resolves.
    registry = gateway_utils.provider_registry(provider)
    if registry is None:
        # Without a registry the plugin would derive the cluster from the
        # endpoint host, which never matches a provider-{id} registry name
        # -- the entry could not resolve, so it is skipped rather than
        # deployed broken.
        logger.warning(
            "decision-service ModelProvider %s (id %s) has no registry; skipping "
            "the decision-service catalogue entry",
            provider.name,
            provider.id,
        )
        return None
    entry["cluster"] = f"outbound|{registry.port}||{registry.get_service_name()}"
    return entry


def decision_model_entry(
    model: Model, instances: List[ModelInstance]
) -> Optional[Dict[str, Any]]:
    """One locally deployed decision Model's catalogue entry.

    Every servable RUNNING instance rides the entry as one cluster in the
    ``clusters`` list: the plugin (v0.5.0.post2+ / plugin VERSION 0.2.0)
    picks one uniformly at random per callout, so the decision traffic
    spreads across the replicas. The legacy singular ``cluster`` (first
    instance) and the ``endpoint`` (:authority source, still required by
    the plugin) are rendered alongside so plugin builds that predate the
    list form resolve the entry too — ``clusters`` takes precedence over
    ``cluster`` on builds that know it.

    No ``apiTokens``: local instances are not credential-gated (the same
    posture as inference traffic reaching them directly); an authenticated
    self-hosted service belongs on a ``gpustack-lb-typesafe`` provider row.
    """
    from gpustack.gateway import utils as gateway_utils

    resolved = []
    for instance in instances:
        address = instance.worker_advertise_address or instance.worker_ip
        if not address or instance.port is None:
            continue
        registry = gateway_utils.model_instance_registry(instance)
        if registry is None:
            continue
        resolved.append((instance, address, registry))
    if not resolved:
        return None

    def _cluster_name(registry) -> str:
        return f"outbound|{registry.port}||{registry.get_service_name()}"

    first_instance, first_address, first_registry = resolved[0]
    # The served model name is the natural decision-engine alias for a
    # local engine (vLLM/SGLang accept any name and ignore the
    # distinction); a route's decisionModel overrides it.
    return {
        "id": gateway_utils.model_ai_proxy_provider_id(model.id),
        "type": "systemone",
        "endpoint": f"http://{first_address}:{first_instance.port}",
        "model": model.name,
        "clusters": [_cluster_name(registry) for _, _, registry in resolved],
        "cluster": _cluster_name(first_registry),
    }


def decision_model_entries(
    models: List[Model], instances: List[ModelInstance]
) -> List[Dict[str, Any]]:
    """The locally deployed half of the catalogue: one entry per decision
    Model that has a RUNNING servable instance, model-id-sorted for a
    byte-stable CR.

    A model whose instances are all down is absent rather than present with
    dead clusters: routes selecting it fail open (their rule is stripped)
    until an instance is back, which the model-event-driven catalogue sync
    re-renders. Every servable RUNNING instance enters the entry's
    ``clusters`` list (instance-id order, byte-stable) — the plugin spreads
    the callouts across them — with the lowest-id instance also serving the
    legacy singular ``cluster`` and the ``endpoint``.
    """
    instances_by_model: Dict[int, List[ModelInstance]] = {}
    for instance in instances:
        if instance.model_id is None:
            continue
        if instance.state != ModelInstanceStateEnum.RUNNING:
            continue
        if getattr(instance, "deleted_at", None) is not None:
            continue
        instances_by_model.setdefault(instance.model_id, []).append(instance)

    entries: List[Dict[str, Any]] = []
    for model in sorted(models, key=lambda m: m.id):
        if model.deleted_at is not None or not is_decision_model(model):
            continue
        running = servable_instances(model, instances_by_model.get(model.id, []))
        candidates = sorted(
            running,
            key=lambda inst: inst.id if inst.id is not None else 0,
        )
        if not candidates:
            continue
        try:
            entry = decision_model_entry(model, candidates)
        except Exception as e:
            logger.warning(
                "decision-service catalogue entry for model %s (id %s) "
                "could not be built; skipping it: %s",
                model.name,
                model.id,
                e,
            )
            continue
        if entry is not None:
            entries.append(entry)
    return entries


def decision_model_referencing_route_ids(
    policies: List[Any], model_id: int
) -> "set[int]":
    """Route ids whose stored decision-service policy pins ``model_id`` —
    the local-target twin of ``decision_referencing_route_ids``: the routes
    a model event (instances up/down, category moved) has to re-reconcile."""
    return {
        policy.route_id
        for policy in policies or []
        if (policy.config or {}).get("modelId") == model_id
    }


def decision_provider_entries(providers: List[ModelProvider]) -> List[Dict[str, Any]]:
    """The full catalogue: one entry per decision-service ModelProvider,
    id-sorted for a byte-stable CR.

    One malformed provider must not block the rest: the catalogue spans
    every organization, so a bad entry is skipped with a warning and the
    valid endpoints and credentials still sync.

    There is deliberately NO synthetic hosted-default entry: every entry
    names a ModelProvider, whose endpoint is that provider's custom base
    url or the hosted default — one entry per configured provider, nothing
    invented.
    """
    entries: List[Dict[str, Any]] = []
    for provider in sorted(providers, key=lambda p: p.id):
        if not is_decision_config(provider.config):
            continue
        try:
            entry = decision_provider_entry(provider)
        except Exception as e:
            logger.warning(
                "decision-service catalogue entry for provider %s (id %s) "
                "could not be built; skipping it: %s",
                provider.name,
                provider.id,
                e,
            )
            continue
        if entry is not None:
            entries.append(entry)
    return entries


def decision_module_available(cfg: Optional[Config]) -> bool:
    """Whether the shipped plugins manifest can deploy the module. Until the
    gpustack-higress-plugins bump that packages it, the catalogue sync stays
    silent rather than failing every provider reconcile."""
    from gpustack.gateway.plugins import plugin_spec_overrides

    try:
        plugin_spec_overrides(DECISION_SERVICE_CR_NAME, cfg=cfg)
    except ValueError:
        return False
    return True


def _carry_match_rules(
    expected_default_config: Dict[str, Any],
    current: Optional[WasmPluginSpec],
) -> Optional[WasmPluginSpec]:
    """diff for the catalogue sync: replace only ``defaultConfig`` (the
    expected entries are the complete truth — every decision provider) on
    top of the live spec, carrying everything else — the static half
    (url/sha256/phase/priority) belongs to the init pass, matchRules to the
    route reconcile. A rebuild from just defaultConfig would strip the
    module URL and leave Envoy nothing to fetch.

    ``current`` None returns None: this sync never creates the CR, because
    a spec without the static half is not deployable — init owns creation.
    """
    if current is None:
        return None
    merged = current.model_dump(exclude_none=True)
    merged["defaultConfig"] = expected_default_config
    return WasmPluginSpec.model_validate(merged)


async def sync_decision_service_providers(
    cfg: Config,
    session: AsyncSession,
    extensions_api: Any,
) -> None:
    """Rebuild the CR's providers catalogue from every decision source:
    the external ``gpustack-lb-typesafe`` ModelProviders and the locally
    deployed decision Models (one entry per deployment with a running
    servable instance). Runs on ModelProvider and decision-Model events;
    the route-driven matchRules are preserved untouched."""
    from gpustack.gateway import utils as gateway_utils

    if not decision_module_available(cfg):
        return
    providers = await ModelProvider.all_by_field(session, "deleted_at", None)
    models = await Model.all_by_field(session, "deleted_at", None)
    instances = await ModelInstance.all_by_field(session, "deleted_at", None)
    default_config = decision_default_config(cfg)
    default_config["providers"] = decision_provider_entries(providers) + (
        decision_model_entries(models, instances)
    )
    # No activeProviderId here: an id that names no catalogue entry makes
    # the plugin's config parse fail outright, and the routes select their
    # provider through the matchRule's activeProviderId instead. Left
    # unset, the plugin auto-selects a single entry.
    # Positional binding: ensure_wasm_plugin invokes spec_diff(current_spec)
    # positionally, and a keyword-bound first argument collides with it.
    await gateway_utils.ensure_wasm_plugin(
        api=extensions_api,
        name=DECISION_SERVICE_CR_NAME,
        namespace=cfg.gateway_namespace,
        spec_diff=partial(_carry_match_rules, default_config),
    )
