all(.items[];
  any(.metadata.ownerReferences[]?;
    .controller == true and .kind == "DaemonSet"
  ) or (
    .metadata.namespace == "kube-system" and
    .metadata.name == ("nvidia-device-plugin-" + $node) and
    .metadata.deletionTimestamp == null and
    .spec.nodeName == $node and .status.phase == "Running" and
    (.status.containerStatuses | length) > 0 and
    all(.status.containerStatuses[]; .ready == true) and
    (.metadata.ownerReferences | length) == 1 and
    .metadata.ownerReferences[0].apiVersion == "v1" and
    .metadata.ownerReferences[0].kind == "Node" and
    .metadata.ownerReferences[0].name == $node and
    .metadata.ownerReferences[0].uid == $node_uid and
    .metadata.ownerReferences[0].controller == true
  )
)
