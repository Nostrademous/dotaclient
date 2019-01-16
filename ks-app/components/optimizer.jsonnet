local params = std.extVar("__ksonnet/params").components["dotaservice"];

local worker(replicas) = {
    "replicas": replicas,
    "restartPolicy": "OnFailure",
    "template": {
        "spec": {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "cloud.google.com/gke-preemptible",
                                        "operator": "DoesNotExist"
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
            "containers": [
                {
                    "args": [
                        "--ip",
                        params.jobname + "-rmq.default.svc.cluster.local",
                        "--batch-size",
                        params.batch_size,
                        "--learning-rate",
                        params.learning_rate
                    ],
                    "command": [
                        "python3.7",
                        "optimizer.py"
                    ],
                    "image": "gcr.io/dotaservice-225201/dotaclient:" + params.dotaclient_image_tag,
                    "name": "pytorch",
                    "resources": {
                        "requests": {
                            "cpu": "300m",
                            "memory": "2048Mi"
                        }
                    }
                }
            ]
        }
    }
};

{
    "apiVersion": "kubeflow.org/v1beta1",
    "kind": "PyTorchJob",
    "metadata": {
        "name": "optimizer",
        "labels": {
            "app": "optimizer",
            "job": params.jobname,
        }
    },
    "spec": {
        "pytorchReplicaSpecs": {
            "Master": {
                "replicas": 1,
                "restartPolicy": "OnFailure",
                "template": {
                    "spec": {
                        "affinity": {
                            "nodeAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                    "nodeSelectorTerms": [
                                        {
                                            "matchExpressions": [
                                                {
                                                    "key": "cloud.google.com/gke-preemptible",
                                                    "operator": "DoesNotExist"
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        },
                        "containers": [
                            {
                                "args": [
                                    "--ip",
                                    params.jobname + "-rmq.default.svc.cluster.local",
                                    "--batch-size",
                                    params.batch_size,
                                    "--learning-rate",
                                    params.learning_rate,
                                    "--exp-dir",
                                    params.expname,
                                    "--job-dir",
                                    params.jobname,
                                ] + if params.pretrained_model == '' then [] else [
                                    '--pretrained_model', params.pretrained_model ,
                                ],
                                "command": [
                                    "python3.7",
                                    "optimizer.py"
                                ],
                                "env": [
                                    {
                                        "name": "GOOGLE_APPLICATION_CREDENTIALS",
                                        "value": "/etc/gcp/sa_credentials.json"
                                    }
                                ],
                                "image": "gcr.io/dotaservice-225201/dotaclient:" + params.dotaclient_image_tag,
                                "name": "pytorch",
                                "resources": {
                                    "requests": {
                                        "cpu": "200m",
                                        "memory": "2048Mi"
                                    }
                                },
                                "volumeMounts": [
                                    {
                                        "mountPath": "/etc/gcp",
                                        "name": "gcs-secret",
                                        "readOnly": true
                                    }
                                ]
                            }
                        ],
                        "volumes": [
                            {
                                "name": "gcs-secret",
                                "secret": {
                                    "items": [
                                        {
                                            "key": "sa_json",
                                            "path": "sa_credentials.json"
                                        }
                                    ],
                                    "secretName": "gcs-admin-secret"
                                }
                            }
                        ]
                    }
                }
            }, [if params.optimizers > 1 then 'Worker']: worker(params.optimizers-1),
        },
        "terminationGracePeriodSeconds": 30
    }
}