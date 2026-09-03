import json

import yaml
from sqlalchemy.dialects import sqlite

from gpustack.k8s.values import build_chart_values
from gpustack.k8s.manifest_template import (
    TemplateConfig,
)
from gpustack.schemas.clusters import ClusterRegistrationTokenPublic
from gpustack.schemas.clusters import (
    Cluster,
    DATA_DIR_MOUNT_NAME,
    GpuInstanceOptions,
    K8sOptions,
    is_gpu_service_k8s_options,
)

# PCI presence labels per vendor (mirrors _MANUFACTURER_PCI_ID).
NVIDIA_PCI_LABEL = "feature.node.kubernetes.io/pci-10de.present"
ASCEND_PCI_LABEL = "feature.node.kubernetes.io/pci-19e5.present"
AMD_PCI_LABEL = "feature.node.kubernetes.io/pci-1002.present"
# CPU node label (mirrors _CPU_NODE_LABEL).
CPU_NODE_LABEL = "feature.gpustack.ai/acceleratable"


def _registration():
    return ClusterRegistrationTokenPublic(
        token="t",
        server_url="http://server",
        image="gpustack/gpustack:test",
        env={"GPUSTACK_TOKEN": "t"},
        args=[],
    )


def _config(**kwargs):
    return TemplateConfig(
        registration=_registration(),
        cluster_owner_principal_identifier="alice",
        **kwargs,
    )


def _pod_spec(ds):
    return ds["spec"]["template"]["spec"]


# ---------------------------------------------------------------------------
# CPU-only mode — always rendered, even with no GPU runtimes.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Single-GPU-runtime mode — CPU DS + one GPU vendor DS. Label-minimal,
# affinity-free, with the vendor's PCI nodeSelector.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multi-vendor mode — CPU DS + one DS per GPU runtime, plus the cross-DS
# safety net (component/runtime labels + podAntiAffinity).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Image pull secrets — should be referenced by all DaemonSets (CPU + GPU).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Volume mounts — applied to all DaemonSets (CPU + GPU).
# ---------------------------------------------------------------------------


def _data_dir_volumes(ds):
    return [
        v
        for v in (_pod_spec(ds).get("volumes") or [])
        if v["name"] == DATA_DIR_MOUNT_NAME
    ]


# ---------------------------------------------------------------------------
# GPUSTACK_CONTAINER_NAMESPACE — derived from the gpustack image, not the
# operator image (the operator image may live in a different namespace).
# ---------------------------------------------------------------------------


def _config_with_image(image, **kwargs):
    registration = ClusterRegistrationTokenPublic(
        token="t",
        server_url="http://server",
        image=image,
        env={"GPUSTACK_TOKEN": "t"},
        args=[],
    )
    return TemplateConfig(
        registration=registration,
        cluster_owner_principal_identifier="alice",
        **kwargs,
    )


def test_container_namespace_default_gpustack_is_suppressed():
    # gpustack/gpustack:test → namespace "gpustack" is the built-in default,
    # so the operator already knows it and the env var is omitted.
    cfg = _config_with_image("gpustack/gpustack:test")
    assert cfg.container_namespace is None


def test_container_namespace_from_custom_gpustack_image():
    cfg = _config_with_image("myorg/gpustack:test")
    assert cfg.container_namespace == "myorg"


def test_container_namespace_strips_registry_and_keeps_deep_namespace():
    cfg = _config_with_image(
        "reg.io/myorg/sub/gpustack:v1",
        system_default_container_registry="reg.io",
    )
    assert cfg.container_namespace == "myorg/sub"


def test_container_namespace_ignores_operator_image_namespace():
    # The operator image lives in a different namespace than the gpustack
    # image; the env var must follow the gpustack image so the operator
    # composes sibling references against the right namespace.
    cfg = _config_with_image(
        "myorg/gpustack:test",
        k8s_options=K8sOptions(operatorImage="otherns/gpustack-operator:test"),
    )
    assert cfg.container_namespace == "myorg"


