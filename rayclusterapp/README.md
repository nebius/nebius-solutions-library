# Ray Job Helm Chart with Kueue Support
Two Helm charts are provided:
- kueue-bootstrap : It creates all required K8s cluster wide kueue resources to support gang scheduling for a Rayjob:
    https://docs.ray.io/en/latest/cluster/kubernetes/examples/rayjob-kueue-gang-scheduling.html

- ray-job : This helm chart is used to submit a Rayjob to a Ray cluster:
    https://docs.ray.io/en/latest/cluster/kubernetes/getting-started/rayjob-quick-start.html 

## Instructions to install Kuberay operator and a Ray cluster
### Make sure to set the raycluster-values.yaml file based on your settings
### Rayjob also has the capability to create a Ray cluster with the job submission at the same time
```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm install kuberay-operator kuberay/kuberay-operator --namespace kuberay-operator --create-namespace
helm install raycluster1 kuberay/ray-cluster -f raycluster-values.yaml --namespace raycluster1 --create-namespace
```
----
## Install Kueue for Gang Scheduling
```bash
helm install kueue oci://registry.k8s.io/kueue/charts/kueue \
  --version=0.13.0 \
  --namespace  kueue-system \
  --create-namespace
```
## Bootstrap your cluster for Kueue support
```bash
cd kueue-bootstrap
helm install kueue-bs . --namespace kueue-system
```
----
## Example of submitting a Ray job to the Ray Cluster using the helm chart
### Make sure to set the values.yaml file in the helm chart based on your settings
```bash
cd ray-job
helm install rayjob1 . --namespace raycluster1
```
### Uninstall the job
```bash
helm uninstall rayjob1 -n raycluster1
```
----
## Example of building a Docker image for the Head, Worker and Driver job pods
### It's prefered to build one image for all 3 roles; here is an example:
```bash
docker build -t rezabah/rayclusterapp-py39-cu128:0.1.1 .
docker push rezabah/rayclusterapp-py39-cu128:0.1.1
```
### Build command for MAC Laptop with Arm cpu:
```bash
docker buildx build --platform linux/amd64 -t rezabah/rayclusterapp-py39-cu128:0.1.1 --push .
```
