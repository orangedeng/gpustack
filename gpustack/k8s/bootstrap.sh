#!/bin/bash
#
# Installs the GPUStack chart into the cluster this Job runs in.
#
# Rendered into a ConfigMap by bootstrap.jinja and run by the bootstrap Job. The
# Job's name carries a hash of the values, so a changed configuration produces a
# new Job and this runs again; an unchanged one is a no-op apply. Every step here
# has to be safe to repeat, because `backoffLimit` restarts the script from the
# top after any failure.
#
# Values are read from the mounted ConfigMap rather than baked in, so if two
# Jobs ever overlap the later one cannot install an older configuration.

set -euo pipefail

RELEASE="${RELEASE:?RELEASE is required}"
NAMESPACE="${NAMESPACE:?NAMESPACE is required}"
CHART_URL="${CHART_URL:?CHART_URL is required}"
VALUES_FILE="${VALUES_FILE:?VALUES_FILE is required}"
DESIRED_REVISION="${DESIRED_REVISION:?DESIRED_REVISION is required}"

# Releases the operator creates at runtime for the applications this chart
# deploys itself. The operator installs an application only where no chart
# deploys it, so every cluster registered before this one — where the manifest
# deployed the operator alone — has these five, each owning objects this chart
# now renders.
#
# Adopting them is the operator's own documented migration (its
# docs/migration/to-subcharts.md), not a collision: nothing is torn down, the
# objects change release and keep running. The first four are the set that
# migration names; the device managers are here too because the pre-chart
# manifest left the operator to install those as well.
OPERATOR_APP_RELEASES="gpustack-kueue gpustack-node-feature-discovery gpustack-csi-driver-nfs gpustack-csi-driver-s3 gpustack-operator-device-manager"

log() { echo "[bootstrap] $*"; }
fail() {
  echo "[bootstrap] $*" >&2
  exit 1
}

for binary in helm kubectl jq; do
  command -v "${binary}" >/dev/null 2>&1 || fail "${binary} is not available in this image"
done

rendered=$(mktemp)
live=$(mktemp)
trap 'rm -f "${rendered}" "${live}"' EXIT

# The chart records the configuration it was installed from, as part of the
# release. When it already matches what this Job was created for, and the
# release is in a state that says the install finished, there is nothing to do.
#
# Not for correctness — `helm upgrade` with the same values changes no objects —
# but for `helm history`: every apply would otherwise add a no-op revision, and
# Helm keeps ten by default, so a handful of redundant applies would push the
# revision anyone would actually want to roll back to out of the list.
#
# Both conditions are required. A `failed` or `pending-*` release may well have
# applied this ConfigMap before stopping, so the recorded revision alone cannot
# say the install finished.
release_status=$(helm status "${RELEASE}" --namespace "${NAMESPACE}" -o json 2>/dev/null | jq -r '.info.status' || true)
recorded_revision=$(
  kubectl get configmap "${RELEASE}-applied-revision" \
    --namespace "${NAMESPACE}" \
    --ignore-not-found \
    -o jsonpath='{.data.appliedRevision}' 2>/dev/null || true
)
if [[ "${release_status}" == "deployed" && "${recorded_revision}" == "${DESIRED_REVISION}" ]]; then
  log "release is already at revision ${DESIRED_REVISION}, nothing to do"
  exit 0
fi
log "installing revision ${DESIRED_REVISION} (release is ${release_status:-absent} at ${recorded_revision:-none})"

# What this install would create, from the same chart and values it will use.
# Everything below reasons about this set rather than a hardcoded inventory, so
# a chart that starts or stops rendering an object needs no change here.
log "rendering the chart"
helm template "${RELEASE}" "${CHART_URL}" \
  --namespace "${NAMESPACE}" \
  --values "${VALUES_FILE}" >"${rendered}"