def test_container_namespace_strips_embedded_registry_from_image_override():
    # image_name_override may carry a full reference with an embedded registry
    # and no system_default_container_registry set. The registry (first segment
    # with a ".") must not leak into the namespace; quay.io/gpustack/gpustack
    # resolves to the default "gpustack" namespace → suppressed.
    cfg = _config_with_image("quay.io/gpustack/gpustack:dev")
    assert cfg.container_namespace is None


def test_container_namespace_strips_embedded_registry_with_port():
    cfg = _config_with_image("myreg:5000/org/gpustack:dev")
    assert cfg.container_namespace == "org"


# ---------------------------------------------------------------------------
# Operator env vars from k8s_options.operator.env
# ---------------------------------------------------------------------------


def _operator_deployment(docs):
    """Extract the operator worker Deployment from the embedded ConfigMap's
    template.yaml data (ytt-processed)."""
    cm = next(
        d
        for d in docs
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"] == "gpustack-operator-worker-deployment"
    )
    template_yaml = cm["data"]["template.yaml"]
    # The template.yaml is ytt-templated, but the Deployment portion is plain
    # YAML after the last ytt directive. Parse all YAML docs and find the
    # Deployment.
    inner_docs = [d for d in yaml.safe_load_all(template_yaml) if d]
    return next(d for d in inner_docs if d.get("kind") == "Deployment")


def _operator_deployment_env(docs):
    """Extract the operator worker Deployment container env list from the
    embedded ConfigMap's template.yaml data (ytt-processed)."""
    deploy = _operator_deployment(docs)
    return deploy["spec"]["template"]["spec"]["containers"][0].get("env") or []


# ---------------------------------------------------------------------------
# Worker Deployment upgrade shape — the worker fronts the aggregated API and
# finishes its whole install (Prepare) before it serves, so a RollingUpdate
# would keep the OLD replica answering the old API until the new one turns
# Ready, and Prepare's sequential CRD polls need a startup budget that covers
# them. The chart pins both; the image-mode template must match.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Worker ports — worker_port / worker_metrics_port from the cluster config
# must flow into the worker container (env, so prerun binds them), the
# DaemonSet containerPorts, and the Service ports + prometheus scrape
# annotation. Otherwise a cluster overriding the defaults renders a Service
# that routes to the wrong port and a worker that binds the defaults.
# ---------------------------------------------------------------------------


def _worker_service(docs):
    return next(
        d
        for d in docs
        if d.get("kind") == "Service" and d["metadata"]["name"] == "gpustack-worker"
    )


def _worker_container(ds):
    return _pod_spec(ds)["containers"][0]


def _container_env_map(container):
    return {e["name"]: e.get("value") for e in container.get("env", []) if "value" in e}


def _container_port_map(container):
    return {p["name"]: p["containerPort"] for p in container.get("ports", [])}


# ---------------------------------------------------------------------------
# NVIDIA MIG capability env — the NVIDIA container runtime only exposes the
# driver's MIG capability subtree to a container that declares it manages MIG,
# so without these the worker cannot even read a MIG instance the Operator
# carved. The declaration is that one runtime's contract, so it must land on
# the nvidia DaemonSet and on nothing else.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GPU Service operator knobs — the three settings GPUStack manages on a GPU
# Service cluster. Each is tri-state: unset means GPUStack does not manage it
# and the cluster's own value stands, which is a different instruction from an
# explicit off. Two things are under test here: that the tri-state survives
# both the schema round trip and the persisted column (the column's presence
# is the cluster-purpose signal), and that each *set* knob renders the
# GPUSTACK_* env entry the operator seeds its Setting from on first deploy.
# ---------------------------------------------------------------------------


