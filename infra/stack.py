"""CDK stack for the Connect ACGR Resource Replicator."""

import os

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from constructs import Construct


class ConnectAcgrReplicatorStack(Stack):
    """Serverless infrastructure stack for the Amazon Connect ACGR Resource Replicator.

    Provisions:
    - AWS Lambda function with FastAPI backend (512 MB, 900s timeout)
    - Amazon API Gateway REST API with Lambda proxy integration and CORS
    - Amazon S3 bucket for frontend static assets
    - Amazon CloudFront distribution (Amazon S3 frontend + Amazon API Gateway backend /api/*)
    - Amazon Cognito user pool, app client and hosted sign-in domain; the REST API
      requires a valid Cognito ID token on every method
    - Amazon DynamoDB table for session persistence (with TTL)
    - AWS IAM roles for AWS Lambda execution
    - CDK outputs: Amazon CloudFront URL, Amazon API Gateway endpoint
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
            # Point-in-time recovery (cdk-nag AwsSolutions-DDB3). Additive and
            # applied in place — no downtime, no data migration.
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
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
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # ---------------------------------------------------------------
        # 2. AWS Lambda execution role with discovery/replication permissions
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

        # Amazon Lex V2 — discovery + ALGR replication + association + cleanup.
        # Only the actions the backend calls. The tool never creates or edits
        # bots, locales, intents, versions or aliases: ALGR replicates them and
        # the replica is read-only.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    # discovery (source) and checks (target)
                    "lex:ListBots",
                    "lex:DescribeBot",
                    "lex:ListBotAliases",
                    "lex:DescribeBotAlias",
                    "lex:ListBotLocales",
                    "lex:ListIntents",
                    "lex:DescribeIntent",
                    "lex:ListSlotTypes",
                    # ALGR
                    "lex:CreateBotReplica",
                    "lex:ListBotReplicas",
                    "lex:ListBotAliasReplicas",
                    # cleanup
                    "lex:DeleteBotReplica",
                    "lex:DeleteBot",
                    # resource policy so Connect can call the replica alias
                    # (ALGR does not replicate resource-based policies)
                    "lex:CreateResourcePolicy",
                    "lex:UpdateResourcePolicy",
                    "lex:DescribeResourcePolicy",
                ],
                resources=["*"],
            )
        )

        # Amazon Lex Global Resiliency — the first CreateBotReplica in an account makes
        # Lex create the AWSServiceRoleForLexV2Replication service-linked role on the
        # caller's behalf, which needs this permission. Scoped to that one role and
        # service, as in the AmazonLexFullAccess managed policy.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[
                    "arn:aws:iam::*:role/aws-service-role/replication.lexv2.amazonaws.com/"
                    "AWSServiceRoleForLexV2Replication*"
                ],
                conditions={
                    "StringEquals": {"iam:AWSServiceName": "replication.lexv2.amazonaws.com"}
                },
            )
        )

        # Amazon Lex V1 — discovery (classic bots)
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

        # Amazon Kinesis Data Streams — discovery + replication + cleanup
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

        # Amazon Data Firehose — discovery + replication + cleanup
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

        # Amazon Kinesis Video Streams — discovery + replication + cleanup
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

        # Amazon Connect — approved origins for association
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

        # S3 — AWS Lambda code download during replication + Amazon S3 bucket replication + cleanup
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

        # Amazon Q in Connect — assistant and knowledge base replication
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "wisdom:ListAssistants",
                    "wisdom:ListTagsForResource",
                    "wisdom:ListKnowledgeBases",
                    "wisdom:GetKnowledgeBase",
                    "wisdom:GetAssistant",
                    "wisdom:CreateAssistant",
                    "wisdom:CreateKnowledgeBase",
                    "wisdom:CreateAssistantAssociation",
                    # CreateAssistant/CreateKnowledgeBase with tags require the
                    # separate TagResource permission — without it the calls
                    # fail with AccessDenied on wisdom:TagResource.
                    "wisdom:TagResource",
                    "wisdom:UntagResource",
                ],
                resources=["*"],
            )
        )

        # Amazon Connect — integration associations (Wisdom linking)
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:ListIntegrationAssociations",
                    "connect:CreateIntegrationAssociation",
                ],
                resources=["*"],
            )
        )

        # AWS Step Functions permissions for API Lambda role (Task 12.2)
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
        # 3. AWS Lambda functions
        # ---------------------------------------------------------------
        backend_code_path = os.path.join(os.path.dirname(__file__), "..", "backend")

        # 3a. Resource AWS Lambda — single-resource replication for Step Functions
        resource_lambda = _lambda.Function(
            self,
            "ResourceReplicatorLambda",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="replication.resource_lambda_handler.handler",
            code=_lambda.Code.from_asset(backend_code_path),
            memory_size=512,
            timeout=Duration.seconds(900),
            role=lambda_role,
            # X-Ray active tracing (cdk-nag Serverless-LambdaTracing).
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "SESSION_TABLE_NAME": session_table.table_name,
                "FLOW_ANALYSIS_TABLE_NAME": flow_analysis_table.table_name,
                "DEPLOYMENT_MODE": "lambda",
                "LOG_LEVEL": "INFO",
            },
        )

        # ---------------------------------------------------------------
        # 3b. AWS Step Functions state machine — dependency-ordered replication
        # ---------------------------------------------------------------
        # Resource AWS Lambda task — invoked for each resource
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
        # Execution log group for the state machine (cdk-nag AwsSolutions-SF1).
        state_machine_log_group = logs.LogGroup(
            self,
            "ReplicationStateMachineLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        state_machine = sfn.StateMachine(
            self,
            "ReplicationStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(outer_map),
            timeout=Duration.hours(2),
            # Log ALL events (AwsSolutions-SF1). Execution data is excluded so
            # inputs/outputs are not written to logs.
            logs=sfn.LogOptions(
                destination=state_machine_log_group,
                level=sfn.LogLevel.ALL,
                include_execution_data=False,
            ),
            # X-Ray tracing (AwsSolutions-SF2, Serverless-StepFunctionStateMachineXray).
            tracing_enabled=True,
        )

        # Grant Step Functions permission to invoke the Resource Lambda
        resource_lambda.grant_invoke(state_machine)

        # 3c. API AWS Lambda — FastAPI backend via Mangum
        backend_function = _lambda.Function(
            self,
            "ReplicatorBackend",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_handler.handler",
            code=_lambda.Code.from_asset(backend_code_path),
            memory_size=512,
            timeout=Duration.seconds(900),
            role=lambda_role,
            # X-Ray active tracing (cdk-nag Serverless-LambdaTracing).
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "DEPLOYMENT_MODE": "lambda",
                "SESSION_TABLE_NAME": session_table.table_name,
                "FLOW_ANALYSIS_TABLE_NAME": flow_analysis_table.table_name,
                "STATE_MACHINE_ARN": state_machine.state_machine_arn,
                "LOG_LEVEL": "INFO",
            },
        )

        # S3 access log bucket for the frontend bucket and Amazon CloudFront distribution.
        # ACLs enabled because CloudFront still uses ACL-based log delivery.
        access_logs_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAccessLogs",
                    enabled=True,
                    expiration=Duration.days(90),
                ),
            ],
        )

        # ---------------------------------------------------------------
        # 4. S3 bucket — frontend static assets
        # Hardened: SSE-S3 encryption at rest + TLS-only transport.
        # ---------------------------------------------------------------
        frontend_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # Server access logging (cdk-nag AwsSolutions-S1). Target is the
            # dedicated access-logs bucket created above.
            server_access_logs_bucket=access_logs_bucket,
            server_access_logs_prefix="s3-access-logs/frontend/",
        )

        # ---------------------------------------------------------------
        # 5. Amazon CloudFront distribution (constructed before API Gateway so
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

        # Amazon CloudFront Function for SPA URI rewriting — rewrites non-asset
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
            # Hardening: enable distribution access logging to S3.
            log_bucket=access_logs_bucket,
            log_file_prefix="cloudfront/",
            log_includes_cookies=False,
        )

        # ---------------------------------------------------------------
        # 5b. Amazon Cognito — sign-in for the UI and the API.
        #     Administrators create users (no self sign-up). The UI signs in
        #     with the hosted page (authorization code + PKCE, no client
        #     secret) and sends its ID token to the API, where a Cognito
        #     authorizer verifies it before the Lambda runs.
        # ---------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "ReplicatorUserPool",
            user_pool_name="ConnectAcgrReplicatorUsers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            # MFA is optional so a first user can sign in straight away; users
            # (or an administrator, via the console) can turn on an
            # authenticator app. Require it in production.
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # Like the DynamoDB tables: `cdk destroy` does not delete the users.
            removal_policy=RemovalPolicy.RETAIN,
        )

        callback_urls = [f"https://{distribution.distribution_domain_name}/"]
        extra_callbacks = self.node.try_get_context("additionalCallbackUrls")
        if extra_callbacks:
            # e.g. -c additionalCallbackUrls='["http://localhost:5173/"]' for local UI development
            callback_urls += list(extra_callbacks)

        user_pool_client = user_pool.add_client(
            "ReplicatorWebClient",
            user_pool_client_name="ConnectAcgrReplicatorWeb",
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=False,
                ),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=callback_urls,
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(1),
            auth_session_validity=Duration.minutes(3),
        )

        # No direct (password / SRP / custom) API sign-in: users sign in on the
        # hosted page, so only token refresh is allowed. An empty AuthFlow()
        # emits nothing and Cognito would then apply its broader defaults, so
        # set the list explicitly on the underlying resource.
        user_pool_client.node.default_child.explicit_auth_flows = [
            "ALLOW_REFRESH_TOKEN_AUTH"
        ]

        # Hosted-UI domain prefix: unique per stack (first block of the stack's
        # GUID); override with -c cognitoDomainPrefix=<prefix>.
        domain_prefix = self.node.try_get_context("cognitoDomainPrefix") or Fn.join(
            "-",
            [
                "acgr-replicator",
                Fn.select(0, Fn.split("-", Fn.select(2, Fn.split("/", self.stack_id)))),
            ],
        )
        user_pool_domain = user_pool.add_domain(
            "ReplicatorHostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        api_authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "ReplicatorAuthorizer",
            authorizer_name="ConnectAcgrReplicatorCognito",
            cognito_user_pools=[user_pool],
            identity_source="method.request.header.Authorization",
            results_cache_ttl=Duration.minutes(5),
        )

        # ---------------------------------------------------------------
        # 6. Amazon API Gateway REST API — Lambda proxy integration with CORS
        #    scoped to the Amazon CloudFront distribution (or an override via
        #    the `allowedOrigin` CDK context variable).
        # ---------------------------------------------------------------
        context_origin = self.node.try_get_context("allowedOrigin")
        if context_origin:
            allowed_origins = [context_origin]
        else:
            allowed_origins = [f"https://{distribution.distribution_domain_name}"]

        # Amazon API Gateway access logs to a CloudWatch Logs group.
        api_access_log_group = logs.LogGroup(
            self,
            "ReplicatorApiAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        api = apigw.RestApi(
            self,
            "ReplicatorApi",
            rest_api_name="ConnectAcgrReplicatorApi",
            description="REST API for the Connect ACGR Resource Replicator",
            # Every method requires a valid Cognito ID token (fail closed: a
            # method added later is protected too). The CORS preflight OPTIONS
            # methods are the only unauthenticated ones; browsers send no
            # credentials on a preflight, and they return no data.
            default_method_options=apigw.MethodOptions(
                authorizer=api_authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
            ),
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
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                # Hardening: enable access + execution logging on the prod stage.
                access_log_destination=apigw.LogGroupLogDestination(api_access_log_group),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=False,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=False,
                ),
                logging_level=apigw.MethodLoggingLevel.INFO,
                metrics_enabled=True,
                # X-Ray tracing (cdk-nag Serverless-APIGWXrayEnabled).
                tracing_enabled=True,
                # Stage-level default method throttling
                # (cdk-nag Serverless-APIGWDefaultThrottling). Values match the
                # existing usage plan (1000 rps / 2000 burst) so effective
                # request behaviour is unchanged.
                throttling_rate_limit=1000,
                throttling_burst_limit=2000,
            ),
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
            "UserPoolId",
            value=user_pool.user_pool_id,
            description="Amazon Cognito user pool ID (create users here)",
        )

        CfnOutput(
            self,
            "UserPoolClientId",
            value=user_pool_client.user_pool_client_id,
            description="Amazon Cognito app client ID used by the UI",
        )

        CfnOutput(
            self,
            "CognitoDomain",
            value=f"{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com",
            description="Amazon Cognito hosted sign-in domain (host name)",
        )

        CfnOutput(
            self,
            "StateMachineArn",
            value=state_machine.state_machine_arn,
            description="Step Functions state machine ARN for async replication",
        )

