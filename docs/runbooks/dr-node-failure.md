# DR Runbook: Node / EC2 Instance Failure

**RTO**: 5 minutes (pod reschedule) / 10 minutes (new node provision)
**RPO**: 0 (stateless API and worker pods) / flush-to-disk for stateful pods

## Symptoms

- `AEOSNodeDiskPressure` alert, or `kubectl get nodes` shows `NotReady`
- Pods in `Pending` state (no schedulable node)
- `AEOSWorkerDown` fires due to pods not rescheduling
- EC2 instance status check failure in AWS Console

## Triage

```bash
# Check node health
kubectl get nodes -o wide
kubectl describe node <node-name> | tail -30

# Find pods affected by node failure
kubectl get pods --all-namespaces \
  --field-selector=spec.nodeName=<failed-node>

# Check recent node events
kubectl get events --all-namespaces \
  --field-selector=involvedObject.kind=Node \
  --sort-by='.lastTimestamp' | tail -20
```

## Scenario A: Single Node Failure (Spot interruption or hardware fault)

Cluster Autoscaler (CA) detects the failure and provisions a replacement node within ~3-5 minutes. Pods reschedule automatically.

```bash
# Watch node recovery
kubectl get nodes -w

# If pods are stuck Terminating (due to node loss)
kubectl delete pods --all-namespaces \
  --field-selector=spec.nodeName=<failed-node> \
  --grace-period=0 --force

# Verify pods rescheduled
kubectl get pods -n aeos-api
kubectl get pods -n aeos-jobs
```

## Scenario B: Node Not Draining (stuck Terminating)

```bash
# Manually cordon the node to prevent new scheduling
kubectl cordon <node-name>

# Drain workloads off the node
kubectl drain <node-name> \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --timeout=120s

# If drain hangs (pods with PDBs blocking eviction):
# Temporarily check PDB status
kubectl get pdb --all-namespaces

# Force delete stuck pods
kubectl delete pod <pod-name> -n <namespace> --force --grace-period=0

# Terminate the EC2 instance
aws ec2 terminate-instances --instance-ids <ec2-instance-id>
```

## Scenario C: Multiple Nodes Down (partial AZ failure)

If an entire AZ is unavailable:

```bash
# Check which AZ the failed nodes are in
kubectl get nodes -o custom-columns='NODE:.metadata.name,AZ:.metadata.labels.topology\.kubernetes\.io/zone,STATUS:.status.conditions[-1].type'

# Scale ASG to force provisioning in healthy AZs
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name aeos-production-general \
  --desired-capacity 9   # Increase to cover failed AZ's workload

# Watch new nodes join
kubectl get nodes -w
```

API and worker pods have `podAntiAffinity` with `preferredDuring...` — they will spread across remaining AZs. StatefulSets (Redis, Kafka, Qdrant) have `requiredDuring...` so pods in the failed AZ will be Pending until the AZ recovers or their data volume is migrated.

### Migrate stuck StatefulSet pod to healthy AZ:
```bash
# Example: redis-1 stuck because its PVC is in the failed AZ
# 1. Delete the PVC (data loss if not replicated — Redis replica OK, primary NOT OK)
kubectl delete pvc data-redis-1 -n aeos-data

# 2. Delete the stuck pod — will recreate and get a new PVC in healthy AZ
kubectl delete pod redis-1 -n aeos-data

# 3. Monitor rejoin
kubectl exec -n aeos-data redis-0 -- redis-cli -a $REDIS_PASSWORD info replication
# Look for: connected_slaves:2
```

## Scenario D: Node Group Completely Gone (all nodes terminated)

```bash
# Force ASG to desired capacity (wakes up new nodes)
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name aeos-production-general \
  --desired-capacity 6

# Watch nodes provision
aws ec2 describe-instances \
  --filters "Name=tag:eks:cluster-name,Values=aeos-production" "Name=instance-state-name,Values=pending,running" \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,PrivateDnsName]'

kubectl get nodes -w
```

## Disk Pressure

```bash
# Check which disks are full
kubectl exec -n <namespace> <pod> -- df -h

# Identify large files
kubectl exec -n <namespace> <pod> -- du -sh /* 2>/dev/null | sort -rh | head -20

# For ephemeral storage (tmp/cache):
kubectl delete pod <pod>   # Pod recreates with empty emptyDir

# For PVC storage: resize (EBS supports online resize)
kubectl patch pvc <pvc-name> -n <namespace> \
  -p '{"spec":{"resources":{"requests":{"storage":"50Gi"}}}}'
```

## Post-Recovery Checklist

- [ ] All nodes Ready: `kubectl get nodes`
- [ ] No pods Pending > 2 minutes: `kubectl get pods --all-namespaces | grep Pending`
- [ ] StatefulSet replicas all Running: check Redis, Kafka, Qdrant
- [ ] PodDisruptionBudgets respected (no mass eviction happened)
- [ ] Cluster Autoscaler logs show healthy scaling: `kubectl logs -n kube-system -l app=cluster-autoscaler`
- [ ] API and worker health checks passing