def _persisted_k8s_options(k8s_options: K8sOptions) -> dict:
    """Serialize through the real ``clusters.k8s_options`` column.

    Drives the column's own bind processor rather than a hand-rolled
    ``jsonable_encoder`` call, so the test reads the
    ``exclude_none`` / ``exclude_unset`` / ``exclude_defaults`` flags off the
    schema itself. Changing them there has to fail here rather than slip past
    a duplicated literal.
    """
    processor = Cluster.__table__.c.k8s_options.type.bind_processor(sqlite.dialect())
    dumped = processor(k8s_options)
    return json.loads(dumped) if isinstance(dumped, str) else dumped


def _gpu_instance_env(**gpu_instance_options):
    """Operator container env map for a GPU Service cluster with these knobs."""
    # The values the operator's sub-chart is handed, which is where the three
    # tri-state knobs and the free-form operator env converge. On TemplateConfig
    # they are separate computed fields; asserting on one of them alone would
    # miss the merge that decides what the operator actually reads.
    values = build_chart_values(
        _config(
            k8s_options=K8sOptions(
                gpu_instance_options=GpuInstanceOptions(**gpu_instance_options)
            )
        )
    )
    return values["gpustack-operator"]["worker"]["env"]


def test_gpu_instance_options_round_trips_snake_keys():
    options = GpuInstanceOptions.model_validate(
        {
            "gpu_instances_access_static_address": "10.0.0.1",
            "gpu_instance_type_derived_from_node": False,
            "gpu_instance_type_mixed_on_node": True,
        }
    )
    assert options.gpu_instances_access_static_address == "10.0.0.1"
    assert options.gpu_instance_type_derived_from_node is False
    assert options.gpu_instance_type_mixed_on_node is True


def test_gpu_instance_options_round_trips_camel_keys():
    """The aliases must equal the operator/gateway keys exactly: the UI and API
    submit camel, and the column persists camel (``by_alias``)."""
    options = GpuInstanceOptions.model_validate(
        {
            "gpuInstancesAccessStaticAddress": "10.0.0.1",
            "gpuInstanceTypeDerivedFromNode": True,
            "gpuInstanceTypeMixedOnNode": False,
        }
    )
    assert options.gpu_instance_type_derived_from_node is True
    assert options.gpu_instance_type_mixed_on_node is False
    assert options.model_dump(by_alias=True, exclude_none=True) == {
        "gpuInstancesAccessStaticAddress": "10.0.0.1",
        "gpuInstanceTypeDerivedFromNode": True,
        "gpuInstanceTypeMixedOnNode": False,
    }


def test_unset_gpu_instance_knobs_are_none_not_false():
    """Tri-state at construction: an unset knob is ``None`` (unmanaged), never
    ``False``. The settings reconciler only ever writes a knob that is set, so
    collapsing the two would make it assert a value nobody asked GPUStack to
    own — on a catalog that is also administered by ``kubectl``."""
    options = GpuInstanceOptions()
    assert options.gpu_instance_type_derived_from_node is None
    assert options.gpu_instance_type_mixed_on_node is None


def test_all_unset_gpu_instance_options_persists_as_a_present_object():
    """The cluster-purpose signal has to survive the new knobs.

    ``k8s_options.gpu_instance_options`` being *present* is what makes a
    cluster GPU Service. The column persists with ``exclude_none`` /
    ``exclude_unset`` / ``exclude_defaults``, so a knob carrying a non-``None``
    default would be stripped back out — and if that ever emptied the object
    into ``null``, every knob-less GPU Service cluster would silently convert
    to Model Service on its next write, across the whole fleet.
    """
    persisted = _persisted_k8s_options(
        K8sOptions(gpu_instance_options=GpuInstanceOptions())
    )
    assert persisted == {"gpuInstanceOptions": {}}
    assert is_gpu_service_k8s_options(persisted) is True


