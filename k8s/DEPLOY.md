app-monitor — Kubernetes Deployment
=====================================

Files are numbered in apply order. Always apply in sequence — later resources
depend on earlier ones (namespace must exist before PVCs, PVCs before pods, etc.)


Step 1 — Deploy app-monitor
-----------------------------

    microk8s kubectl apply -f k8s/01-namespace.yaml
    microk8s kubectl apply -f k8s/02-pv.yaml
    microk8s kubectl apply -f k8s/03-pvc.yaml
    microk8s kubectl apply -f k8s/06-deployment.yaml
    microk8s kubectl apply -f k8s/07-service.yaml             # ClusterIP — GELF/CEF for K8s pods
    microk8s kubectl apply -f k8s/08-service-external.yaml    # LoadBalancer — Docker hosts + HSG
    microk8s kubectl apply -f k8s/09-frontend-ingress.yaml    # dashboard Service + Ingress at monitor.lab.int

Verify:

    microk8s kubectl get pods -n monitoring
    microk8s kubectl get svc  -n monitoring

Check that:
  - Pod status is Running
  - service/app-monitor         has a ClusterIP (used by K8s pods and ingress)
  - service/app-monitor-external has an EXTERNAL-IP from MetalLB (used by Docker hosts / HSG)
  - Ingress admits monitor.lab.int

Dashboard:     https://monitor.lab.int
GELF inbound:  <EXTERNAL-IP>:12201  (UDP and TCP)
CEF inbound:   <EXTERNAL-IP>:12202  (TCP — from HSG)


Step 2 — Deploy applog shared volume (once per cluster + once per app namespace)
----------------------------------------------------------------------------------
The applog package lives on the host at /apps/app-monitor/data/applog and is
mounted read-only into monitored pods via a shared PV/PVC — no image rebuilds
needed when applog is updated.

    # Cluster-wide PV (apply once):
    microk8s kubectl apply -f k8s/04-applog-pv.yaml

    # PVC per namespace that has monitored pods (repeat for each namespace):
    microk8s kubectl apply -f k8s/05-applog-pvc.yaml -n <namespace>

Verify:

    microk8s kubectl get pv  applog-pv
    microk8s kubectl get pvc applog-pvc -n <namespace>

Both should show STATUS = Bound.


Step 3 — Add monitoring to an existing K8s app
------------------------------------------------
See data/applog/applog_deployment_guide.md Step 3B for the full walkthrough.

Summary of what gets added to the app's Deployment:

    env:
      #-----app-monitor-------------------------------
      - name: APP_MONITOR
        value: "1"
      - name: GELF_HOST
        value: "app-monitor.monitoring.svc.cluster.local"
      - name: GELF_PORT
        value: "12201"
      - name: CONTAINER_NAME
        value: "<stable-service-name>"
      - name: GELF_SOURCE_HOST
        valueFrom:
          fieldRef:
            fieldPath: spec.nodeName
      #-----------------------------------------------

    volumeMounts:
      #-----app-monitor-------------------------------
      - name: applog
        mountPath: /app/applog
        readOnly: true
      #-----------------------------------------------

    volumes:
      #-----app-monitor-------------------------------
      - name: applog
        persistentVolumeClaim:
          claimName: applog-pvc
          readOnly: true
      #-----------------------------------------------

After applying: kubectl rollout restart deployment/<name> -n <namespace>


Troubleshooting
---------------
Pod not starting:
    microk8s kubectl describe pod -n monitoring -l app=app-monitor

No EXTERNAL-IP on app-monitor-external (MetalLB not assigning):
    microk8s kubectl describe svc app-monitor-external -n monitoring
    microk8s kubectl get ipaddresspool -n metallb-system

GELF packets not arriving (run with --raw to see raw packets):
    microk8s kubectl logs -n monitoring -l app=app-monitor -f

Applog PVC stuck Pending:
    microk8s kubectl describe pvc applog-pvc -n <namespace>
    # Confirm applog-pv STATUS=Available and storageClassName matches (applog)
