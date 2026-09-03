"""Chart values for a registered Kubernetes cluster.

The registration flow hands a cluster a Helm chart plus a values file rather than
rendered objects. This module owns the second half: turning a cluster's
``K8sOptions``, worker config and registration token into the values the chart
reads.

The split is deliberate. Everything that decides *what a value is* — defaults,
normalization, the reserved data-dir mount, which objects the manifest owns
rather than the chart — stays here in Python, where it can be tested and where
the cluster record is. The chart only decides what the YAML looks like. Nothing
in this module renders Kubernetes objects.
"""

import hashlib
from typing import Any, Dict, List, Optional, Tuple

import yaml

from gpustack.k8s.manifest_template import TemplateConfig
from gpustack.schemas.clusters import SERVER_OWNED_VALUE_PATHS, mount_host_path

# The Secret the bootstrap manifest creates with kubectl. Naming it in the values
# is what keeps Helm from rendering one of its own: the token belongs to the
# manifest, so a re-render must not be able to rotate or delete it.
REGISTRATION_TOKEN_SECRET_NAME = "registration-token"


def split_image_reference(reference: str) -> Tuple[Optional[str], str, str]:
    """Split ``[registry/]repository[:tag]`` into its three parts.

    The chart composes an image as ``{global.hub}/{image.repository}:{image.tag}``,
    so a reference has to be taken apart before it can be handed over — the
    registry cannot stay inside ``repository`` or it would be prefixed twice.

    A leading segment counts as a registry only when it carries a ``.`` or a
    ``:``, or is exactly ``localhost``. That is the same test the operator chart
    applies, and it is what tells ``myregistry.io/gpustack/gpustack`` (registry +
    repository) apart from ``gpustack/gpustack`` (namespace + repository), which
    are otherwise the same shape.

    Raises ``ValueError`` for a digest reference: the chart has no way to express
    ``repository@sha256:...``, and silently dropping the digest would deploy a
    different image than the one asked for.
    """
    if "@" in reference:
        raise ValueError(
            f"image reference {reference!r} pins a digest, which the chart cannot "
            "express: it composes an image as repository:tag. Use a tag."
        )

    registry: Optional[str] = None
    remainder = reference
    head, slash, rest = reference.partition("/")
    if slash and ("." in head or ":" in head or head == "localhost"):
        registry, remainder = head, rest

    repository, colon, tag = remainder.rpartition(":")
    if not colon:
        # No tag at all: `rpartition` puts everything in the tail.
        return registry, tag, ""
    return registry, repository, tag


def _operator_env(config: TemplateConfig) -> Dict[str, str]:
    """Environment for the operator's control plane, as a name/value map.

    The three instance knobs are tri-state and are read with ``is not None``, not
    for truthiness: an unset knob must leave the operator's own default in place,
    while an explicit ``false`` has to reach it as the string ``"false"``.
    """
    env: Dict[str, str] = dict(config.operator_env or {})

    if config.operator_instance_access_static_address:
        env["GPUSTACK_INSTANCE_ACCESS_STATIC_ADDRESS"] = (
            config.operator_instance_access_static_address
        )
    if config.operator_instance_type_derived_from_node is not None:
        env["GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE"] = (
            config.operator_instance_type_derived_from_node
        )
    if config.operator_instance_type_mixed_on_node is not None:
        env["GPUSTACK_INSTANCE_TYPE_MIXED_ON_NODE"] = (
            config.operator_instance_type_mixed_on_node
        )
    return env


