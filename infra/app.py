#!/usr/bin/env python3
"""CDK app entry point for the Connect ACGR Resource Replicator."""

import aws_cdk as cdk

from stack import ConnectAcgrReplicatorStack

app = cdk.App()
ConnectAcgrReplicatorStack(app, "ConnectAcgrReplicatorStack")
app.synth()
