"""CDK stack for the Connect ACGR Resource Replicator."""

import os

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from constructs import Construct


class ConnectAcgrReplicatorStack(Stack):
    """Serverless infrastructure stack for the Connect ACGR Resource Replicator.

    Provisions:
    - Lambda function with FastAPI backend (512 MB, 900s timeout)
    - API Gateway REST API with Lambda proxy integration and CORS
    - S3 bucket for frontend static assets
    - CloudFront distribution (S3 frontend + API Gateway backend /api/*)
    - DynamoDB table for session persistence (with TTL)
    - IAM roles for Lambda execution
    - CDK outputs: CloudFront URL, API Gateway endpoint
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # 1. DynamoDB Table — session persistence
        # ---------------------------------------------------------------
        session_table = dynamodb.Table(
            self,
            "ReplicatorSessions",
            table_name="ReplicatorSessions",
            partition_key=dynamodb.Attribute(
                name="session_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # DynamoDB Table — flow analysis jobs (ephemeral, 1-hour TTL)
        flow_analysis_table = dynamodb.Table(
            self,
            "FlowAnalysisJobs",
            table_name="FlowAnalysisJobs",
            partition_key=dynamodb.Attribute(
                name="job_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # ---------------------------------------------------------------
        # 2. Lambda execution role with discovery/replication permissions
        # ---------------------------------------------------------------
        lambda_role = iam.Role(
            self,
            "ReplicatorLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # CloudWatch Logs
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],
            )
        )

        # STS — identity verification
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            )
        )

        # Amazon Connect — discovery read APIs + Global Resiliency + association
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:DescribeInstance",
                    "connect:ListInstances",
                    "connect:ListLambdaFunctions",
                    "connect:ListBots",
                    "connect:ListInstanceStorageConfigs",
                    "connect:ListTrafficDistributionGroups",
                    "connect:DescribeTrafficDistributionGroup",
                    "connect:AssociateInstanceStorageConfig",
                    "connect:AssociateLambdaFunction",
                    "connect:AssociateBot",
                    "connect:DescribeInstanceAttribute",
                    "connect:UpdateInstanceAttribute",
                    "connect:DisassociateLambdaFunction",
                    "connect:DisassociateBot",
                    "connect:DisassociateInstanceStorageConfig",
                    "connect:ListContactFlows",
                    "connect:DescribeContactFlow",
                ],
                resources=["*"],
            )
        )

        # Lambda — discovery + replication + cleanup + association + layers
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:GetFunction",
                    "lambda:GetFunctionConfiguration",
                    "lambda:ListEventSourceMappings",
                    "lambda:CreateFunction",
                    "lambda:CreateEventSourceMapping",
                    "lambda:InvokeFunction",
                    "lambda:DeleteFunction",
                    "lambda:AddPermission",
                    "lambda:GetPolicy",
                    "lambda:TagResource",
                    "lambda:GetLayerVersion",
                    "lambda:PublishLayerVersion",
                ],
                resources=["*"],
            )
        )

        # Lex V2 — discovery + replication + cleanup
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lex:DescribeBot",
                    "lex:ListBotAliases",
                    "lex:ListBotLocales",
                    "lex:ListBotVersions",
                    "lex:ListIntents",
                    "lex:ListSlotTypes",
                    "lex:ListBots",
                    "lex:CreateBot",
                    "lex:CreateIntent",
                    "lex:CreateSlot",
                    "lex:CreateSlotType",
                    "lex:CreateBotLocale",
                    "lex:BuildBotLocale",
                    "lex:DescribeBotLocale",
                    "lex:CreateBotAlias",
                    "lex:CreateBotVersion",
                    "lex:DescribeBotAlias",
                    "lex:DescribeBotVersion",
                    "lex:UpdateBotAlias",
                    "lex:UpdateIntent",
                    "lex:DescribeIntent",
                    "lex:ListSlots",
                    "lex:DeleteBot",
                    "lex:DeleteBotAlias",
                    "lex:DeleteBotLocale",
                    "lex:DeleteBotVersion",
                    "lex:DeleteIntent",
                    "lex:DeleteSlot",
                    "lex:DeleteSlotType",
                    "lex:CreateBotReplica",
                    "lex:DeleteBotReplica",
                    "lex:ListBotReplicas",
                    "lex:DeleteBotChannel",
                    "lex:TagResource",
                    "lex:CreateResourcePolicy",
                    "lex:UpdateResourcePolicy",
                    "lex:DescribeResourcePolicy",
                ],
                resources=["*"],
            )
        )

        # Lex V1 — discovery (classic bots)
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lex:GetBot",
                    "lex:GetBotAliases",
                    "lex:GetIntent",
                    "lex:GetBots",
                    "lex:GetSlotType",
                    "lex:GetSlotTypes",
                ],
                resources=["*"],
            )
        )

        # Kinesis Data Streams — discovery + replication + cleanup
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kinesis:DescribeStream",
                    "kinesis:DescribeStreamSummary",
                    "kinesis:CreateStream",
                    "kinesis:IncreaseStreamRetentionPeriod",
                    "kinesis:StartStreamEncryption",
                    "kinesis:DeleteStream",
                    "kinesis:AddTagsToStream",
                ],
                resources=["*"],
            )
        )

        # Kinesis Firehose — discovery + replication + cleanup
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "firehose:DescribeDeliveryStream",
                    "firehose:CreateDeliveryStream",
                    "firehose:DeleteDeliveryStream",
                    "firehose:TagDeliveryStream",
                ],
                resources=["*"],
            )
        )

        # Kinesis Video Streams — discovery + replication + cleanup
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kinesisvideo:DescribeStream",
                    "kinesisvideo:CreateStream",
                    "kinesisvideo:ListStreams",
                    "kinesisvideo:DeleteStream",
                    "kinesisvideo:TagStream",
                ],
                resources=["*"],
            )
        )

        # IAM — discovery + replication + cleanup
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:GetRole",
                    "iam:CreateRole",
                    "iam:PutRolePolicy",
                    "iam:AttachRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListRolePolicies",
                    "iam:GetRolePolicy",
                    "iam:PassRole",
                    "iam:DeleteRole",
                    "iam:DeleteRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:TagRole",
                ],
                resources=["*"],
            )
        )

        # Service Quotas — quota comparison page
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "servicequotas:GetServiceQuota",
                    "servicequotas:GetAWSDefaultServiceQuota",
                    "servicequotas:ListServiceQuotas",
                    "servicequotas:ListAWSDefaultServiceQuotas",
                ],
                resources=["*"],
            )
        )

        # Connect — approved origins for association
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:ListApprovedOrigins",
                    "connect:AssociateApprovedOrigin",
                    "connect:DisassociateApprovedOrigin",
                ],
                resources=["*"],
            )
        )

        # S3 — Lambda code download during replication + S3 bucket replication + cleanup
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:GetObject",
                    "s3:CreateBucket",
                    "s3:HeadBucket",
                    "s3:PutBucketEncryption",
                    "s3:PutBucketVersioning",
                    "s3:PutBucketPublicAccessBlock",
                    "s3:GetBucketEncryption",
                    "s3:GetBucketVersioning",
                    "s3:DeleteBucket",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "s3:GetBucketPolicy",
                    "s3:PutBucketPolicy",
                    "s3:GetBucketCORS",
                    "s3:PutBucketCors",
                    "s3:GetLifecycleConfiguration",
                    "s3:PutLifecycleConfiguration",
                    "s3:GetBucketLocation",
                    "s3:PutBucketTagging",
                    "s3:PutBucketOwnershipControls",
                    "s3:GetBucketOwnershipControls",
                    "s3:PutPublicAccessBlock",
                    "s3:GetPublicAccessBlock",
                    "s3:GetBucketAcl",
                ],
                resources=["*"],
            )
        )

        # KMS — key resolution for cross-region encryption + Wisdom key replication
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:DescribeKey",
                    "kms:CreateKey",
                    "kms:CreateAlias",
                    "kms:ListAliases",
                    "kms:TagResource",
                ],
                resources=["*"],
            )
        )

        # Wisdom / Q in Connect — assistant and knowledge base replication
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "wisdom:ListAssistants",
                    "wisdom:ListTagsForResource",
                    "wisdom:ListKnowledgeBases",
                    "wisdom:GetKnowledgeBase",
                    "wisdom:CreateAssistant",
                    "wisdom:CreateKnowledgeBase",
                ],
                resources=["*"],
            )
        )

        # Connect — integration associations (Wisdom linking)
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:ListIntegrationAssociations",
                    "connect:CreateIntegrationAssociation",
                ],
                resources=["*"],
            )
        )

        # Step Functions permissions for API Lambda role (Task 12.2)
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "states:StartExecution",
                    "states:DescribeExecution",
                    "states:GetExecutionHistory",
                ],
                resources=["*"],
            )
        )

        # Grant DynamoDB read/write on the session table
        session_table.grant_read_write_data(lambda_role)
        flow_analysis_table.grant_read_write_data(lambda_role)

        # ---------------------------------------------------------------
        # 3. Lambda functions
        # ---------------------------------------------------------------
        backend_code_path = os.path.join(os.path.dirname(__file__), "..", "backend")

        # 3a. Resource Lambda — single-resource replication for Step Functions
        resource_lambda = _lambda.Function(
            self,
            "ResourceReplicatorLambda",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="replication.resource_lambda_handler.handler",
            code=_lambda.Code.from_asset(backend_code_path),
            memory_size=512,
            timeout=Duration.seconds(900),
            role=lambda_role,
            environment={
                "SESSION_TABLE_NAME": session_table.table_name,
                "FLOW_ANALYSIS_TABLE_NAME": flow_analysis_table.table_name,
                "DEPLOYMENT_MODE": "lambda",
                "LOG_LEVEL": "INFO",
            },
        )

        # ---------------------------------------------------------------
        # 3b. Step Functions state machine — dependency-ordered replication
        # ---------------------------------------------------------------
        # Resource Lambda task — invoked for each resource
        resource_task = sfn_tasks.LambdaInvoke(
            self,
            "ReplicateResource",
            lambda_function=resource_lambda,
            output_path="$.Payload",
        )

        # Inner Map: parallel resources within a dependency level
        inner_map = sfn.Map(
            self,
            "ProcessResources",
            items_path="$.resources",
            max_concurrency=10,
        )
        inner_map.item_processor(resource_task)

        # Outer Map: sequential dependency levels
        outer_map = sfn.Map(
            self,
            "ProcessLevels",
            items_path="$.levels",
            max_concurrency=1,
        )
        outer_map.item_processor(inner_map)

        # State machine
        state_machine = sfn.StateMachine(
            self,
            "ReplicationStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(outer_map),
            timeout=Duration.hours(2),
        )

        # Grant Step Functions permission to invoke the Resource Lambda
        resource_lambda.grant_invoke(state_machine)

        # 3c. API Lambda — FastAPI backend via Mangum
        backend_function = _lambda.Function(
            self,
            "ReplicatorBackend",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_handler.handler",
            code=_lambda.Code.from_asset(backend_code_path),
            memory_size=512,
            timeout=Duration.seconds(900),
            role=lambda_role,
            environment={
                "DEPLOYMENT_MODE": "lambda",
                "SESSION_TABLE_NAME": session_table.table_name,
                "FLOW_ANALYSIS_TABLE_NAME": flow_analysis_table.table_name,
                "STATE_MACHINE_ARN": state_machine.state_machine_arn,
                "LOG_LEVEL": "INFO",
            },
        )

        # ---------------------------------------------------------------
        # 4. S3 bucket — frontend static assets
        # ---------------------------------------------------------------
        frontend_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ---------------------------------------------------------------
        # 5. CloudFront distribution (constructed before API Gateway so
        #    the API's CORS can be scoped to the distribution domain).
        #    The /api/* behavior is wired in after the API is created via
        #    distribution.add_behavior(...).
        # ---------------------------------------------------------------
        # Origin Access Identity for S3
        oai = cloudfront.OriginAccessIdentity(
            self,
            "FrontendOAI",
            comment="OAI for Connect ACGR Replicator frontend",
        )
        frontend_bucket.grant_read(oai)

        # CloudFront Function for SPA URI rewriting — rewrites non-asset
        # URIs to /index.html so client-side routing works without relying
        # on custom error responses (which caused stale cache issues).
        spa_rewrite_function = cloudfront.Function(
            self,
            "SpaUriRewrite",
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {\n"
                "    var request = event.request;\n"
                "    var uri = request.uri;\n"
                "    if (uri.match(/\\.\\w+$/)) {\n"
                "        return request;\n"
                "    }\n"
                "    request.uri = '/index.html';\n"
                "    return request;\n"
                "}\n"
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment="Rewrite non-asset URIs to /index.html for SPA routing",
        )

        # Cache policy that respects origin Cache-Control headers.
        # index.html is served with no-cache, hashed assets with immutable/1yr.
        frontend_cache_policy = cloudfront.CachePolicy(
            self,
            "ReplicatorFrontendCachePolicy",
            cache_policy_name="ReplicatorFrontendCachePolicy",
            comment="Respects origin Cache-Control; short default TTL for index.html",
            default_ttl=Duration.seconds(0),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.days(365),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )

        distribution = cloudfront.Distribution(
            self,
            "ReplicatorDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_identity(
                    frontend_bucket, origin_access_identity=oai
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=frontend_cache_policy,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=spa_rewrite_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            default_root_object="index.html",
        )

        # ---------------------------------------------------------------
        # 6. API Gateway REST API — Lambda proxy integration with CORS
        #    scoped to the CloudFront distribution (or an override via
        #    the `allowedOrigin` CDK context variable).
        # ---------------------------------------------------------------
        context_origin = self.node.try_get_context("allowedOrigin")
        if context_origin:
            allowed_origins = [context_origin]
        else:
            allowed_origins = [f"https://{distribution.distribution_domain_name}"]

        api = apigw.RestApi(
            self,
            "ReplicatorApi",
            rest_api_name="ConnectAcgrReplicatorApi",
            description="REST API for the Connect ACGR Resource Replicator",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=allowed_origins,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=[
                    "Content-Type",
                    "X-Amz-Date",
                    "Authorization",
                    "X-Api-Key",
                    "X-Amz-Security-Token",
                ],
            ),
            deploy_options=apigw.StageOptions(stage_name="prod"),
        )

        # Proxy resource {proxy+} to catch all routes
        lambda_integration = apigw.LambdaIntegration(
            backend_function, proxy=True
        )
        api.root.add_method("ANY", lambda_integration)
        proxy_resource = api.root.add_resource("{proxy+}")
        proxy_resource.add_method("ANY", lambda_integration)

        # Usage plan — default throttling (1000 rps / 2000 burst) on the prod stage.
        usage_plan = api.add_usage_plan(
            "ReplicatorUsagePlan",
            name="ConnectAcgrReplicatorUsagePlan",
            throttle=apigw.ThrottleSettings(rate_limit=1000, burst_limit=2000),
        )
        usage_plan.add_api_stage(stage=api.deployment_stage)

        # Wire CloudFront's /api/* behavior to point at API Gateway now
        # that the API exists. CDK tokens resolve the domain at synth time.
        api_domain = f"{api.rest_api_id}.execute-api.{self.region}.amazonaws.com"
        distribution.add_behavior(
            "/api/*",
            origins.HttpOrigin(
                api_domain,
                origin_path="/prod",
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            ),
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        )

        # ---------------------------------------------------------------
        # 7. CDK Outputs
        # ---------------------------------------------------------------
        CfnOutput(
            self,
            "CloudFrontUrl",
            value=distribution.distribution_domain_name,
            description="CloudFront distribution URL for the Replicator UI",
        )

        CfnOutput(
            self,
            "ApiGatewayEndpoint",
            value=api.url,
            description="API Gateway endpoint URL for the Replicator backend",
        )

        CfnOutput(
            self,
            "StateMachineArn",
            value=state_machine.state_machine_arn,
            description="Step Functions state machine ARN for async replication",
        )