def _worker_volumes(
    config: TemplateConfig,
) -> Tuple[Optional[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The data dir host path plus any extra mounts, in the chart's shapes.

    ``TemplateConfig.volume_mounts`` guarantees the reserved data-dir mount is
    present and first (a cluster created before the mount became configurable
    carries none at all), so index 0 is the data dir by construction and the rest
    are the caller's own.
    """
    mounts = config.volume_mounts
    if not mounts:
        return None, [], []

    data_dir = mount_host_path(mounts[0])

    extra_mounts: List[Dict[str, Any]] = []
    extra_volumes: List[Dict[str, Any]] = []
    for mount in mounts[1:]:
        extra_mounts.append(
            {
                "name": mount.name,
                "mountPath": mount.mount_path,
                "readOnly": mount.read_only,
            }
        )
        volume: Dict[str, Any] = {"name": mount.name}
        if mount.volume_source is not None:
            volume.update(
                mount.volume_source.model_dump(by_alias=True, exclude_none=True)
            )
        extra_volumes.append(volume)

    return data_dir, extra_mounts, extra_volumes


def applied_revision(values: Dict[str, Any], *extra: str) -> str:
    """A digest of everything an install of these values would do.

    Handed to the chart as ``appliedRevision`` so the chart records it in a
    ConfigMap as part of the release, and to the bootstrap Job as an environment
    variable. Comparing the two is what lets the Job skip a `helm upgrade` that
    would change nothing — see bootstrap.sh.

    Computed over the values *without* the field itself, which would otherwise
    have to contain its own digest. ``extra`` carries the inputs that are not
    values but still decide what an install does: the script, and the chart it
    installs.
    """
    payload = yaml.safe_dump(values, sort_keys=True, default_flow_style=False)
    digest = hashlib.sha256("\0".join([payload, *extra]).encode("utf-8"))
    return digest.hexdigest()[:16]


def build_chart_values(config: TemplateConfig) -> Dict[str, Any]:
    """Values for a worker-only install of the GPUStack chart.

    Reads ``config`` rather than the cluster directly so that every derived value
    — the data-dir mount, the image pull Secret names, the operator knobs — comes
    from the one place that already normalizes them.
    """
    registry, repository, tag = split_image_reference(config.image)
    k8s_options = config.k8s_options

    # Read once, handed out as copies. Dumping the same dict into two places
    # makes PyYAML emit an anchor and an alias, which Helm accepts but nobody
    # wants to read — and the generated anchor name shifts with key order, which
    # would move the values hash the bootstrap Job is named after.
    configured_node_selector = (
        k8s_options.node_selector if k8s_options else None
    ) or {}
    data_dir, extra_mounts, extra_volumes = _worker_volumes(config)

    values: Dict[str, Any] = {
        # No server and no gateway: this release exists to add workers to a
        # cluster whose server lives elsewhere. Helm cannot derive a sub-chart
        # condition from another value, so higress-core is turned off by name.
        "server": {"enabled": False},
        "higress-core": {"enabled": False},
        "image": {"repository": repository, "tag": tag},
        "registrationTokenSecretName": REGISTRATION_TOKEN_SECRET_NAME,
        "imagePullSecret": {
            # The manifest creates the pull Secrets itself, one per credential
            # entry, so the chart must reference them without owning them.
            "create": False,
        },
        "worker": {
            "enabled": True,
            "serverURL": config.server_url,
            "gpuVendors": [runtime.value for runtime in config.runtimes or []],
            "port": config.worker_port,
            "metricsPort": config.worker_metrics_port,
            "nodeSelector": dict(configured_node_selector),
            "extraVolumeMounts": extra_mounts,
            "extraVolumes": extra_volumes,
        },
        "gpustack-operator": {
            "worker": {
                "nodeSelector": dict(configured_node_selector),
                "env": _operator_env(config),
            },
        },
    }

    if data_dir:
        values["worker"]["dataDir"] = data_dir

    # Explicit even when empty: the chart's default references the canonical
    # Secret it would otherwise create, and with `create: false` that would
    # leave every pod pointing at a Secret nobody made.
    values["global"] = {
        "imagePullSecrets": [
            {"name": secret.name} for secret in config.image_pull_secrets
        ],
    }

    if registry:
        values["global"]["hub"] = registry
        # Also set on the operator's own tree. `hub` reaches it only from the
        # release that carries the alias it added in 0.8.7; setting the key that
        # tree has always read keeps a mirrored install whole on older pins too,
        # and is harmless once the alias makes it redundant.
        values["gpustack-operator"].setdefault("global", {})["imageRegistry"] = registry

    if config.container_namespace:
        values["gpustack-operator"].setdefault("global", {})[
            "imageNamespace"
        ] = config.container_namespace

    operator_image = k8s_options.operator_image if k8s_options else None
    if operator_image:
        image_registry, image_repository, image_tag = split_image_reference(
            operator_image
        )
        operator_values: Dict[str, Any] = {"repository": image_repository}
        if image_tag:
            operator_values["tag"] = image_tag
        values["gpustack-operator"]["image"] = operator_values
        if image_registry:
            values["gpustack-operator"].setdefault("global", {})[
                "imageRegistry"
            ] = image_registry

    return _merge_helm_values(values, k8s_options.helm_values if k8s_options else None)


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """``overlay`` over ``base``, per key and depth-first.

    A list replaces rather than extends, which is what Helm's own value merging
    does — an overlay that meant to add one entry to `global.imagePullSecrets`
    and wrote only that entry gets exactly that, in both tools.
    """
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _merge_helm_values(
    values: Dict[str, Any], overlay: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Fold a cluster's own chart values over the derived ones.

    The caller's win, except for the paths that decide what this deployment is:
    those are restored afterwards. ``K8sOptions`` already refuses them, so this
    only catches a row written around the API — but a release that quietly does
    not match the cluster it was issued for is worth two defences.
    """
    if not overlay:
        return values

    merged = _deep_merge(values, overlay)
    for path in SERVER_OWNED_VALUE_PATHS:
        segments = path.split(".")
        source: Any = values
        target: Any = merged
        for segment in segments[:-1]:
            source = source.get(segment, {}) if isinstance(source, dict) else {}
            target = target.setdefault(segment, {})
        leaf = segments[-1]
        if isinstance(source, dict) and leaf in source:
            target[leaf] = source[leaf]
        elif isinstance(target, dict):
            target.pop(leaf, None)
    return merged