# Pre-flight, before anything is created or deleted: an object we are about to
# claim that already belongs to a *different* Helm release is a sibling
# release's, and adopting it would hand its objects to this release — the next
# `helm upgrade` on that release would then prune them. Helm's own ownership
# check cannot tell this case from the one below, so it is made here.
#
# Except for the operator's own application releases, where taking them over is
# the point rather than an accident. Which of them this install actually adopts
# is read from the cluster rather than assumed: a release only turns up here
# when the render names one of its objects, so an application the values switch
# off is never touched by the migration below.
#
# `helm template` does not contact the cluster, so this is the first API call
# and a failure at this point has changed nothing.
log "checking for objects owned by another release"
kubectl get -f "${rendered}" --ignore-not-found -o json 2>/dev/null >"${live}" || true
owners=$(
  jq -r --arg release "${RELEASE}" '
    [ (.items // [.])[]
      | (.metadata.annotations["meta.helm.sh/release-name"] // "") as $owner
      | select($owner != "" and $owner != $release)
      | "\($owner)\t\(.kind)/\(.metadata.name)"
    ] | .[]
  ' "${live}" 2>/dev/null || true
)

foreign=""
adopted_releases=""
while IFS=$'\t' read -r owner object; do
  [[ -n "${owner}" ]] || continue
  if [[ " ${OPERATOR_APP_RELEASES} " == *" ${owner} "* ]]; then
    [[ " ${adopted_releases} " == *" ${owner} "* ]] || adopted_releases="${adopted_releases} ${owner}"
    continue
  fi
  foreign="${foreign}${object} (release ${owner})
"
done <<<"${owners}"

if [[ -n "${foreign}" ]]; then
  fail "refusing to install: these objects belong to another Helm release, adopting them would let this release prune them:
${foreign}"
fi

adopted_list=""
for app_release in ${adopted_releases}; do
  adopted_list="${adopted_list:+${adopted_list},}${app_release}"
done

# A release of this name that owns a StatefulSet is a server install, not a
# previous run of this path: these values never render one. Upgrading it with
# worker-only values would re-render the server from them and prune whatever
# they leave out — quietly, because Helm considers that a normal upgrade.
#
# The name has to be this one: the chart derives object names from the release
# name, and `gpustack` is what makes them match the manifest that registered
# clusters before the chart, so they can be adopted instead of duplicated.
existing_statefulsets=$(
  helm get manifest "${RELEASE}" --namespace "${NAMESPACE}" 2>/dev/null |
    kubectl get -f - --ignore-not-found -o jsonpath='{range .items[?(@.kind=="StatefulSet")]}{.metadata.name} {end}' 2>/dev/null || true
)
if [[ -n "${existing_statefulsets// /}" ]]; then
  fail "refusing to install: release '${RELEASE}' in namespace '${NAMESPACE}' already owns StatefulSet(s) ${existing_statefulsets}, so it is a GPUStack server install. Installing workers here would re-render that release from worker-only values. Register this cluster into a different namespace."
fi

# A release left mid-operation by a killed Job blocks every later attempt with
# "another operation is in progress". Repair it, never uninstall: this release
# owns Kueue, whose CRDs are Helm-managed templates and whose custom resources
# carry controller finalizers, so an uninstall tears down the controller while
# the finalizers still pin the CRs — and it would take the worker DaemonSets
# with it.
case "${release_status}" in
pending-install)
  # No previous revision to roll back to: the release record is all that exists,
  # so dropping it lets the install start over. Whatever the interrupted attempt
  # managed to create is adopted below.
  log "clearing an interrupted first install"
  kubectl delete secret --namespace "${NAMESPACE}" \
    --ignore-not-found \
    --selector "owner=helm,name=${RELEASE}"
  ;;
pending-upgrade | pending-rollback)
  log "rolling back an interrupted upgrade"
  helm rollback "${RELEASE}" --namespace "${NAMESPACE}" --wait --timeout 5m
  ;;
esac

