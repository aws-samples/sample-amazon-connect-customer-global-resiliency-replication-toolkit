#!/usr/bin/env python3
"""CDK app entry point for the Connect ACGR Resource Replicator."""

import aws_cdk as cdk

from stack import ConnectAcgrReplicatorStack

app = cdk.App()
stack = ConnectAcgrReplicatorStack(app, "ConnectAcgrReplicatorStack")

# Optional cdk-nag security/compliance scan.
#
# Disabled by default so normal `cdk synth`/`cdk deploy` runs are unaffected.
# Enable explicitly for a security review:
#
#     cdk synth -c nag=true
#
# Findings are emitted as CDK annotations and written to CSV reports in
# cdk.out/. Errors will fail the synth by design, which is what you want in a
# CI gate; run it on its own rather than in the deploy path.
if app.node.try_get_context("nag"):
    from cdk_nag import AwsSolutionsChecks, ServerlessChecks

    # AwsSolutions is the general best-practice baseline. ServerlessChecks adds
    # Lambda/API Gateway/Step Functions specific rules that AwsSolutions does
    # not cover, which matters because this stack is entirely serverless.
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    cdk.Aspects.of(app).add(ServerlessChecks(verbose=True))

    # Justified suppressions for findings that are not remediable, deferred for
    # deployment safety, or accepted as documented proof-of-concept limitations.
    import nag_suppressions

    nag_suppressions.apply(stack)

    # Compliance packs are intentionally NOT enabled by default — they apply
    # only if the deployment is in scope for that regime. Enable as needed:
    #   from cdk_nag import HIPAASecurityChecks, NIST80053R5Checks, PCIDSS321Checks

app.synth()