def test_unmanaged_gpu_instance_knob_is_dropped_from_the_persisted_row():
    """An explicit null — what the form submits for "not managed" — is dropped,
    so an unmanaged knob reads back as unset rather than as ``False``, while a
    sibling explicit ``False`` is kept."""
    persisted = _persisted_k8s_options(
        K8sOptions.model_validate(
            {
                "gpuInstanceOptions": {
                    "gpuInstanceTypeDerivedFromNode": None,
                    "gpuInstanceTypeMixedOnNode": False,
                }
            }
        )
    )
    assert persisted == {"gpuInstanceOptions": {"gpuInstanceTypeMixedOnNode": False}}
    assert is_gpu_service_k8s_options(persisted) is True


def test_model_service_cluster_persists_without_gpu_instance_options():
    """The negative half of the signal: no knobs object, Model Service."""
    persisted = _persisted_k8s_options(K8sOptions())
    assert persisted == {}
    assert is_gpu_service_k8s_options(persisted) is False


def test_operator_env_seeds_set_gpu_instance_knobs():
    env = _gpu_instance_env(
        gpu_instance_type_derived_from_node=True,
        gpu_instance_type_mixed_on_node=True,
    )
    assert env["GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE"] == "true"
    assert env["GPUSTACK_INSTANCE_TYPE_MIXED_ON_NODE"] == "true"


def test_operator_env_seeds_explicit_false_as_the_string_false():
    """The sharp edge of the tri-state: an explicit off renders ``"false"``,
    not nothing — and as a *string*, because an unquoted YAML ``false`` is a
    boolean and a container env value must be a string."""
    env = _gpu_instance_env(
        gpu_instance_type_derived_from_node=False,
        gpu_instance_type_mixed_on_node=False,
    )
    assert env["GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE"] == "false"
    assert env["GPUSTACK_INSTANCE_TYPE_MIXED_ON_NODE"] == "false"
    assert isinstance(env["GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE"], str)
    assert isinstance(env["GPUSTACK_INSTANCE_TYPE_MIXED_ON_NODE"], str)


def test_operator_env_omits_unset_gpu_instance_knobs():
    """A GPU Service cluster managing none of the three seeds none of them, so
    the operator's own defaults apply."""
    env = _gpu_instance_env()
    assert "GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE" not in env
    assert "GPUSTACK_INSTANCE_TYPE_MIXED_ON_NODE" not in env
    assert "GPUSTACK_INSTANCE_ACCESS_STATIC_ADDRESS" not in env


def test_operator_env_seeds_one_knob_without_the_other():
    """The knobs are independent — managing one must not seed the other."""
    env = _gpu_instance_env(gpu_instance_type_derived_from_node=False)
    assert env["GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE"] == "false"
    assert "GPUSTACK_INSTANCE_TYPE_MIXED_ON_NODE" not in env


def test_operator_env_seeds_the_static_access_address():
    """The pre-existing third knob still seeds, alongside the two new ones."""
    env = _gpu_instance_env(
        gpu_instances_access_static_address="10.0.0.1",
        gpu_instance_type_derived_from_node=True,
    )
    assert env["GPUSTACK_INSTANCE_ACCESS_STATIC_ADDRESS"] == "10.0.0.1"
    assert env["GPUSTACK_INSTANCE_TYPE_DERIVED_FROM_NODE"] == "true"


def test_operator_env_static_address_survives_yaml_metacharacters():
    """The static address is administrator-supplied free text — the field takes
    any string — so it is JSON-quoted rather than interpolated bare.

    Each shape below breaks an unquoted ``value:`` differently: the bracket form
    of an IPv6 address with a port opens a flow sequence, ``{`` a flow mapping,
    ``*`` an alias, and a `` #`` starts a comment that truncates the value
    without any error at all. Plain double quotes would cover those four but
    break on the last two, which carry a quote and a backslash of their own —
    hence ``tojson``, which escapes them.
    """
    for address in (
        "[2001:db8::1]:8080",
        "{a}",
        "*anchor",
        "1.2.3.4 # note",
        'say "hi"',
        "back\\slash",
    ):
        env = _gpu_instance_env(gpu_instances_access_static_address=address)
        assert env["GPUSTACK_INSTANCE_ACCESS_STATIC_ADDRESS"] == address, address