# A workload whose `spec.selector` still names the release being retired cannot
# be patched into this one: the field is immutable, and Helm fails the entire
# install on it. Deleting it lets the install recreate it — the same trade the
# operator chart makes, and the reason this runs before anything is installed
# rather than after a failure.
#
# The operator chart frees these itself, but only when its own release is being
# upgraded: on install it logs "nothing is adopted" and skips. That holds
# everywhere except here, where the release is new by construction — a cluster
# registered before the chart has no release of this name — while the objects
# being adopted are years old.
if [[ -n "${adopted_list}" ]]; then
  for kind in deployments daemonsets statefulsets; do
    stale=$(
      kubectl get "${kind}" --namespace "${NAMESPACE}" \
        --selector "app.kubernetes.io/instance in (${adopted_list}),app.kubernetes.io/managed-by=Helm" \
        -o json 2>/dev/null |
        jq -r --arg release "${RELEASE}" '
          .items[]
          | select((.spec.selector.matchLabels["app.kubernetes.io/instance"] // "")
                   | . != "" and . != $release)
          | .metadata.name
        ' || true
    )
    [[ -n "${stale}" ]] || continue
    log "deleting ${kind} whose selector adoption cannot rewrite: ${stale//$'\n'/ }"
    # shellcheck disable=SC2086 # deliberate word splitting: one name per line, none can contain spaces
    kubectl delete "${kind}" --namespace "${NAMESPACE}" ${stale} --ignore-not-found
  done
fi

# `--take-ownership` adopts objects that no release owns yet, which is how a
# cluster registered before the chart existed is migrated in place: its worker
# DaemonSets and operator Deployment keep running and become this release's.
# Safe only because the pre-flight above already refused anything owned by
# someone else.
#
# No `--wait`: this Job's job is to apply the release, not to babysit it. The
# operator's own startup can take minutes (it installs two custom resources that
# poll for CRDs), and a Job that outlives its deadline waiting would be retried
# into an upgrade it already completed.
log "installing ${RELEASE} into ${NAMESPACE}"
helm upgrade --install "${RELEASE}" "${CHART_URL}" \
  --namespace "${NAMESPACE}" \
  --values "${VALUES_FILE}" \
  --take-ownership

# The release records that still claim what was just adopted. Deleting a record
# leaves its objects alone — `helm uninstall` is what would delete them, and
# that is the hazard being closed: left in place, an `helm uninstall
# gpustack-kueue` by anyone takes Kueue out from under this release.
#
# The operator chart does this itself, in a `post-upgrade` hook. That hook does
# not run here: this release is being created, not upgraded, so Helm fires
# `pre-install` (which reaps a stranded Kueue and applies the subcharts' CRDs)
# and nothing after. Mirrors its files/migrate-post.sh.
if [[ -n "${adopted_list}" ]]; then
  log "retiring the operator's release records for:${adopted_releases}"
  for app_release in ${adopted_releases}; do
    kubectl delete secret --namespace "${NAMESPACE}" \
      --ignore-not-found \
      --selector "owner=helm,name=${app_release}"
  done

  # Adoption rewrote `app.kubernetes.io/instance` on everything the render
  # names, so an object still carrying a retired release's instance label is one
  # the operator's version of that application created and this chart's version
  # does not — owned by nobody now, and invisible to any later uninstall.
  #
  # CRDs, PersistentVolumes and PersistentVolumeClaims are left alone even when
  # they match: deleting a CRD takes every custom resource of that kind with it.
  prune_selector="app.kubernetes.io/instance in (${adopted_list}),app.kubernetes.io/managed-by=Helm"
  log "pruning what those releases left behind"
  for kind in deployments daemonsets statefulsets services serviceaccounts \
    configmaps secrets roles rolebindings poddisruptionbudgets jobs networkpolicies; do
    kubectl delete "${kind}" --namespace "${NAMESPACE}" \
      --ignore-not-found --selector "${prune_selector}" ||
      log "WARNING: could not prune every orphaned ${kind}"
  done
  for kind in clusterroles clusterrolebindings mutatingwebhookconfigurations \
    validatingwebhookconfigurations csidrivers storageclasses apiservices; do
    kubectl delete "${kind}" \
      --ignore-not-found --selector "${prune_selector}" ||
      log "WARNING: could not prune every orphaned ${kind}"
  done
fi

# Objects the pre-chart manifest created that this chart does not render. Helm
# prunes only what it rendered before, so it never learns about these: they are
# not in any release and would keep running unmanaged.
#
# By exact name, never by prefix or label: this Job's own name shares a prefix
# with the one it replaces.
log "removing objects left by the pre-chart manifest"
kubectl delete --namespace "${NAMESPACE}" --ignore-not-found \
  service/gpustack-worker \
  configmap/gpustack-operator-worker-deployment \
  job/gpustack-operator-worker-deployment
# The blunt grant the pre-chart manifest gave the worker. Left in place it keeps
# cluster-admin bound to the ServiceAccount, and the chart's fine-grained roles
# would only be added alongside it.
kubectl delete --ignore-not-found clusterrolebinding/gpustack-worker

log "done"
